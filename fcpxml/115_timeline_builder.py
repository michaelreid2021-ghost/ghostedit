#!/usr/bin/env python3
# 115_timeline_builder.py
# v4.0 (2025-09-04)
# - MAJOR REFACTOR: Implemented intelligent "replace or insert" logic.
#   - The script now parses the subclip name (e.g., "... - Subclip 1").
#   - For the first subclip of a source, it finds and REPLACES the original
#     multicam clip placeholder on the timeline spine.
#   - For subsequent subclips (2, 3, etc.), it finds the previously placed
#     subclip and INSERTS the new one immediately after it.
#   - This preserves the structure of complex timelines with intros, transitions, etc.
# - REMOVED: The '--is-first-take' argument, which is no longer needed.
# - FIXED: Timecode map now loads and appends correctly.

import argparse
import xml.etree.ElementTree as ET
import copy
import re
import uuid
import json
from pathlib import Path
from fractions import Fraction
from xml.dom import minidom
from typing import List, Tuple, Dict, Any

# Global list to store the timecode map segments
timecode_map_segments: List[Dict[str, Any]] = []

# ---------- Time & XML Utilities ----------
def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if not s.endswith('s'):
        raise ValueError(f"Invalid FCPXML time format: {s}")
    body = s[:-1]
    if '/' in body:
        n, d = body.split('/')
        return Fraction(int(n), int(d))
    return Fraction(body)

def frames_to_fcpx_frac(frames: int, tb_num: int, tb_den: int) -> str:
    f = Fraction(frames * tb_den, tb_num)
    return f"{f.numerator}/{f.denominator}s"

def frac_to_frames(frac_sec: Fraction, tb_num: int, tb_den: int) -> int:
    x = frac_sec * Fraction(tb_num, tb_den)
    return int(round(x.numerator / x.denominator))

def seconds_to_frames(sec: float, tb_num: int, tb_den: int) -> int:
    return max(0, int(round(Fraction(sec).limit_denominator(10_000_000) * Fraction(tb_num, tb_den))))

class Clip:
    def __init__(self, el: ET.Element, tb_num: int, tb_den: int):
        self.el = el
        self.offset_f = parse_fcpx_frac(el.get('offset', '0/1s'))
        self.dur_f = parse_fcpx_frac(el.get('duration', '0/1s'))
        self.ofr = frac_to_frames(self.offset_f, tb_num, tb_den)
        self.dfr = max(0, frac_to_frames(self.dur_f, tb_num, tb_den))

def timebase_from_sequence(resources_root: ET.Element, seq_elem: ET.Element) -> Tuple[int, int]:
    fmt_id = seq_elem.get('format')
    if not fmt_id: raise RuntimeError("Sequence missing 'format' attribute.")
    fmt = resources_root.find(f".//format[@id='{fmt_id}']")
    if fmt is None: raise RuntimeError(f"Could not find <format id='{fmt_id}'> in resources.")
    fd = fmt.get('frameDuration')
    if not fd: raise RuntimeError("Format missing 'frameDuration'.")
    frac = parse_fcpx_frac(fd)
    return (frac.denominator, frac.numerator)

def load_spine_elements(fcpx_path: str) -> Tuple[ET.ElementTree, ET.Element, ET.Element, ET.Element]:
    tree = ET.parse(fcpx_path)
    root = tree.getroot()
    resources = root.find('.//resources')
    seq = root.find('.//library/event/project/sequence')
    if resources is None or seq is None: raise RuntimeError("Could not locate resources or sequence.")
    return tree, root, resources, seq

def get_max_resource_id(resources_root: ET.Element) -> int:
    max_id = 0
    for el in resources_root.findall(".//*[@id]"):
        rid = el.get('id', '')
        m = re.match(r'r(\d+)$', rid)
        if m: max_id = max(max_id, int(m.group(1)))
    return max_id

def pretty_write_xml(tree: ET.ElementTree, out_path: str):
    rough_string = ET.tostring(tree.getroot(), 'utf-8')
    reparsed = minidom.parseString(rough_string)
    ugly_xml = reparsed.toprettyxml(indent="    ", newl="\n", encoding="utf-8")
    text_lines = [line for line in ugly_xml.decode('utf-8').split('\n') if line.strip()]
    clean_xml = '\n'.join(text_lines)
    with open(out_path, "w", encoding="utf-8") as f: f.write(clean_xml)

