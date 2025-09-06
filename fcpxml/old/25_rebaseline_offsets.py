#!/usr/bin/env python3
# Version: 2.6.0
# Last Modified: 2025-08-14
# Step 25 — Rebaseline FCPXML Offsets (Hardened)
#
# Key features:
# - Uses active project's working directory (projects/{active_project.txt, <name>.json}).
# - Reads Step 20 output (20_countdown_offsets.json) and supports --source {proposed|first|two|midpoint}.
# - Signed global --nudge (seconds) applied after source selection.
# - Ripple uses CURRENT cutoff (prevents overtake gaps).
# - Optional --contiguity pass to pack the spine end-to-end (good hygiene).
# - **HARDENING**: Baseline selection for each mc-clip start:
#     baseline = current | tcStart | override, chosen by:
#       --baseline (current run) OR project policy in <project>.json when --baseline=auto.
#     desired_start = baseline_start + chosen_seconds
#     delta = desired_start - current_start (idempotent; template-proof)
# - Baseline audit: logs baseline mismatch (current_start - tcStart) and flags when beyond threshold.
# - Emits 25_adjustment_map.json with detailed per-clip info.
# - Emits 25_multicam_registry.json (inventory of multicam containers/angles).
#
# Project policy schema (optional, inside the per-project JSON you already have):
# {
#   "policy": {
#     "default": { "baseline": "current", "threshold_seconds": 0.5 },
#     "takes": {
#       "12": {
#         "baseline": "tcStart",                   # "current" | "tcStart" | "override"
#         "starting_override_seconds": null,       # when baseline="override", interpreted as seconds relative to tcStart
#         "preserve": false                        # if true, do not move this clip (still listed in outputs)
#       }
#     },
#     "media": {
#       "r6": { "baseline": "tcStart" }
#     }
#   }
# }
#
# Precedence: media[id] > takes[take] > default.
#
# CLI overrides:
#   --baseline auto|current|tcStart|override     (default: auto = use project policy or fall back to "current")
#   --override-seconds FLOAT                     (when baseline=override at CLI level; interpreted as seconds relative to tcStart)
#   --audit-baselines                            (log baseline mismatches and flag when |mismatch| > threshold)
#   --threshold-seconds FLOAT                    (override policy default threshold for this run)
#
# Notes:
# - "override" baseline seconds are interpreted RELATIVE TO tcStart to form the baseline_start.
# - If preserve=true for a clip (from policy), we do not move it and do not ripple from it.

import argparse
import json
from fractions import Fraction
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple, Any, Optional

# ---------- Defaults (relative to the resolved working directory) ----------

DEFAULT_FCPXML = "Info.fcpxml"
DEFAULT_COUNTDOWN = "20_countdown_offsets.json"
DEFAULT_OUT_FCPXML = "26_adjusted.fcpxml"
DEFAULT_ADJUST_MAP = "25_adjustment_map.json"
DEFAULT_MC_REGISTRY = "25_multicam_registry.json"

# ---------- Project Working Directory Resolution ----------

def get_script_dir() -> Path:
    return Path(__file__).resolve().parent

def read_active_project_name(projects_dir: Path) -> str:
    ap = projects_dir / "active_project.txt"
    if not ap.exists():
        raise RuntimeError(f"Active project file not found: {ap}")
    name = ap.read_text(encoding="utf-8").strip().strip("'\"")
    if not name:
        raise RuntimeError(f"Active project name is empty in: {ap}")
    return name

def read_project_config(projects_dir: Path, project_name: str) -> Dict[str, Any]:
    cfg = projects_dir / f"{project_name}.json"
    if not cfg.exists():
        raise RuntimeError(f"Project config not found: {cfg}")
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Failed to parse project config {cfg}: {e}") from e
    for k in ("base_project_directory", "workspace_subdir"):
        if k not in data or not str(data[k]).strip():
            raise RuntimeError(f"Missing '{k}' in project config: {cfg}")
    return data

def resolve_working_dir() -> Path:
    script_dir = get_script_dir()
    projects_dir = script_dir / "projects"
    if not projects_dir.exists():
        raise RuntimeError(f"Projects directory not found: {projects_dir}")
    project_name = read_active_project_name(projects_dir)
    cfg = read_project_config(projects_dir, project_name)
    base = Path(cfg["base_project_directory"])
    sub = Path(cfg["workspace_subdir"])
    wd = (base / sub).resolve()
    return wd

