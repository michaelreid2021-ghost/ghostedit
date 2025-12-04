#!/usr/bin/env python3
# 117_insert_broll_asset_clip.py
# v2.5.0 - 2025-10-07
# - UPDATE: Adjust input nominal timestamps and durations to real-time values for correct placement in DaVinci Resolve.
#           Input timestamps (from JSON) are treated as nominal (24 fps equivalent) seconds.
#           Converted to real-time (23.976 fps) seconds using scale factor 1001/1000.
#           This ensures Resolve imports and displays the b-roll at the intended nominal positions (e.g., 3600.833s nominal).
# v2.4.0 (FINAL) - 2025-10-07
# Inserts a single b-roll <asset-clip> into an FCPXML on lane="1".
# Key behaviors:
# - Treats placeholder timestamps from filenames (e.g., 3600.83s) as ABSOLUTE sequence time.
# - The sequence's tcStart attribute is for display only and correctly ignored by placement logic.
# - Associates b-roll with the temporally closest mc-clip on the timeline.
# - Computes the asset-clip's offset in the mc's source timecode domain using a precise translation formula.
# - Quantizes all generated offset and duration values to the nearest 23.976 fps frame boundary.
# - Allows b-roll clips to extend past their containing mc-clip boundary.
# - Extends <sequence duration> when the inserted overlay runs past the current sequence end.

import os
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from xml.dom import minidom
from typing import Optional, Tuple, List

# ---------------------------
# Time Utilities
# ---------------------------

def parse_rational_seconds(s: str) -> Fraction:
    """
    Parse strings like '641481841/12000s' into Fraction seconds.
    """
    if not s or not s.endswith('s'):
        raise ValueError(f"Bad time literal: {s}")
    core = s[:-1]
    if '/' in core:
        num_str, den_str = core.split('/')
        return Fraction(int(num_str), int(den_str))
    return Fraction(int(core), 1)

def sec_to_23976_literal(sec: float) -> str:
    """
    Rounds seconds to the nearest 23.976 frame (1001/24000s) and returns
    an FCPXML-compliant rational time string. This prevents rounding drift.
    """
    frames = (float(sec) * 24000 / 1001)
    return f"{frames * 1001}/24000s"

def timebase_from_sequence(resources_root: ET.Element, seq_elem: ET.Element) -> Tuple[int, int]:
    """
    Return (tb_num, tb_den) where 1 frame = tb_den/tb_num seconds.
    """
    fmt_id = seq_elem.get('format')
    if not fmt_id:
        raise RuntimeError("Sequence missing 'format' attribute.")
    fmt = resources_root.find(f".//format[@id='{fmt_id}']")
    if fmt is None:
        raise RuntimeError(f"Could not find <format id='{fmt_id}'> in resources.")
    fd = fmt.get('frameDuration')
    if not fd:
        raise RuntimeError("Format missing 'frameDuration'.")
    frac = parse_rational_seconds(fd)
    return (frac.denominator, frac.numerator)

def seconds_to_frames(sec: float, tb_num: int, tb_den: int) -> int:
    return int(round(Fraction(sec).limit_denominator(10_000_000) * Fraction(tb_num, tb_den)))

def frames_to_literal(frames: int, tb_num: int, tb_den: int) -> str:
    frac = Fraction(frames * tb_den, tb_num)
    return f"{frac.numerator}/{frac.denominator}s"

# ---------------------------
# XML Utilities
# ---------------------------

def find_next_r_id(root: ET.Element) -> int:
    used = set()
    for el in root.iter():
        rid = el.get('id')
        if rid and rid.startswith('r'):
            try:
                used.add(int(rid[1:]))
            except ValueError:
                pass
        ref = el.get('ref')
        if ref and ref.startswith('r'):
            try:
                used.add(int(ref[1:]))
            except ValueError:
                pass
    i = 1
    while i in used:
        i += 1
    return i