def update_sequence_duration(sequence: ET.Element, tb_num: int, tb_den: int):
    spine = sequence.find('./spine')
    if spine is None or len(spine) == 0:
        total_duration_frames = 0
    else:
        clips = [Clip(el, tb_num, tb_den) for el in spine if el.get('duration') and el.tag.endswith('clip')]
        if not clips:
            total_duration_frames = 0
        else:
            tc_start_frames = frac_to_frames(parse_fcpx_frac(sequence.get('tcStart', '0/1s')), tb_num, tb_den)
            # Recalculate all offsets to ensure they are contiguous
            current_offset_frames = frac_to_frames(parse_fcpx_frac(clips[0].el.get('offset', '0/1s')), tb_num, tb_den)
            for clip in clips:
                clip.ofr = current_offset_frames
                clip.el.set('offset', frames_to_fcpx_frac(current_offset_frames, tb_num, tb_den))
                current_offset_frames += clip.dfr
            
            last_clip = clips[-1]
            new_end_point_frames = last_clip.ofr + last_clip.dfr
            total_duration_frames = max(0, new_end_point_frames - tc_start_frames)
            
    sequence.set('duration', frames_to_fcpx_frac(total_duration_frames, tb_num, tb_den))
    print(f"Sequence duration updated to {total_duration_frames} frames.")

# ---------- Sub-clip Construction ----------
def _copy_adjust_children(src_asset_clip: ET.Element, dst_asset_clip: ET.Element):
    for child in list(src_asset_clip):
        tag = child.tag
        if tag.startswith('adjust-') or tag in ('filter-video', 'filter-audio', 'timeMap', 'param'):
            dst_asset_clip.append(copy.deepcopy(child))

def cmd_process_take(
    resources: ET.Element,
    spine: ET.Element,
    tb_num: int,
    tb_den: int,
    source_ref: str,
    start_sec: float,
    duration_sec: float,
    name: str
):
    # --- Part 1: Create the new subclip <media> resource in <resources> ---
    source_media = resources.find(f".//media[@id='{source_ref}']")
    if source_media is None: raise RuntimeError(f"Could not find source media with ref='{source_ref}'")
    source_multicam = source_media.find('multicam')
    if source_multicam is None: raise RuntimeError(f"Source media '{source_ref}' is not a multicam.")

    src_tc_start_f = parse_fcpx_frac(source_multicam.get('tcStart', '0/1s'))
    src_tc_start_frames = frac_to_frames(src_tc_start_f, tb_num, tb_den)
    start_incr_frames = seconds_to_frames(start_sec, tb_num, tb_den)
    duration_frames = max(1, seconds_to_frames(duration_sec, tb_num, tb_den))

    subclip_abs_start_frames = src_tc_start_frames + start_incr_frames
    new_start_str = frames_to_fcpx_frac(subclip_abs_start_frames, tb_num, tb_den)
    new_duration_str = frames_to_fcpx_frac(duration_frames, tb_num, tb_den)

    max_r_id = get_max_resource_id(resources)
    max_r_id += 1
    new_media_id = f"r{max_r_id}"
    new_media = ET.Element('media', {'name': name, 'id': new_media_id})
    new_multicam = ET.SubElement(new_media, 'multicam', {'tcStart': new_start_str, 'tcFormat': source_multicam.get('tcFormat', 'NDF'), 'format': source_multicam.get('format', '')})

    default_angle_id_for_timeline = ""
    for i, source_angle in enumerate(source_multicam.findall('mc-angle')):
        source_asset_clip = source_angle.find('asset-clip')
        if source_asset_clip is None: continue
        orig_asset_ref = source_asset_clip.get('ref')
        if not orig_asset_ref or resources.find(f".//asset[@id='{orig_asset_ref}']") is None: continue
        
        # NOTE: We no longer need to clone assets. We can reference the originals.
        new_angle_id = str(uuid.uuid4())
        if i == 0: default_angle_id_for_timeline = new_angle_id

        new_angle = ET.SubElement(new_multicam, 'mc-angle', {'name': source_angle.get('name', f'Angle {i+1}'), 'angleID': new_angle_id})
        new_clip_attrs = {'ref': orig_asset_ref, 'name': source_asset_clip.get('name'), 'offset': '0/1s', 'duration': new_duration_str, 'start': new_start_str, 'format': source_asset_clip.get('format', ''), 'enabled': '1', 'tcFormat': source_multicam.get('tcFormat', 'NDF')}
        new_asset_clip = ET.SubElement(new_angle, 'asset-clip', {k: v for k, v in new_clip_attrs.items() if v is not None})
        _copy_adjust_children(source_asset_clip, new_asset_clip)

    resources.append(new_media)
    print(f"Created new sub-clip resource '{name}' with ID '{new_media_id}'")

    # --- Part 2: Find where to place the clip on the timeline spine ---
    take_match = re.search(r'Subclip (\d+)$', name)
    take_number = int(take_match.group(1)) if take_match else 0

    if take_number == 1:
        # REPLACE mode: Find the original placeholder and modify it.
        target_clip_el = spine.find(f"./mc-clip[@ref='{source_ref}']")
        if target_clip_el is None:
            raise RuntimeError(f"FATAL: Could not find placeholder mc-clip with ref='{source_ref}' to replace.")
        print(f" -> Replacing placeholder for '{source_ref}' on timeline.")
        target_clip_el.set('ref', new_media_id)
        target_clip_el.set('name', name)
        target_clip_el.set('duration', new_duration_str)
        target_clip_el.set('start', new_start_str)
        mc_source = target_clip_el.find('mc-source')
        if mc_source and default_angle_id_for_timeline:
            mc_source.set('angleID', default_angle_id_for_timeline)
    else:
        # INSERT mode: Find the previously inserted clip and add this one after it.
        previous_take_name = name.replace(f'Subclip {take_number}', f'Subclip {take_number - 1}')
        previous_clip_el = spine.find(f"./mc-clip[@name='{previous_take_name}']")
        if previous_clip_el is None:
            raise RuntimeError(f"FATAL: Could not find previous take '{previous_take_name}' to insert after.")
        
        print(f" -> Inserting after '{previous_take_name}' on timeline.")
        spine_children = list(spine)
        insert_index = spine_children.index(previous_clip_el) + 1

        clip_attrs = {"name": name, "ref": new_media_id, "duration": new_duration_str, "start": new_start_str, "offset": "0/1s"}
        new_clip_el = ET.Element("mc-clip", attrib=clip_attrs)
        mc_source = ET.SubElement(new_clip_el, "mc-source", attrib={"angleID": default_angle_id_for_timeline or "", "srcEnable": "all"})
        ET.SubElement(mc_source, "adjust-transform", attrib={"scale": "1 1", "position": "0 0", "anchor": "0 0"})
        spine.insert(insert_index, new_clip_el)
    
    # --- Timecode Map Generation (applies to both modes) ---
    # The final timeline position is calculated by update_sequence_duration, so we calculate a temporary one here.
    timeline_start_sec = 0.0 # Will be corrected later
    segment = {"source_start": start_sec, "source_end": start_sec + duration_sec, "timeline_start": timeline_start_sec, "note": "Timeline start is final after all clips are placed"}
    timecode_map_segments.append(segment)
    print(f" -> Mapped source time [{start_sec:.2f}s - {start_sec + duration_sec:.2f}s]")