def resolve_cli_path(arg_value: Optional[str], working_dir: Path, default_name: str) -> Path:
    if not arg_value:
        return (working_dir / default_name).resolve()
    p = Path(arg_value)
    return p if p.is_absolute() else (working_dir / p).resolve()

# ---------- XML / Timebase Utilities ----------

def strip_namespaces(root: ET.Element) -> None:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if s.endswith("s"):
        body = s[:-1]
        if "/" in body:
            n, d = body.split("/", 1)
            return Fraction(int(n), int(d))
        return Fraction(body)
    return Fraction(s)

def frames_to_fcpx(frames: int, tb_num: int, tb_den: int) -> str:
    frac = Fraction(frames * tb_den, tb_num)
    return f"{frac.numerator}/{frac.denominator}s"

def seconds_to_frames(sec: float, tb_num: int, tb_den: int) -> int:
    frac = Fraction(sec).limit_denominator(10_000_000) * Fraction(tb_num, tb_den)
    return int(round(frac.numerator / frac.denominator))

def frac_to_frames(frac_s: Fraction, tb_num: int, tb_den: int) -> int:
    f = frac_s * Fraction(tb_num, tb_den)
    return int(round(f.numerator / f.denominator))

def find_sequence_and_timebase(root: ET.Element) -> Tuple[ET.Element, int, int]:
    seq = root.find(".//library/event/project/sequence")
    if seq is None:
        seq = root.find(".//sequence")
    if seq is None:
        raise RuntimeError("No <sequence> found in FCPXML")
    fmt_id = seq.get("format", "")
    if fmt_id:
        res = root.find(f".//resources/format[@id='{fmt_id}']")
    else:
        res = root.find(".//resources/format")
    if res is None:
        raise RuntimeError("No <format> found in <resources>")
    fd = res.get("frameDuration")
    if not fd:
        raise RuntimeError("Format missing frameDuration")
    frame_duration = parse_fcpx_frac(fd)  # seconds per frame
    return seq, frame_duration.denominator, frame_duration.numerator  # (tb_num, tb_den)

# ---------- Generic Token Parsing ----------

def extract_last_two_digit_token(name: str) -> str:
    if not name:
        return ""
    tokens = name.replace("_", " ").split()
    nums = [t for t in tokens if t.isdigit() and 1 <= len(t) <= 2]
    return nums[-1].zfill(2) if nums else ""

# ---------- Multicam Registry (Generic, External JSON) ----------

def gather_multicam_registry(root: ET.Element, tb_num: int, tb_den: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"multicams": []}
    for media in root.findall(".//resources/media"):
        mc = media.find("multicam")
        if mc is None:
            continue
        media_id = media.get("id", "") or ""
        media_name = media.get("name", "") or ""
        take = extract_last_two_digit_token(media_name)
        tc_start = mc.get("tcStart", "") or ""
        fmt = mc.get("format", "") or ""
        try:
            tc_frames = frac_to_frames(parse_fcpx_frac(tc_start), tb_num, tb_den) if tc_start else 0
        except Exception:
            tc_frames = 0
        entry = {
            "media_id": media_id,
            "media_name": media_name,
            "take": take,
            "tcStart_fcpx": tc_start,
            "tcStart_frames_seq": int(tc_frames),
            "format_ref": fmt,
            "angles": []
        }
        for angle in mc.findall("mc-angle"):
            angle_name = angle.get("name", "") or ""
            angle_id = angle.get("angleID", "") or ""
            for ac in angle.findall("asset-clip"):
                asset_ref = ac.get("ref", "") or ""
                asset_name = ac.get("name", "") or ""
                start_fcpx = ac.get("start", "0/1s")
                dur_fcpx = ac.get("duration", "0/1s")
                off_fcpx = ac.get("offset", "0/1s")
                try:
                    start_frames = frac_to_frames(parse_fcpx_frac(start_fcpx), tb_num, tb_den)
                except Exception:
                    start_frames = 0
                try:
                    dur_frames = frac_to_frames(parse_fcpx_frac(dur_fcpx), tb_num, tb_den)
                except Exception:
                    dur_frames = 0
                try:
                    off_frames = frac_to_frames(parse_fcpx_frac(off_fcpx), tb_num, tb_den)
                except Exception:
                    off_frames = 0
                entry["angles"].append({
                    "angle_name": angle_name,
                    "angle_id": angle_id,
                    "asset_ref": asset_ref,
                    "asset_name": asset_name,
                    "start_fcpx": start_fcpx,
                    "start_frames_seq": int(start_frames),
                    "duration_fcpx": dur_fcpx,
                    "duration_frames_seq": int(dur_frames),
                    "offset_fcpx": off_fcpx,
                    "offset_frames_seq": int(off_frames),
                })
        out["multicams"].append(entry)
    return out

