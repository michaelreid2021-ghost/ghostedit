#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: apply_tail_trims.py
Version: 1.4.0
Last Modified: 2025-08-16

Purpose:
  Apply END trims using 21_tailcut_offsets.json by passing the exact
  "seconds to trim from the END" into 103-reflow-and-more.py, mirroring how
  START trims pass first_content_seconds directly.

Behavior:
  - For each braw_filename in 21_tailcut_offsets.json:
      seconds_to_trim_end = asset_duration_seconds - proposed_end_seconds
    where asset_duration_seconds is read from Info.fcpxml for the matching asset
    inside the multicam referenced by the timeline ref (multicam_parent.id).
  - Calls:
      python 103-reflow-and-more.py --fcpxml Info.fcpxml --out output.fcpxml \
        adjust-clip-end --ref <timeline_ref_id> --seconds <seconds_to_trim_end>

Usage:
  python apply_tail_trims.py \
    --base "E:\\davinci\\projects\\" \
    --projects "E:\\ai_editing_assistant\\fcpxml\\projects\\" \
    --tail-offsets "E:\\davinci\\projects\\<active>\\ai_working_files\\21_tailcut_offsets.json" \
    --python-exe python \
    --tool "E:\\ai_editing_assistant\\fcpxml\\103-reflow-and-more.py" \
    [--manifest "E:\\davinci\\projects\\<active>\\ai_working_files\\master_manifest.json"] \
    [--active-file "E:\\ai_editing_assistant\\fcpxml\\projects\\active_project.txt"]

Notes:
  - This script does NOT compute new durations or compare to current timeline lengths.
    It only computes the end trim delta from asset duration, just like start trims.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

# ----------------------------
# IO helpers
# ----------------------------

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def read_json(path: Path) -> Any:
    return json.loads(read_text(path))

def build_paths(base: Path, projects_folder: Path, active_name: str) -> Tuple[Path, Path, Path, Path, Path]:
    project_root = (base / active_name).resolve()
    ai_dir = project_root / "ai_working_files"
    fcpxml_in = project_root / "Info.fcpxml"
    fcpxml_out = project_root / "output.fcpxml"
    manifest_path = ai_dir / "master_manifest.json"
    return project_root, ai_dir, fcpxml_in, fcpxml_out, manifest_path

def find_first_manifest_clip(manifest: List[Dict[str, Any]], braw_filename: str) -> Optional[Dict[str, Any]]:
    needle = str(braw_filename).strip().lower()
    for clip in manifest:
        if str(clip.get("braw_filename", "")).strip().lower() == needle:
            return clip
    return None

def timeline_ref_id_from_manifest(clip: Dict[str, Any]) -> Optional[str]:
    parent = clip.get("multicam_parent")
    if isinstance(parent, dict) and parent.get("id"):
        return str(parent.get("id"))
    if clip.get("asset_id"):
        return str(clip.get("asset_id"))
    return None

# ----------------------------
# FCPXML helpers
# ----------------------------

def parse_fcpx_fraction_seconds(s: str) -> float:
    """
    Convert FCPXML time format 'NUM/DENs' to float seconds.
    Example: '1001/24000s' -> 1001/24000 seconds.
    """
    s = s.strip()
    if not s.endswith('s') or '/' not in s:
        raise ValueError(f"Invalid FCPXML time literal: {s}")
    num_str, den_str = s[:-1].split('/', 1)
    return float(int(num_str)) / float(int(den_str))

def fcpx_asset_duration_seconds_for_braw(fcpxml: Path, timeline_ref_id: str, braw_filename: str) -> Optional[float]:
    """
    Find the multicam <media id=timeline_ref_id>, then locate the <asset-clip> within its mc-angles
    matching the given braw_filename (by 'name'). Return its 'duration' in seconds.
    If a direct match is not found, return the MAX duration among the angles for safety.
    """
    tree = ET.parse(str(fcpxml))
    root = tree.getroot()
    resources = root.find('.//resources')
    if resources is None:
        return None

    media = resources.find(f".//media[@id='{timeline_ref_id}']")
    if media is None:
        return None
    mclang = media.find('./multicam')
    if mclang is None:
        return None

    target_key = str(braw_filename).strip().lower()
    max_dur = 0.0
    match_dur = None

    for angle in mclang.findall('./mc-angle'):
        for acl in angle.findall('./asset-clip'):
            name = (acl.get('name') or '').strip()
            dur_attr = acl.get('duration')
            if not dur_attr:
                continue
            try:
                dur_sec = parse_fcpx_fraction_seconds(dur_attr)
            except Exception:
                continue
            max_dur = max(max_dur, dur_sec)
            if name.strip().lower() == target_key:
                match_dur = dur_sec

    return match_dur if match_dur is not None else (max_dur if max_dur > 0 else None)

# ----------------------------
# Core apply
# ----------------------------

