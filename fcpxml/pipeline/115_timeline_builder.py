#!/usr/bin/env python3
# 115_timeline_builder.py
# v5.5 (2025-09-14)
# - Implemented the correct offset calculation for nested clips for full DaVinci Resolve compatibility.
# - Isolated the offset logic in a new helper function for clarity and future tweaking.

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

# (All utility functions from the previous version remain the same)
# ---------- Time & XML Utilities (No Changes) ----------
def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if not s.endswith('s'): raise ValueError(f"Invalid FCPXML time format: {s}")
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
        clips = [el for el in spine if el.get('duration') and el.tag.endswith('clip') and not el.get('lane')]
        if not clips:
            total_duration_frames = 0
        else:
            tc_start_frames = frac_to_frames(parse_fcpx_frac(sequence.get('tcStart', '0/1s')), tb_num, tb_den)
            current_offset_frames = tc_start_frames 
            for clip_el in clips:
                clip_el.set('offset', frames_to_fcpx_frac(current_offset_frames, tb_num, tb_den))
                dur_frames = frac_to_frames(parse_fcpx_frac(clip_el.get('duration')), tb_num, tb_den)
                current_offset_frames += dur_frames
            last_clip_el = clips[-1]
            last_offset_frames = frac_to_frames(parse_fcpx_frac(last_clip_el.get('offset')), tb_num, tb_den)
            last_dur_frames = frac_to_frames(parse_fcpx_frac(last_clip_el.get('duration')), tb_num, tb_den)
            new_end_point_frames = last_offset_frames + last_dur_frames
            total_duration_frames = max(0, new_end_point_frames - tc_start_frames)
    sequence.set('duration', frames_to_fcpx_frac(total_duration_frames, tb_num, tb_den))
    print(f"Sequence duration updated to {total_duration_frames} frames.")

def _copy_adjust_children(src_asset_clip: ET.Element, dst_asset_clip: ET.Element):
    for child in list(src_asset_clip):
        tag = child.tag
        if tag.startswith('adjust-') or tag in ('filter-video', 'filter-audio', 'timeMap', 'param'):
            dst_asset_clip.append(copy.deepcopy(child))

def _create_multicam_subclip_resource(
    resources: ET.Element, tb_num: int, tb_den: int, source_ref: str,
    start_sec: float, duration_sec: float, name: str
) -> Tuple[str, str, str, str]:
    # This function is unchanged and correctly creates the media resource
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
    new_media_id = f"r{max_r_id + 1}"
    new_media = ET.Element('media', {'name': name, 'id': new_media_id})
    new_multicam = ET.SubElement(new_media, 'multicam', {'tcStart': new_start_str, 'tcFormat': source_multicam.get('tcFormat', 'NDF'), 'format': source_multicam.get('format', '')})
    default_angle_id = ""
    for i, source_angle in enumerate(source_multicam.findall('mc-angle')):
        source_asset_clip = source_angle.find('asset-clip')
        if source_asset_clip is None: continue
        orig_asset_ref = source_asset_clip.get('ref')
        if not orig_asset_ref: continue
        new_angle_id = str(uuid.uuid4())
        if i == 0: default_angle_id = new_angle_id
        new_angle = ET.SubElement(new_multicam, 'mc-angle', {'name': source_angle.get('name', f'Angle {i+1}'), 'angleID': new_angle_id})
        original_asset = resources.find(f".//asset[@id='{orig_asset_ref}']")
        if original_asset is None: continue
        original_asset_start_f = parse_fcpx_frac(original_asset.get('start', '0/1s'))
        start_incr_f = parse_fcpx_frac(frames_to_fcpx_frac(start_incr_frames, tb_num, tb_den))
        new_asset_clip_start_f = original_asset_start_f + start_incr_f
        new_asset_clip_start_str = f"{new_asset_clip_start_f.numerator}/{new_asset_clip_start_f.denominator}s"
        new_clip_attrs = {'ref': orig_asset_ref, 'name': source_asset_clip.get('name'), 'offset': source_asset_clip.get('offset', '0/1s'),'duration': new_duration_str, 'start': new_asset_clip_start_str, 'format': source_asset_clip.get('format', ''),'enabled': '1', 'tcFormat': source_multicam.get('tcFormat', 'NDF')}
        new_asset_clip = ET.SubElement(new_angle, 'asset-clip', {k: v for k, v in new_clip_attrs.items() if v is not None})
        _copy_adjust_children(source_asset_clip, new_asset_clip)
    resources.append(new_media)
    print(f"Created new sub-clip resource '{name}' with ID '{new_media_id}'")
    return new_media_id, new_start_str, new_duration_str, default_angle_id