# ---------- Step 20 Offsets Loader with Source Selection ----------

def _to_float_safe(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None

def build_take_to_seconds(countdown_list: List[Dict[str, Any]], source: str) -> Dict[str, float]:
    m: Dict[str, float] = {}
    for entry in countdown_list:
        take = str(entry.get("take_number", "")).strip()
        if not take:
            continue
        take = take.zfill(2)
        two = _to_float_safe(entry.get("two_end_seconds"))
        first = _to_float_safe(entry.get("first_content_seconds"))
        proposed = _to_float_safe(entry.get("proposed_start_seconds"))
        val: Optional[float] = None
        if source == "proposed":
            val = proposed
        elif source == "first":
            val = first
        elif source == "two":
            val = two
        elif source == "midpoint":
            if two is not None and first is not None:
                val = two + (first - two) / 2.0
        if val is None:
            continue
        m[take] = float(val)
    return m

# ---------- Policy Helpers (Baseline / Preserve / Threshold) ----------

def load_policy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    pol = cfg.get("policy") or {}
    default = pol.get("default") or {}
    takes = pol.get("takes") or {}
    media = pol.get("media") or {}
    out = {
        "default": {
            "baseline": str(default.get("baseline", "current")),
            "threshold_seconds": float(default.get("threshold_seconds", 0.5)),
        },
        "takes": takes,
        "media": media,
    }
    return out

def policy_for_clip(policy: Dict[str, Any], take: str, media_id: str) -> Dict[str, Any]:
    # Precedence: media[id] > takes[take] > default
    if media_id and media_id in policy.get("media", {}):
        p = policy["media"][media_id]
        return {
            "baseline": str(p.get("baseline", policy["default"]["baseline"])),
            "starting_override_seconds": p.get("starting_override_seconds"),
            "preserve": bool(p.get("preserve", False)),
        }
    if take and take in policy.get("takes", {}):
        p = policy["takes"][take]
        return {
            "baseline": str(p.get("baseline", policy["default"]["baseline"])),
            "starting_override_seconds": p.get("starting_override_seconds"),
            "preserve": bool(p.get("preserve", False)),
        }
    return {
        "baseline": str(policy["default"]["baseline"]),
        "starting_override_seconds": None,
        "preserve": False,
    }

# ---------- Core Rebaseline Logic ----------

def passthrough_copy(in_path: Path, out_path: Path, adjust_map_path: Path) -> None:
    tree = ET.parse(in_path)
    root = tree.getroot()
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    adjust_map_path.parent.mkdir(parents=True, exist_ok=True)
    with adjust_map_path.open("w", encoding="utf-8") as f:
        json.dump({"takes": 0, "notes": "no trims applied"}, f, indent=2)

def rebaseline_fcpxml(
    in_fcpxml: Path,
    countdown_json: Path,
    out_fcpxml: Path,
    adjust_map_path: Path,
    mc_registry_path: Path,
    source: str,
    global_nudge_sec: float,
    do_contiguity: bool,
    cli_baseline: str,
    cli_override_seconds: Optional[float],
    audit_baselines: bool,
    audit_threshold_seconds: float,
) -> Tuple[int, List[Dict]]:
    if not in_fcpxml.exists():
        raise SystemExit(f"Input FCPXML not found: {in_fcpxml}")

    # Load countdown list if present
    countdown_list: List[Dict[str, Any]] = []
    if countdown_json.exists():
        with countdown_json.open("r", encoding="utf-8") as f:
            countdown_list = json.load(f)

    # Parse XML, normalize, and get timebase
    proj_tree = ET.parse(in_fcpxml)
    root = proj_tree.getroot()
    strip_namespaces(root)
    sequence, tb_num, tb_den = find_sequence_and_timebase(root)

    # Multicam registry (always emit)
    mc_registry = gather_multicam_registry(root, tb_num, tb_den)
    mc_registry_path.parent.mkdir(parents=True, exist_ok=True)
    with mc_registry_path.open("w", encoding="utf-8") as f:
        json.dump(mc_registry, f, indent=2)

    # Build take -> seconds map based on requested source
    take_to_seconds = build_take_to_seconds(countdown_list, source)

    # If no usable trims, passthrough
    if not take_to_seconds:
        passthrough_copy(in_fcpxml, out_fcpxml, adjust_map_path)
        return (0, [])

    # Build mappings
    # media id -> take (from media name, last 1-2 digit token)
    mid_to_take: Dict[str, str] = {}
    # media id -> tcStart frames (for baseline tcStart anchoring)
    mid_to_tcstart_frames: Dict[str, int] = {}

    for media in root.findall(".//resources/media"):
        mid = media.get("id", "") or ""
        name = media.get("name", "") or ""
        if mid:
            t = extract_last_two_digit_token(name)
            if t:
                mid_to_take[mid] = t
        mc = media.find("multicam")
        if mc is not None:
            tc = mc.get("tcStart", "")
            if tc:
                try:
                    mid_to_tcstart_frames[mid] = frac_to_frames(parse_fcpx_frac(tc), tb_num, tb_den)
                except Exception:
                    mid_to_tcstart_frames[mid] = 0

    # Load policy from project JSON (sibling to active_project.txt)
    # Resolve the project JSON used to compute working dir
    try:
        script_dir = get_script_dir()
        projects_dir = script_dir / "projects"
        project_name = read_active_project_name(projects_dir)
        project_cfg = read_project_config(projects_dir, project_name)
        policy = load_policy(project_cfg)
    except Exception:
        # If anything fails, fall back to minimal defaults
        policy = {"default": {"baseline": "current", "threshold_seconds": 0.5}, "takes": {}, "media": {}}

    # Collect spine items with initial offsets; maintain CURRENT offsets in el_to_offset
    spine = sequence.find("spine")
    if spine is None:
        raise RuntimeError("No <spine> in <sequence>")

    items: List[Tuple[int, ET.Element]] = []
    for el in list(spine):
        off_attr = el.get("offset")
        if off_attr is None:
            continue
        try:
            off_frames = frac_to_frames(parse_fcpx_frac(off_attr), tb_num, tb_den)
        except Exception:
            continue
        items.append((off_frames, el))
    items.sort(key=lambda t: t[0])

    el_to_offset: Dict[ET.Element, int] = {el: off for off, el in items}
    adjustments: List[Dict] = []

    # Apply trims with CURRENT cutoff ripple logic + hardened baseline selection
    for original_off, el in items:
        if el.tag != "mc-clip":
            continue

        ref = el.get("ref", "") or ""
        take = mid_to_take.get(ref, "") or extract_last_two_digit_token(el.get("name", "") or "")
        if not take or take not in take_to_seconds:
            continue

        # Determine baseline policy for this clip
        pol = policy_for_clip(policy, take, ref)
        baseline_choice = cli_baseline if cli_baseline != "auto" else pol.get("baseline", "current")
        preserve = bool(pol.get("preserve", False))

        # Compute current clip start/duration (sequence timebase)
        start_attr = el.get("start", "0/1s")
        dur_attr = el.get("duration", "0/1s")
        cur_start_frames = frac_to_frames(parse_fcpx_frac(start_attr), tb_num, tb_den)
        cur_dur_frames = frac_to_frames(parse_fcpx_frac(dur_attr), tb_num, tb_den)

        # Get tcStart frames (0 if unknown)
        tc_frames = mid_to_tcstart_frames.get(ref, 0)

        # Baseline starting point (sequence frames)
        if baseline_choice == "current":
            baseline_start_frames = cur_start_frames
        elif baseline_choice == "tcStart":
            baseline_start_frames = tc_frames
        elif baseline_choice == "override":
            # CLI override takes precedence; else per-clip "starting_override_seconds"; else fallback to tcStart
            sec = cli_override_seconds if cli_override_seconds is not None else pol.get("starting_override_seconds")
            if sec is None:
                baseline_start_frames = tc_frames
            else:
                baseline_start_frames = tc_frames + seconds_to_frames(float(sec), tb_num, tb_den)
        else:
            # Unknown option falls back to "current"
            baseline_start_frames = cur_start_frames

        # Audit baseline mismatch (current vs tcStart) if requested
        baseline_mismatch_frames = cur_start_frames - tc_frames
        baseline_mismatch_seconds = baseline_mismatch_frames / (tb_num / tb_den) if (tb_num / tb_den) != 0 else 0.0
        baseline_flagged = False
        threshold_seconds = audit_threshold_seconds if audit_threshold_seconds is not None else float(policy["default"].get("threshold_seconds", 0.5))
        if audit_baselines and abs(baseline_mismatch_seconds) > threshold_seconds:
            baseline_flagged = True

        # Skip movement if preserve=true
        if preserve:
            adjustments.append({
                "take": take,
                "media_ref": ref,
                "source": source,
                "preserve": True,
                "baseline": baseline_choice,
                "baseline_mismatch_seconds": round(baseline_mismatch_seconds, 6),
                "baseline_flagged": baseline_flagged,
                "note": "preserve=true; clip not moved"
            })
            # Do NOT ripple from preserved clips
            continue

        # Build seconds from Step 20 + global nudge
        base_sec = float(take_to_seconds[take])
        applied_trim_sec = base_sec + float(global_nudge_sec)
        applied_trim_frames = seconds_to_frames(applied_trim_sec, tb_num, tb_den)

        # Hardened: desired_start = baseline_start + applied_trim
        desired_start_frames = baseline_start_frames + applied_trim_frames

        # Delta relative to CURRENT start (idempotent)
        delta_frames = desired_start_frames - cur_start_frames

        # Apply to clip
        new_start_frames = cur_start_frames + delta_frames
        new_dur_frames = max(0, cur_dur_frames - delta_frames)  # negative delta increases duration
        el.set("start", frames_to_fcpx(new_start_frames, tb_num, tb_den))
        el.set("duration", frames_to_fcpx(new_dur_frames, tb_num, tb_den))

        # Ripple strictly to the right of the clip's CURRENT offset (prevents overtake)
        current_cutoff = el_to_offset.get(el, original_off)
        for other_el, other_off in list(el_to_offset.items()):
            if other_el is el:
                continue
            if other_off > current_cutoff:
                new_off = max(0, other_off - delta_frames)  # signed-safe
                other_el.set("offset", frames_to_fcpx(new_off, tb_num, tb_den))
                el_to_offset[other_el] = new_off

        adjustments.append({
            "take": take,
            "media_ref": ref,
            "source": source,
            "preserve": False,
            "baseline": baseline_choice,
            "baseline_start_frames": int(baseline_start_frames),
            "tcStart_frames": int(tc_frames),
            "current_start_frames_before": int(cur_start_frames),
            "desired_start_frames": int(desired_start_frames),
            "delta_frames": int(delta_frames),
            "duration_frames_before": int(cur_dur_frames),
            "duration_frames_after": int(new_dur_frames),
            "base_seconds_from_step20": round(base_sec, 6),
            "global_nudge_seconds": round(global_nudge_sec, 6),
            "applied_trim_seconds": round(applied_trim_sec, 6),
            "baseline_mismatch_seconds": round(baseline_mismatch_seconds, 6),
            "baseline_flagged": baseline_flagged,
            "clip_offset_frames_cutoff": int(current_cutoff),
        })

    # Optional contiguity pass: pack everything end-to-end by current offsets
    if do_contiguity:
        ordered = sorted([(off, el) for el, off in el_to_offset.items()], key=lambda t: t[0])
        prev_end = None
        for off_frames, el in ordered:
            if prev_end is not None and off_frames != prev_end:
                el.set("offset", frames_to_fcpx(prev_end, tb_num, tb_den))
                el_to_offset[el] = prev_end
                off_frames = prev_end
            dur_attr = el.get("duration")
            if dur_attr:
                try:
                    dur_frames = frac_to_frames(parse_fcpx_frac(dur_attr), tb_num, tb_den)
                except Exception:
                    dur_frames = 0
            else:
                dur_frames = 0
            prev_end = off_frames + dur_frames if prev_end is not None else off_frames + dur_frames

    # Write outputs
    out_fcpxml.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out_fcpxml, encoding="utf-8", xml_declaration=True)

    adjust_map_path.parent.mkdir(parents=True, exist_ok=True)
    with adjust_map_path.open("w", encoding="utf-8") as f:
        json.dump({"adjustments": adjustments}, f, indent=2)

    return (len(adjustments), adjustments)

