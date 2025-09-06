#!/usr/bin/env python3
# 117_insert_broll_asset_clip.py
# v1.7.0 — 2025-08-27
# Inserts a single b-roll <asset-clip> into an FCPXML on lane="1".
# Key behaviors:
# - Treats placeholder timestamps as RELATIVE TO 01:00:00:00 (3600s) baseline unless an explicit <gap start="..."> exists.
# - Detects mc-clip containment by comparing absolute timeline seconds against each mc-clip's [offset, offset+duration).
# - If inside an mc-clip, computes the asset-clip's offset in the mc's TC domain (mc.start + delta).
# - If not inside any mc-clip, inserts directly under <spine> using timeline-domain offset seconds.
# - Ensures a 1080p30 <format> exists for b-roll assets and uses it for both <asset> and inserted <asset-clip>.
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

def frac_to_time_literal(sec: Fraction, denom: int = 24000) -> str:
    """
    Represent seconds as '<num>/<denoms>' (with integer num), using supplied denominator.
    Default denom=24000 aligns well with 23.976 timelines.
    """
    num = int(sec * denom)
    return f"{num}/{denom}s"

def float_to_time_literal(seconds: float, denom: int = 24000) -> str:
    return frac_to_time_literal(Fraction(seconds).limit_denominator(denom * 1000), denom)