# ---------- Main ----------
def main():
    global timecode_map_segments
    parser = argparse.ArgumentParser(description="Builds a clean timeline by replacing placeholders with subclips.")
    parser.add_argument('--fcpxml', required=True)
    parser.add_argument('--out', required=True)
    subparsers = parser.add_subparsers(dest='command', required=True)

    p_pt = subparsers.add_parser('process-take')
    p_pt.add_argument('--source-ref', required=True)
    p_pt.add_argument('--start-seconds', required=True, type=float)
    p_pt.add_argument('--duration-seconds', required=True, type=float)
    p_pt.add_argument('--name', required=True, type=str)

    args = parser.parse_args()
    tree, root, resources, sequence = load_spine_elements(args.fcpxml)
    spine = sequence.find('./spine')
    if spine is None: raise RuntimeError("Spine element not found.")
    tb_num, tb_den = timebase_from_sequence(resources, sequence)
    
    output_path = Path(args.out)
    map_path = output_path.parent / "timecode_map.json"

    if map_path.exists():
        try:
            with open(map_path, 'r', encoding='utf-8') as f:
                timecode_map_segments = json.load(f)
        except json.JSONDecodeError:
            print(f"[!] Warning: Could not parse existing timecode map at {map_path}. Starting fresh.")
            timecode_map_segments = []
    
    # On the very first run, the PowerShell script might delete an old map.
    # This logic handles both starting fresh and appending.
    take_match = re.search(r'Subclip 1$', args.name)
    is_first_subclip_of_a_scene = bool(take_match)
    # A simple way to detect the start of a new process run is if we are processing the very first scene's first take.
    # We can infer this if the map is empty and we are processing a "Subclip 1".
    if not timecode_map_segments and is_first_subclip_of_a_scene:
         print("-> Detected start of new build process. Resetting timecode map.")
         timecode_map_segments = []
    
    if args.command == 'process-take':
        cmd_process_take(resources, spine, tb_num, tb_den, args.source_ref, args.start_seconds, args.duration_seconds, args.name)

    update_sequence_duration(sequence, tb_num, tb_den)
    pretty_write_xml(tree, args.out)
    print(f"\nSuccessfully wrote modified timeline to: {args.out}")
    
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(timecode_map_segments, f, indent=2)
    print(f"Successfully wrote timecode map to: {map_path}")

if __name__ == '__main__':
    main()