# ---------- CLI ----------

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Step 25 — Rebaseline Offsets (hardened) using active project working directory."
    )
    ap.add_argument("--fcpxml", default=None, help=f"Input FCPXML (default: {DEFAULT_FCPXML} in working dir)")
    ap.add_argument("--countdown", default=None, help=f"Countdown offsets JSON (default: {DEFAULT_COUNTDOWN} in working dir)")
    ap.add_argument("--outfcpxml", default=None, help=f"Output adjusted FCPXML (default: {DEFAULT_OUT_FCPXML} in working dir)")
    ap.add_argument("--adjustmap", default=None, help=f"Adjustment map JSON (default: {DEFAULT_ADJUST_MAP} in working dir)")
    ap.add_argument("--mc-registry", default=None, help=f"Multicam registry JSON (default: {DEFAULT_MC_REGISTRY} in working dir)")
    ap.add_argument("--nudge", type=float, default=0.0, help="Signed global seconds to add to all trims (e.g., -0.25 or +0.25)")
    ap.add_argument("--source", choices=["proposed", "first", "two", "midpoint"], default="proposed",
                    help="Which Step 20 field to use (default: proposed)")
    ap.add_argument("--contiguity", action="store_true", help="After ripple, pack spine contiguously by current order")
    ap.add_argument("--print-paths", action="store_true", help="Print resolved working dir and file paths")

    # Hardening flags
    ap.add_argument("--baseline", choices=["auto", "current", "tcStart", "override"], default="auto",
                    help="Baseline for desired start: auto=project policy (default), or force one")
    ap.add_argument("--override-seconds", type=float, default=None,
                    help="When --baseline=override, seconds relative to tcStart to form baseline_start")
    ap.add_argument("--audit-baselines", action="store_true",
                    help="Log baseline mismatches (current_start - tcStart) and flag when beyond threshold")
    ap.add_argument("--threshold-seconds", type=float, default=None,
                    help="Override baseline audit threshold (seconds) for this run")
    return ap