def timebase_from_sequence(resources_root: ET.Element, seq_elem: ET.Element) -> Tuple[int, int]:
    """
    Return (tb_num, tb_den) where 1 frame = tb_den/tb_num seconds.
    For 23.976, frameDuration="1001/24000s" → tb_num=24000, tb_den=1001.
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
    frac = parse_rational_seconds(fd)  # seconds per frame (e.g., 1001/24000)
    return (frac.denominator, frac.numerator)  # (tb_num, tb_den)

def seconds_to_frames(sec: float, tb_num: int, tb_den: int) -> int:
    return int(round(Fraction(sec).limit_denominator(10_000_000) * Fraction(tb_num, tb_den)))

def frames_to_literal(frames: int, tb_num: int, tb_den: int) -> str:
    frac = Fraction(frames * tb_den, tb_num)
    return f"{frac.numerator}/{frac.denominator}s"

# ---------------------------
# XML Utilities
# ---------------------------

def find_next_r_id(root: ET.Element) -> int:
    """
    Find the next available integer for an 'r###' id across id/ref attributes.
    """
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

def detect_timeline_fps(root: ET.Element) -> float:
    """
    Derive sequence FPS from <format frameDuration>.
    """
    seq = get_sequence(root)
    resources = get_resources(root)
    tb_num, tb_den = timebase_from_sequence(resources, seq)
    return float(tb_num) / float(tb_den)

def detect_timeline_base_seconds(spine: ET.Element) -> float:
    """
    Determine the visible timeline's base (commonly 01:00:00:00 = 3600s).
    Priority:
      1) Check for a primary <mc-clip> with an offset near 3600s. This is the most
         reliable indicator after the timeline has been modified.
      2) If not found, check for a top-level <gap start="...">. This works for
         the initial, unmodified FCPXML.
      3) Otherwise, default to 3600.0 seconds as a final fallback.
    """
    # 1. Prioritize the offset of the first mc-clip, as it's the most stable element.
    main_mc_clip = spine.find("mc-clip")
    if main_mc_clip is not None and main_mc_clip.get("offset"):
        try:
            offset_sec = float(parse_rational_seconds(main_mc_clip.get("offset")))
            # If the offset is within a reasonable range of 1 hour, use it as the base.
            if 3599.0 < offset_sec < 3601.0:
                return offset_sec
        except Exception:
            pass # Continue to next method if parsing fails

    # 2. Check for a leading gap with a 'start' attribute (for original FCPXMLs).
    gap = spine.find("gap")
    if gap is not None and gap.get("start"):
        try:
            return float(parse_rational_seconds(gap.get("start")))
        except Exception:
            pass # Continue to fallback

    # 3. Final fallback for timelines that might not have a gap or expected mc-clip.
    return 3600.0

def pretty_write_xml(tree: ET.ElementTree, out_path: Path) -> None:
    rough = ET.tostring(tree.getroot(), encoding="utf-8")
    dom = minidom.parseString(rough)
    pretty = dom.toprettyxml(indent="    ", encoding="utf-8")
    # Strip blank lines to match prior style
    text_lines = [line for line in pretty.decode("utf-8").split("\n") if line.strip()]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(text_lines))

# ---------------------------
# Resources: Formats & Assets
# ---------------------------

def ensure_format_1080p30(resources: ET.Element) -> str:
    """
    Ensure a 1080p30 format exists. Return its @id.
    """
    for fmt in resources.findall("format"):
        fd = fmt.get("frameDuration")
        if not fd:
            continue
        sec = parse_rational_seconds(fd)
        fps = float(Fraction(1, 1) / sec)
        if abs(fps - 30.0) < 0.01 and fmt.get("width") == "1920" and fmt.get("height") == "1080":
            return fmt.get("id")
    new_id = f"r{find_next_r_id(resources)}"
    ET.SubElement(resources, "format", {
        "id": new_id,
        "name": "FFVideoFormat1080p30",
        "width": "1920",
        "height": "1080",
        "frameDuration": "1/30s"
    })
    return new_id

def find_existing_asset_by_src(resources: ET.Element, mov_uri: str) -> Optional[ET.Element]:
    for a in resources.findall("asset"):
        mr = a.find("media-rep")
        if mr is not None and mr.get("src") == mov_uri:
            return a
    return None

def ensure_asset(resources: ET.Element, mov_path: Path, asset_format_id: str, nominal_duration_sec: float) -> str:
    """
    Ensure an <asset> exists for mov_path. If not present, create one with start=0/1s
    and duration ≈ nominal_duration_sec. Return asset @id.
    """
    mov_uri = mov_path.as_uri().replace('%5C', '/')
    existing = find_existing_asset_by_src(resources, mov_uri)
    if existing is not None:
        # Make sure its format matches the desired 1080p30 format
        if existing.get("format") != asset_format_id:
            existing.set("format", asset_format_id)
        return existing.get("id")

    new_id = f"r{find_next_r_id(resources)}"
    asset_el = ET.SubElement(resources, "asset", {
        "id": new_id,
        "name": mov_path.name,
        "hasVideo": "1",
        "format": asset_format_id,
        "duration": float_to_time_literal(max(nominal_duration_sec, 0.5), denom=2000),  # fine-grained literal
        "start": "0/1s"
    })
    ET.SubElement(asset_el, "media-rep", {"kind": "original-media", "src": mov_uri})
    return new_id

# ---------------------------
# mc-clip Containment & Clip Creation
# ---------------------------

def pick_containing_mc(spine: ET.Element, abs_start_sec: float) -> Optional[ET.Element]:
    """
    Return the mc-clip whose [offset, offset+duration) contains abs_start_sec. If multiple,
    pick the one with the greatest offset (latest).
    """
    matches: List[Tuple[float, float, ET.Element]] = []
    for mc in spine.findall("mc-clip"):
        off = mc.get("offset"); dur = mc.get("duration")
        if not off or not dur:
            continue
        off_s = float(parse_rational_seconds(off))
        dur_s = float(parse_rational_seconds(dur))
        if off_s <= abs_start_sec < off_s + dur_s:
            matches.append((off_s, dur_s, mc))
    if not matches:
        return None
    matches.sort(key=lambda t: t[0])
    return matches[-1][2]

def append_asset_clip(parent_el: ET.Element,
                      asset_id: str,
                      asset_name: str,
                      format_id: str,
                      lane: str,
                      offset_seconds_literal: str,
                      duration_seconds_literal: str) -> ET.Element:
    """
    Create and append an <asset-clip> with provided offset/duration literals.
    """
    clip = ET.Element("asset-clip", {
        "lane": lane,
        "name": asset_name,
        "ref": asset_id,
        "tcFormat": "NDF",
        "offset": offset_seconds_literal,
        "format": format_id,
        "enabled": "1",
        "duration": duration_seconds_literal,
        "start": "0/1s"
    })
    clip.append(ET.Element("adjust-transform", {"position": "0 0", "scale": "1 1", "anchor": "0 0"}))
    parent_el.append(clip)
    return clip

# ---------------------------
# Core Insertion
# ---------------------------

def insert_broll_asset_clip(fcpxml_path: str,
                            out_path: str,
                            base_dir: str,
                            mov_path: str,
                            start_seconds: str,
                            duration_seconds: str) -> None:
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

    # Sequence timebase info (for extending duration safely)
    tb_num, tb_den = timebase_from_sequence(resources, seq)

    # Parse inputs
    start_sec = float(start_seconds)
    duration_sec = float(duration_seconds)

    # Determine timeline base (01:00:00:00 by default, or <gap start="..."> if present)
    base_sec = detect_timeline_base_seconds(spine)

    # Compute absolute timeline position (seconds)
    abs_start_sec = start_sec
    abs_end_sec = abs_start_sec + duration_sec

    # Ensure 1080p30 format for b-roll and asset entry
    fmt30_id = ensure_format_1080p30(resources)
    asset_id = ensure_asset(resources, mov_path, fmt30_id, nominal_duration_sec=duration_sec)

    # Determine mc-clip containment
    target_mc = pick_containing_mc(spine, abs_start_sec)

    if target_mc is not None:
        # Inside mc-clip: compute offset in TC domain
        mc_off_s = float(parse_rational_seconds(target_mc.get("offset")))
        mc_dur_s = float(parse_rational_seconds(target_mc.get("duration")))
        mc_tc_start_s = float(parse_rational_seconds(target_mc.get("start", "0/1s")))  # the mc's content TC anchor

        # Clamp duration so we don't bleed past the mc-clip boundary
        remaining_s = (mc_off_s + mc_dur_s) - abs_start_sec
        if remaining_s <= 0:
            raise RuntimeError("Computed start is at/after mc-clip end after base-offset mapping.")
        effective_duration_s = min(duration_sec, remaining_s)

        # delta from mc offset in timeline domain → add to mc TC start for TC-domain offset literal
        delta_s = abs_start_sec - mc_off_s
        asset_tc_offset_s = mc_tc_start_s + delta_s

        clip_el = append_asset_clip(
            parent_el=target_mc,
            asset_id=asset_id,
            asset_name=mov_path.name,
            format_id=fmt30_id,
            lane="1",
            offset_seconds_literal=float_to_time_literal(asset_tc_offset_s, denom=24000),
            duration_seconds_literal=float_to_time_literal(effective_duration_s, denom=24000),
        )
    else:
        # Outside all mc-clips: insert on spine with timeline-domain offset
        clip_el = append_asset_clip(
            parent_el=spine,
            asset_id=asset_id,
            asset_name=mov_path.name,
            format_id=fmt30_id,
            lane="1",
            offset_seconds_literal=float_to_time_literal(abs_start_sec, denom=24000),
            duration_seconds_literal=float_to_time_literal(duration_sec, denom=24000),
        )

    # Extend sequence duration if needed
    seq_tc_start = parse_rational_seconds(seq.get("tcStart", "0/1s"))
    seq_duration = parse_rational_seconds(seq.get("duration", "0/1s"))
    seq_start_frames = seconds_to_frames(float(seq_tc_start), tb_num, tb_den)
    seq_dur_frames = seconds_to_frames(float(seq_duration), tb_num, tb_den)
    seq_end_frames = seq_start_frames + seq_dur_frames

    overlay_end_frames = seconds_to_frames(abs_end_sec, tb_num, tb_den)
    if overlay_end_frames > seq_end_frames:
        new_seq_duration_frames = overlay_end_frames - seq_start_frames
        seq.set("duration", frames_to_literal(new_seq_duration_frames, tb_num, tb_den))

    # Write output
    pretty_write_xml(tree, out_path)

    # Log
    where = f"inside mc-clip '{target_mc.get('name')}'" if target_mc is not None else "on spine"
    print(f"-> Inserting b-roll: {mov_path.name}")
    print(f"   base={base_sec:.3f}s, start={start_sec:.3f}s → abs={abs_start_sec:.3f}s, duration={duration_sec:.3f}s")
    print(f"   Placement: {where}")
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
