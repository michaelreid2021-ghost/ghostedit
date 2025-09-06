#!/usr/bin/env python3
# 25_rebaseline_offsets.py
# Description: Applies start-time trims to multicam clips from a countdown offsets file
# and ripples the changes through the entire timeline spine.
# This version uses a clear, procedural ripple calculation for improved clarity and accuracy.

import argparse
import json
from fractions import Fraction
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple, Any, Optional

# --- Configuration ---
DEFAULT_FCPXML = "info.fcpxml"
DEFAULT_COUNTDOWN = "20_countdown_offsets.json"
DEFAULT_OUT_FCPXML = "26_adjusted.fcpxml"
DEFAULT_ADJUST_MAP = "25_adjustment_map.json"
DEFAULT_MC_REGISTRY = "25_multicam_registry.json"

# --- Utility Functions (Time, XML, Parsing) ---

def parse_fcpx_frac(s: str) -> Fraction:
    """Parses FCPXML's fractional time string (e.g., '1001/24000s') into a Fraction."""
    s = s.strip()
    if s.endswith("s"):
        body = s[:-1]
        if "/" in body:
            n, d = body.split("/", 1)
            return Fraction(int(n), int(d))
        return Fraction(body)
    return Fraction(s)

def frac_to_frames(frac_s: Fraction, tb_num: int, tb_den: int) -> int:
    """Converts a time in seconds (as a Fraction) to an integer number of frames."""
    f = frac_s * Fraction(tb_num, tb_den)
    return int(round(f.numerator / f.denominator))

def frames_to_fcpx(frames: int, tb_num: int, tb_den: int) -> str:
    """Converts an integer number of frames back to an FCPXML time string."""
    frac = Fraction(frames * tb_den, tb_num)
    return f"{frac.numerator}/{frac.denominator}s"

def seconds_to_frames(sec: float, tb_num: int, tb_den: int) -> int:
    """Converts a time in seconds (float) to an integer number of frames."""
    frac = Fraction(sec).limit_denominator(1_000_000) * Fraction(tb_num, tb_den)
    return int(round(frac.numerator / frac.denominator))

def find_sequence_and_timebase(root: ET.Element) -> Tuple[Optional[ET.Element], int, int]:
    """Finds the main sequence and extracts its timebase (numerator, denominator)."""
    seq = root.find(".//library/event/project/sequence")
    if seq is None:
        raise RuntimeError("Could not find <sequence> in the FCPXML file.")

    format_id = seq.get("format")
    if not format_id:
        raise RuntimeError("<sequence> is missing the 'format' attribute.")

    format_el = root.find(f".//resources/format[@id='{format_id}']")
    if format_el is None:
        raise RuntimeError(f"Could not find format with id '{format_id}' in <resources>.")

    fd_str = format_el.get("frameDuration")
    if not fd_str:
        raise RuntimeError(f"Format '{format_id}' is missing the 'frameDuration' attribute.")

    frame_duration = parse_fcpx_frac(fd_str)
    return seq, frame_duration.denominator, frame_duration.numerator

def extract_take_from_name(name: str) -> str:
    """Extracts the last 2-digit number from a clip name to use as the take number."""
    if not name:
        return ""
    # Look for numbers that are 1 or 2 digits long.
    tokens = name.replace("_", " ").replace(".", " ").split()
    nums = [t for t in tokens if t.isdigit() and 1 <= len(t) <= 2]
    return nums[-1].zfill(2) if nums else ""

# --- Core Logic ---