def get_resources(root: ET.Element) -> ET.Element:
    res = root.find('.//resources')
    if res is None:
        raise RuntimeError("No <resources> found.")
    return res

def get_sequence(root: ET.Element) -> ET.Element:
    seq = root.find('.//library/event/project/sequence')
    if seq is None:
        raise RuntimeError("No <sequence> found.")
    return seq

def pretty_write_xml(tree: ET.ElementTree, out_path: Path) -> None:
    rough = ET.tostring(tree.getroot(), encoding="utf-8")
    dom = minidom.parseString(rough)
    pretty = dom.toprettyxml(indent="    ", encoding="utf-8")
    text_lines = [line for line in pretty.decode("utf-8").split("\n") if line.strip()]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(text_lines))

# ---------------------------
# Resources: Formats & Assets
# ---------------------------

def ensure_format_1080p30(resources: ET.Element) -> str:
    for fmt in resources.findall("format"):
        fd = fmt.get("frameDuration")
        if not fd:
            continue
        sec = parse_rational_seconds(fd)
        fps = float(Fraction(1, 1) / sec)
        if abs(fps - 30.0) < 0.01 and fmt.get("width") == "1920" and fmt.get("height") == "1080":
            return fmt.get("id")
    new_id = f"r{find_next_r_id(resources)}"
    ET.SubElement(resources, "format", {"id": new_id, "name": "FFVideoFormat1080p30", "width": "1920", "height": "1080", "frameDuration": "1/30s"})
    return new_id

def find_existing_asset_by_src(resources: ET.Element, mov_uri: str) -> Optional[ET.Element]:
    for a in resources.findall("asset"):
        mr = a.find("media-rep")
        if mr is not None and mr.get("src") == mov_uri:
            return a
    return None

def ensure_asset(resources: ET.Element, mov_path: Path, asset_format_id: str, nominal_duration_sec: float) -> str:
    mov_uri = mov_path.as_uri().replace('%5C', '/')
    existing = find_existing_asset_by_src(resources, mov_uri)
    if existing is not None:
        if existing.get("format") != asset_format_id:
            existing.set("format", asset_format_id)
        return existing.get("id")
    new_id = f"r{find_next_r_id(resources)}"
    asset_el = ET.SubElement(resources, "asset", {"id": new_id, "name": mov_path.name, "hasVideo": "1", "format": asset_format_id, "duration": sec_to_23976_literal(max(nominal_duration_sec, 0.5)), "start": "0/1s"})
    ET.SubElement(asset_el, "media-rep", {"kind": "original-media", "src": mov_uri})
    return new_id

# ---------------------------
# mc-clip Containment & Clip Creation
# ---------------------------

def pick_closest_mc(spine: ET.Element, abs_start_sec: float) -> Optional[ET.Element]:
    """
    Finds the mc-clip whose start time is mathematically closest to the target b-roll time.
    This is more resilient than a strict containment check.
    """
    all_mc_clips = spine.findall("mc-clip")
    if not all_mc_clips:
        return None
    closest_clip = None
    min_distance = float('inf')
    for mc in all_mc_clips:
        offset_str = mc.get("offset")
        if not offset_str:
            continue
        offset_sec = float(parse_rational_seconds(offset_str))
        distance = abs(offset_sec - abs_start_sec)
        if distance < min_distance:
            min_distance = distance
            closest_clip = mc
    return closest_clip

def append_asset_clip(parent_el: ET.Element, asset_id: str, asset_name: str, format_id: str, lane: str, offset_seconds_literal: str, duration_seconds_literal: str) -> ET.Element:
    clip = ET.Element("asset-clip", {"lane": lane, "name": asset_name, "ref": asset_id, "tcFormat": "NDF", "offset": offset_seconds_literal, "format": format_id, "enabled": "1", "duration": duration_seconds_literal, "start": "0/1s"})
    clip.append(ET.Element("adjust-transform", {"position": "0 0", "scale": "1 1", "anchor": "0 0"}))
    parent_el.append(clip)
    return clip