def apply_adjust_clip_end(
    python_exe: str,
    tool_path: Path,
    fcpxml_in: Path,
    fcpxml_out: Path,
    ref: str,
    seconds_to_trim: float
) -> None:
    cmd = [
        python_exe,
        str(tool_path),
        "--fcpxml", str(fcpxml_in),
        "--out", str(fcpxml_out),
        "adjust-clip-end",
        "--ref", ref,
        "--seconds", f"{seconds_to_trim:.3f}",
    ]
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout)
        sys.stderr.write(r.stderr)
        raise RuntimeError(f"adjust-clip-end failed for ref {ref}")
    if not fcpxml_out.exists():
        raise FileNotFoundError(f"Expected output not found: {fcpxml_out}")
    backup = fcpxml_in.with_suffix(f".{ref}.fcpxml")
    try:
        fcpxml_in.replace(backup)  # rotate backup
    except Exception:
        pass
    fcpxml_out.replace(fcpxml_in)
    print(f"[ok] adjust-clip-end ref={ref} trim={seconds_to_trim:.3f}s")

# ----------------------------
# Main
# ----------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Apply end trims using asset_duration - proposed_end_seconds (no timeline math).")
    ap.add_argument("--base", required=True, type=Path, help="Base Davinci projects folder (e.g., E:\\davinci\\projects\\)")
    ap.add_argument("--projects", required=True, type=Path, help="Assistant projects folder with active_project.txt")
    ap.add_argument("--active-file", type=Path, default=None, help="Optional explicit path to active_project.txt")
    ap.add_argument("--manifest", type=Path, default=None, help="Optional explicit path to master_manifest.json")
    ap.add_argument("--tail-offsets", required=True, type=Path, help="Path to 21_tailcut_offsets.json")
    ap.add_argument("--python-exe", type=str, default="python", help="Python executable to run 103-reflow-and-more.py")
    ap.add_argument("--tool", type=Path, default=Path("103-reflow-and-more.py"), help="Path to the reflow tool")
    args = ap.parse_args()

    active_file = args.active_file or (args.projects / "active_project.txt")
    active_name = read_text(active_file).strip()
    if not active_name:
        raise RuntimeError("active_project.txt was empty.")

    project_root, ai_dir, fcpxml_in, fcpxml_out, manifest_default = build_paths(args.base, args.projects, active_name)
    manifest_path = args.manifest or manifest_default

    if not fcpxml_in.exists():
        raise FileNotFoundError(f"Missing Info.fcpxml: {fcpxml_in}")
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing master manifest: {manifest_path}")

    manifest = read_json(manifest_path)
    if not isinstance(manifest, list):
        raise ValueError("master_manifest.json must be a JSON array of clip objects.")

    tail_offsets_arr = read_json(args.tail_offsets)
    if not isinstance(tail_offsets_arr, list):
        raise ValueError("Tail offsets file must be a JSON array.")

    print(f"[i] Active project : {active_name}")
    print(f"[i] Project root   : {project_root}")
    print(f"[i] Info.fcpxml    : {fcpxml_in}")
    print(f"[i] Manifest       : {manifest_path}")
    print(f"[i] Tail offsets   : {args.tail_offsets}")

    for row in tail_offsets_arr:
        braw_filename = str(row.get("braw_filename", "")).strip()
        if not braw_filename:
            print("[!] Skipping entry with no braw_filename.")
            continue

        proposed_end_seconds = float(row.get("proposed_end_seconds", 0.0))
        if proposed_end_seconds <= 0.0:
            print(f"[!] Non-positive proposed_end_seconds for {braw_filename}; skipping.")
            continue

        clip = find_first_manifest_clip(manifest, braw_filename)
        if not clip:
            print(f"[!] Not found in manifest: {braw_filename}")
            continue

        ref = timeline_ref_id_from_manifest(clip)
        if not ref:
            print(f"[!] Missing timeline ref for clip: {braw_filename}")
            continue

        asset_dur = fcpx_asset_duration_seconds_for_braw(fcpxml_in, ref, braw_filename)
        if asset_dur is None or asset_dur <= 0.0:
            print(f"[!] Could not resolve asset duration for ref={ref} ({braw_filename}); skipping.")
            continue

        seconds_to_trim_end = max(asset_dur - proposed_end_seconds, 0.0)

        print(
            f"[*] {braw_filename} -> ref={ref} asset_dur={asset_dur:.3f}s "
            f"proposed_end={proposed_end_seconds:.3f}s trim_end={seconds_to_trim_end:.3f}s"
        )

        apply_adjust_clip_end(
            python_exe=args.python_exe,
            tool_path=args.tool if args.tool.is_absolute() else (Path.cwd() / args.tool),
            fcpxml_in=fcpxml_in,
            fcpxml_out=fcpxml_out,
            ref=ref,
            seconds_to_trim=seconds_to_trim_end
        )

    print("[✓] Tail trims applied via 103 (end = asset_duration - proposed_end_seconds).")

if __name__ == "__main__":
    main()