def main() -> None:
    ap = build_arg_parser()
    args = ap.parse_args()

    try:
        working_dir = resolve_working_dir()
    except Exception as e:
        print(f"[!] Failed to resolve working directory from active project: {e}", file=sys.stderr)
        sys.exit(1)

    in_fcpxml = resolve_cli_path(args.fcpxml, working_dir, DEFAULT_FCPXML)
    countdown_json = resolve_cli_path(args.countdown, working_dir, DEFAULT_COUNTDOWN)
    out_fcpxml = resolve_cli_path(args.outfcpxml, working_dir, DEFAULT_OUT_FCPXML)
    adjust_map_path = resolve_cli_path(args.adjustmap, working_dir, DEFAULT_ADJUST_MAP)
    mc_registry_path = resolve_cli_path(args.mc_registry, working_dir, DEFAULT_MC_REGISTRY)

    if args.print_paths:
        print(f"[i] Working dir:        {working_dir}")
        print(f"[i] Input FCPXML:       {in_fcpxml}")
        print(f"[i] Countdown JSON:     {countdown_json}")
        print(f"[i] Output FCPXML:      {out_fcpxml}")
        print(f"[i] Adjustment Map:     {adjust_map_path}")
        print(f"[i] Multicam Registry:  {mc_registry_path}")
        print(f"[i] Source:             {args.source}")
        print(f"[i] Global Nudge (s):   {args.nudge:+.3f}")
        print(f"[i] Baseline Mode:      {args.baseline}")
        if args.baseline == "override":
            print(f"[i] Override Seconds:   {args.override_seconds}")
        print(f"[i] Audit Baselines:    {args.audit_baselines}")
        if args.threshold_seconds is not None:
            print(f"[i] Audit Threshold:    {args.threshold_seconds:.3f}s")
        print(f"[i] Contiguity Pass:    {args.contiguity}")

    try:
        count, _ = rebaseline_fcpxml(
            in_fcpxml=in_fcpxml,
            countdown_json=countdown_json,
            out_fcpxml=out_fcpxml,
            adjust_map_path=adjust_map_path,
            mc_registry_path=mc_registry_path,
            source=args.source,
            global_nudge_sec=float(args.nudge),
            do_contiguity=bool(args.contiguity),
            cli_baseline=args.baseline,
            cli_override_seconds=args.override_seconds,
            audit_baselines=bool(args.audit_baselines),
            audit_threshold_seconds=float(args.threshold_seconds) if args.threshold_seconds is not None else None,
        )
        print(f"[+] Wrote adjusted FCPXML: {out_fcpxml}")
        print(f"[+] Wrote adjustment map:  {adjust_map_path}")
        print(f"[+] Wrote multicam map:    {mc_registry_path}")
        print(f"[i] Adjustments applied:   {count}")
    except Exception as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