def process_timeline(
    fcpxml_path: Path,
    countdown_path: Path,
    output_fcpxml_path: Path,
    adjustment_map_path: Path,
    mc_registry_path: Path,
):
    """
    Main function to load, process, and save the timeline adjustments.
    """
    print(f"[i] Loading FCPXML: {fcpxml_path}")
    tree = ET.parse(fcpxml_path)
    root = tree.getroot()

    sequence, tb_num, tb_den = find_sequence_and_timebase(root)
    spine = sequence.find("spine")
    if spine is None:
        raise RuntimeError("Could not find <spine> within the <sequence>.")

    print(f"[i] Loading countdown offsets: {countdown_path}")
    with countdown_path.open("r", encoding="utf-8") as f:
        countdown_data = json.load(f)

    # Create a simple lookup map: take -> seconds to trim
    trims_to_apply = {
        str(item.get("take_number", "")).zfill(2): float(item.get("proposed_start_seconds", 0.0))
        for item in countdown_data
        if "take_number" in item and "proposed_start_seconds" in item
    }
    print(f"[i] Found {len(trims_to_apply)} takes with trim data in countdown file.")

    # Get all clips from the spine and sort them by their timeline offset
    sorted_clips = []
    for clip in spine:
        offset_str = clip.get("offset")
        if offset_str:
            offset_frames = frac_to_frames(parse_fcpx_frac(offset_str), tb_num, tb_den)
            sorted_clips.append((offset_frames, clip))
    sorted_clips.sort(key=lambda x: x[0])

    # --- Procedural Ripple Logic ---
    total_ripple_frames = 0
    adjustment_log = []
    print("\n[+] Starting timeline processing and ripple calculation...")

    for original_offset_frames, clip in sorted_clips:
        clip_name = clip.get("name", "Untitled Clip")
        media_ref = clip.get("ref", "")
        
        # This is the key: Apply the accumulated ripple from all PREVIOUS clips
        rippled_offset_frames = original_offset_frames + total_ripple_frames
        clip.set("offset", frames_to_fcpx(rippled_offset_frames, tb_num, tb_den))

        log_entry = {
            "clip_name": clip_name,
            "media_ref": media_ref,
            "original_offset_frames": original_offset_frames,
            "cumulative_ripple_frames": total_ripple_frames,
            "new_offset_frames": rippled_offset_frames,
            "trim_applied_frames": 0,
        }

        # Only apply trims to multicam clips that are in our list
        if clip.tag == "mc-clip":
            take = extract_take_from_name(clip_name)
            if take in trims_to_apply:
                seconds_to_trim = trims_to_apply[take]
                frames_to_trim = seconds_to_frames(seconds_to_trim, tb_num, tb_den)

                # Get current start and duration
                start_str = clip.get("start", "0/1s")
                duration_str = clip.get("duration", "0/1s")
                current_start_frames = frac_to_frames(parse_fcpx_frac(start_str), tb_num, tb_den)
                current_duration_frames = frac_to_frames(parse_fcpx_frac(duration_str), tb_num, tb_den)

                # Calculate new start and duration
                new_start_frames = current_start_frames + frames_to_trim
                new_duration_frames = max(0, current_duration_frames - frames_to_trim)
                
                # Apply changes to the XML element
                clip.set("start", frames_to_fcpx(new_start_frames, tb_num, tb_den))
                clip.set("duration", frames_to_fcpx(new_duration_frames, tb_num, tb_den))

                # This is the second key: Add this clip's trim to the total ripple for all SUBSEQUENT clips
                total_ripple_frames -= frames_to_trim
                
                print(f"  - Adjusting Take {take} ('{clip_name}'):")
                print(f"    - Trimming {seconds_to_trim:.2f}s ({frames_to_trim} frames).")
                print(f"    - Total ripple is now {total_ripple_frames} frames.")

                # Update log entry
                log_entry["take"] = take
                log_entry["trim_applied_frames"] = frames_to_trim
                log_entry["duration_frames_before"] = current_duration_frames
                log_entry["duration_frames_after"] = new_duration_frames

        adjustment_log.append(log_entry)

    # --- Save Outputs ---
    print("\n[+] Timeline processing complete.")
    output_fcpxml_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_fcpxml_path, encoding="utf-8", xml_declaration=True)
    print(f"[+] Wrote adjusted FCPXML to: {output_fcpxml_path}")

    adjustment_map_path.parent.mkdir(parents=True, exist_ok=True)
    with adjustment_map_path.open("w", encoding="utf-8") as f:
        json.dump({"adjustments": adjustment_log}, f, indent=2)
    print(f"[+] Wrote detailed adjustment map to: {adjustment_map_path}")

    # For compatibility, we'll still generate the multicam registry from the original script
    generate_multicam_registry(root, tb_num, tb_den, mc_registry_path)
    print(f"[+] Wrote multicam registry to: {mc_registry_path}")

def generate_multicam_registry(root: ET.Element, tb_num: int, tb_den: int, out_path: Path):
    """Generates a JSON file inventory of all multicam clips in the FCPXML resources."""
    registry = {"multicams": []}
    for media in root.findall(".//resources/media"):
        mc = media.find("multicam")
        if mc is None:
            continue
        
        media_id = media.get("id", "")
        media_name = media.get("name", "")
        take = extract_take_from_name(media_name)
        tc_start = mc.get("tcStart", "0/1s")
        
        entry = {
            "media_id": media_id,
            "media_name": media_name,
            "take": take,
            "tcStart_fcpx": tc_start,
            "tcStart_frames_seq": frac_to_frames(parse_fcpx_frac(tc_start), tb_num, tb_den),
            "format_ref": mc.get("format", ""),
            "angles": []
        }
        
        for angle in mc.findall("mc-angle"):
            asset_clip = angle.find("asset-clip")
            if asset_clip is not None:
                entry["angles"].append({
                    "angle_name": angle.get("name", ""),
                    "angle_id": angle.get("angleID", ""),
                    "asset_ref": asset_clip.get("ref", ""),
                    "asset_name": asset_clip.get("name", ""),
                })
        registry["multicams"].append(entry)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

# --- Main Execution ---

def main():
    parser = argparse.ArgumentParser(
        description="Applies start-time trims to multicam clips and ripples the timeline."
    )
    parser.add_argument("--fcpxml", default=DEFAULT_FCPXML, help=f"Input FCPXML file. Default: {DEFAULT_FCPXML}")
    parser.add_argument("--countdown", default=DEFAULT_COUNTDOWN, help=f"Input countdown offsets JSON. Default: {DEFAULT_COUNTDOWN}")
    parser.add_argument("--outfcpxml", default=DEFAULT_OUT_FCPXML, help=f"Output adjusted FCPXML file. Default: {DEFAULT_OUT_FCPXML}")
    parser.add_argument("--adjustmap", default=DEFAULT_ADJUST_MAP, help=f"Output adjustment map JSON. Default: {DEFAULT_ADJUST_MAP}")
    parser.add_argument("--mcregistry", default=DEFAULT_MC_REGISTRY, help=f"Output multicam registry JSON. Default: {DEFAULT_MC_REGISTRY}")
    parser.add_argument("--base", help="Base directory for resolving relative paths. Defaults to current directory.")
    
    args = parser.parse_args()

    base_path = Path(args.base) if args.base else Path.cwd()

    try:
        process_timeline(
            fcpxml_path=base_path / args.fcpxml,
            countdown_path=base_path / args.countdown,
            output_fcpxml_path=base_path / args.outfcpxml,
            adjustment_map_path=base_path / args.adjustmap,
            mc_registry_path=base_path / args.mcregistry
        )
        print("\n[+] Success!")
    except Exception as e:
        print(f"\n[!] An error occurred: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()