# ---------------------------
# Core Insertion
# ---------------------------

def insert_broll_asset_clip(fcpxml_path: str, out_path: str, base_dir: str, mov_path: str, start_seconds: str, duration_seconds: str) -> None:
    fcpxml_path = Path(fcpxml_path)
    out_path = Path(out_path)
    mov_path = Path(mov_path)

    tree = ET.parse(fcpxml_path)
    root = tree.getroot()

    resources = get_resources(root)
    seq = get_sequence(root)
    spine = seq.find("spine")
    if spine is None:
        raise RuntimeError("No <spine> found under <sequence>.")

    tb_num, tb_den = timebase_from_sequence(resources, seq)

    # Input timestamps and durations are nominal (24 fps display timecode).
    # Convert to real-time (23.976 fps) for XML offsets using 1001/1000 scale.
    scale_factor = 1001 / 1000
    start_sec_nominal = float(start_seconds)
    duration_sec_nominal = float(duration_seconds)
    start_sec_real = start_sec_nominal * scale_factor
    duration_sec_real = duration_sec_nominal * scale_factor

    # Use absolute time directly. Clip offsets on the spine are in the same coordinate system.
    # The sequence's tcStart is for display purposes and not needed for this calculation.
    abs_start_sec = start_sec_real
    abs_end_sec = start_sec_real + duration_sec_real
    
    fmt30_id = ensure_format_1080p30(resources)
    asset_id = ensure_asset(resources, mov_path, fmt30_id, nominal_duration_sec=duration_sec_real)

    target_mc = pick_closest_mc(spine, abs_start_sec)

    if target_mc is None:
        raise RuntimeError(f"B-roll clip '{mov_path.name}' at {abs_start_sec:.3f}s could not be placed because no mc-clips were found on the timeline.")

    mc_off_s = float(parse_rational_seconds(target_mc.get("offset")))
    mc_tc_start_s = float(parse_rational_seconds(target_mc.get("start", "0/1s")))

    # The correct, final translation formula
    delta_s = abs_start_sec - mc_off_s
    asset_tc_offset_s = mc_tc_start_s + delta_s

    append_asset_clip(
        parent_el=target_mc,
        asset_id=asset_id,
        asset_name=mov_path.name,
        format_id=fmt30_id,
        lane="1",
        offset_seconds_literal=sec_to_23976_literal(asset_tc_offset_s),
        duration_seconds_literal=sec_to_23976_literal(duration_sec_real),
    )

    # Extend sequence duration if needed
    seq_duration_s = float(parse_rational_seconds(seq.get("duration", "0/1s")))
    if abs_end_sec > seq_duration_s:
        # Duration is relative to the sequence's internal zero, so abs_end_sec is the correct value.
        new_dur_frames = seconds_to_frames(abs_end_sec, tb_num, tb_den)
        seq.set("duration", frames_to_literal(new_dur_frames, tb_num, tb_den))

    pretty_write_xml(tree, out_path)

    print(f"-> Inserting b-roll: {mov_path.name}")
    print(f"   abs_time (nominal)={start_sec_nominal:.3f}s, duration (nominal)={duration_sec_nominal:.3f}s")
    print(f"   abs_time (real)={abs_start_sec:.3f}s, duration (real)={duration_sec_real:.3f}s")
    print(f"   Placement: inside closest mc-clip '{target_mc.get('name')}'")
    print(f"   Output: {out_path}")

# ---------------------------
# CLI
# ---------------------------

if __name__ == "__main__":
    if len(sys.argv) != 7:
        print("Usage: 117_insert_broll_asset_clip.py <fcpxml> <out> <base_dir> <mov_path> <start_seconds> <duration_seconds>")
        sys.exit(1)
    _, fcpxml_path, out_path, base_dir, mov_path, start_s, dur_s = sys.argv
    insert_broll_asset_clip(fcpxml_path, out_path, base_dir, mov_path, start_s, dur_s)