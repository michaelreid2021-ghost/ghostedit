#!/usr/bin/env python3
# 106_lock_audio_angle.py
# Version: 1.2.0
# Last Modified: 2025-09-04
# Purpose: Lock the audio source for all multicam clips in an FCPXML to a specified angle.
#          Robustly resolves input/output paths so a directory isn't passed where a file is expected.

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional, List
from project_paths import add_base_arg, from_args

def resolve_input_fcpxml(base: Path, user_input: Optional[Path]) -> Optional[Path]:
    """Resolve the FCPXML file to read."""
    if user_input:
        p = Path(user_input)
        if p.is_file() and p.suffix.lower() in {".fcpxml", ".xml"}:
            return p
        return None
    if base.is_file() and base.suffix.lower() in {".fcpxml", ".xml"}:
        return base
    # Prefer non-recursive in base, then ai_workspace, then recursive fallback
    search_roots: List[Path] = [base, base / "ai_workspace"]
    for root in search_roots:
        if root.exists():
            candidates = sorted(root.glob("*.fcpxml"), key=lambda x: x.stat().st_mtime, reverse=True)
            if candidates:
                return candidates[0]
    # Recursive fallback
    candidates = sorted(base.rglob("*.fcpxml"), key=lambda x: x.stat().st_mtime, reverse=True) if base.exists() else []
    if candidates:
        return candidates[0]
    return None

def ensure_output_path(base: Path, user_output: Optional[Path]) -> Path:
    """Determine where to write the modified FCPXML."""
    if user_output:
        out = Path(user_output)
    else:
        out = base / "ai_workspace" / "106_audio_locked.fcpxml" if base.is_dir() else Path.cwd() / "106_audio_locked.fcpxml"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out

def lock_multicam_audio_angle(fcpxml_path: Path, output_path: Path, target_angle_name: str) -> None:
    """Parse FCPXML, lock mc-clip audio to matching angle name, write output."""
    print(f"[+] Processing file: {fcpxml_path}")
    print(f"    Locking audio to angles containing the name: '{target_angle_name}'")

    try:
        tree = ET.parse(str(fcpxml_path))
        root = tree.getroot()
    except FileNotFoundError:
        print(f"[!] FATAL ERROR: Input file not found: {fcpxml_path}")
        return
    except PermissionError as e:
        print(f"[!] FATAL ERROR: Permission denied opening '{fcpxml_path}': {e}")
        return
    except ET.ParseError as e:
        print(f"[!] FATAL ERROR: Could not parse XML file. Error: {e}")
        return

    resources = root.find(".//resources")
    if resources is None:
        print("[!] FATAL ERROR: Could not find <resources> in the FCPXML.")
        return

    audio_angle_id_cache: Dict[str, str] = {}
    clips_modified = 0

    # Iterate all multicam clips in the document (not just top-level spine)
    for mc_clip in root.findall(".//mc-clip"):
        ref_id = mc_clip.get("ref")
        if not ref_id:
            continue

        if ref_id not in audio_angle_id_cache:
            media = resources.find(f".//media[@id='{ref_id}']")
            if media is None:
                audio_angle_id_cache[ref_id] = "not-found"
            else:
                target_lower = target_angle_name.lower()
                found_angle_id: Optional[str] = None
                for angle in media.findall(".//mc-angle"):
                    angle_name = angle.get("name", "")
                    if target_lower in angle_name.lower():
                        found_angle_id = angle.get("angleID")
                        if found_angle_id:
                            break
                audio_angle_id_cache[ref_id] = found_angle_id or "not-found"

        angle_id = audio_angle_id_cache.get(ref_id)
        if angle_id and angle_id != "not-found":
            mc_source = mc_clip.find("mc-source")
            if mc_source is None:
                mc_source = ET.SubElement(mc_clip, "mc-source")
            mc_source.set("srcAudio", angle_id)
            clips_modified += 1

    if clips_modified > 0:
        try:
            tree.write(str(output_path), encoding="utf-8", xml_declaration=True)
            print(f"\n[+] Success! Modified {clips_modified} multicam clips.")
            print(f"    Output saved to: {output_path}")
        except PermissionError as e:
            print(f"[!] FATAL ERROR: Permission denied writing '{output_path}': {e}")
    else:
        print("\n[i] No multicam clips were found or modified.")

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lock the audio source of all multicam clips in an FCPXML file to a specific angle.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    add_base_arg(parser)
    parser.add_argument("--angle-name", required=True, help="Angle name (substring match), e.g., 'Angle 1'.")
    parser.add_argument("--input", type=Path, default=None, help="Path to input .fcpxml. Defaults: detect under --base.")
    parser.add_argument("--output", type=Path, default=None, help="Path to write modified .fcpxml. Defaults: <base>/ai_workspace/106_audio_locked.fcpxml")
    args = parser.parse_args()

    p = from_args(args)
    base_path = Path(p.base)

    input_fcpxml = resolve_input_fcpxml(base_path, args.input)
    if not input_fcpxml:
        print(f"[!] FATAL ERROR: Could not resolve an input FCPXML. Provide --input or place a .fcpxml under '{base_path}'.")
        return

    output_fcpxml = ensure_output_path(base_path, args.output)
    lock_multicam_audio_angle(input_fcpxml, output_fcpxml, args.angle_name)

if __name__ == "__main__":
    main()

# Changelog:
# 1.2.0 (2025-09-04): Fix path resolution so a directory isn't parsed as XML; add --input/--output; case-insensitive angle match; create <mc-source> if missing.
# 1.1.0 (2025-08-20): Initial standalone with project_paths integration and angle locking logic.