def cmd_process_take(
    resources: ET.Element, spine: ET.Element, tb_num: int, tb_den: int, source_ref: str,
    start_sec: float, duration_sec: float, name: str
):
    new_media_id, new_start_str, new_duration_str, default_angle_id = _create_multicam_subclip_resource(
        resources, tb_num, tb_den, source_ref, start_sec, duration_sec, name
    )
    take_match = re.search(r'Subclip (\d+)$', name)
    take_number = int(take_match.group(1)) if take_match else 0
    if take_number == 1:
        target_clip_el = spine.find(f"./mc-clip[@ref='{source_ref}']")
        if target_clip_el is None: raise RuntimeError(f"Could not find placeholder mc-clip with ref='{source_ref}' to replace.")
        print(f" -> Replacing placeholder for '{source_ref}' on timeline.")
        target_clip_el.set('ref', new_media_id)
        target_clip_el.set('name', name)
        target_clip_el.set('duration', new_duration_str)
        target_clip_el.set('start', new_start_str)
        mc_source = target_clip_el.find('mc-source')
        if mc_source and default_angle_id: mc_source.set('angleID', default_angle_id)
    else:
        previous_take_name = name.replace(f'Subclip {take_number}', f'Subclip {take_number - 1}')
        previous_clip_el = spine.find(f"./mc-clip[@name='{previous_take_name}']")
        if previous_clip_el is None: raise RuntimeError(f"Could not find previous take '{previous_take_name}' to insert after.")
        print(f" -> Inserting after '{previous_take_name}' on timeline.")
        spine_children = list(spine)
        insert_index = spine_children.index(previous_clip_el) + 1
        clip_attrs = {"name": name, "ref": new_media_id, "duration": new_duration_str, "start": new_start_str, "offset": "0/1s"}
        new_clip_el = ET.Element("mc-clip", attrib=clip_attrs)
        mc_source = ET.SubElement(new_clip_el, "mc-source", attrib={"angleID": default_angle_id or "", "srcEnable": "all"})
        ET.SubElement(mc_source, "adjust-transform", attrib={"scale": "1 1", "position": "0 0", "anchor": "0 0"})
        spine.insert(insert_index, new_clip_el)

# --- NEW HELPER: The "Math" for calculating the nested offset ---
def _calculate_alt_take_offset(primary_clip: ET.Element) -> str:
    """
    Calculates the correct offset for a nested alternate take for DaVinci Resolve.
    RULE: The offset of a nested clip is the 'start' time of its parent clip.
    This syncs the clips based on their source media in-point.
    """
    parent_start_str = primary_clip.get('start')
    if not parent_start_str:
        raise ValueError(f"Target primary clip '{primary_clip.get('name')}' is missing its 'start' attribute, cannot calculate offset.")
    return parent_start_str

# --- UPDATED: 'add-alt' now uses the correct offset math and nesting ---
def cmd_add_alt(
    resources: ET.Element, spine: ET.Element, tb_num: int, tb_den: int, source_ref: str,
    start_sec: float, duration_sec: float, name: str, target_name: str, lane: int
):
    primary_clip = spine.find(f".//mc-clip[@name='{target_name}']")
    if primary_clip is None:
        raise RuntimeError(f"Could not find target clip '{target_name}' on the timeline.")
    
    # Calculate the correct offset using the new rule
    alt_take_offset = _calculate_alt_take_offset(primary_clip)
    print(f"Found target clip '{target_name}'. Calculated alt take offset: {alt_take_offset}")

    new_media_id, new_start_str, new_duration_str, default_angle_id = _create_multicam_subclip_resource(
        resources, tb_num, tb_den, source_ref, start_sec, duration_sec, name
    )

    clip_attrs = {
        "name": name, "ref": new_media_id, "duration": new_duration_str, "start": new_start_str,
        "offset": alt_take_offset, "lane": str(lane), "enabled": "0"
    }
    new_clip_el = ET.Element("mc-clip", attrib=clip_attrs)
    mc_source = ET.SubElement(new_clip_el, "mc-source", attrib={"angleID": default_angle_id or "", "srcEnable": "all"})
    ET.SubElement(mc_source, "adjust-transform", attrib={"scale": "1 1", "position": "0 0", "anchor": "0 0"})
    
    primary_clip.append(new_clip_el)
    print(f"-> Nesting '{name}' inside '{target_name}' as a connected clip.")

# ---------- Main (No changes) ----------
def main():
    parser = argparse.ArgumentParser(description="Builds a clean timeline by replacing placeholders with subclips.")
    parser.add_argument('--fcpxml', required=True)
    parser.add_argument('--out', required=True)
    subparsers = parser.add_subparsers(dest='command', required=True)
    p_pt = subparsers.add_parser('process-take', help="Adds a clip sequentially to the main storyline.")
    p_pt.add_argument('--source-ref', required=True)
    p_pt.add_argument('--start-seconds', required=True, type=float)
    p_pt.add_argument('--duration-seconds', required=True, type=float)
    p_pt.add_argument('--name', required=True, type=str)
    p_alt = subparsers.add_parser('add-alt', help="Adds an alternate take on a specific lane, synced to a primary take.")
    p_alt.add_argument('--source-ref', required=True)
    p_alt.add_argument('--start-seconds', required=True, type=float)
    p_alt.add_argument('--duration-seconds', required=True, type=float)
    p_alt.add_argument('--name', required=True, type=str)
    p_alt.add_argument('--target-name', required=True, type=str)
    p_alt.add_argument('--lane', required=True, type=int)
    args = parser.parse_args()
    
    tree, root, resources, sequence = load_spine_elements(args.fcpxml)
    spine = sequence.find('./spine')
    if spine is None: raise RuntimeError("Spine element not found.")
    tb_num, tb_den = timebase_from_sequence(resources, sequence)
    
    # We don't need a timecode log for this script's logic
    if args.command == 'process-take':
        cmd_process_take(resources, spine, tb_num, tb_den, args.source_ref, args.start_seconds, args.duration_seconds, args.name)
    elif args.command == 'add-alt':
        cmd_add_alt(resources, spine, tb_num, tb_den, args.source_ref, args.start_seconds, args.duration_seconds, args.name, args.target_name, args.lane)

    update_sequence_duration(sequence, tb_num, tb_den)
    pretty_write_xml(tree, args.out)
    print(f"\nSuccessfully wrote modified timeline to: {args.out}")

if __name__ == '__main__':
    main()