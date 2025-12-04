#!/usr/bin/env python3
# 103_reflow_and_more.py
# v2.2.0 (2025-11-21)
# Purpose: Apply a SINGLE angle change to an FCPXML built by the clean-takes pipeline.
#
# Changes in v2.2.0:
# - Improved "Split Source" compatibility:
#   When changing angles, it specifically looks for mc-source with srcEnable="video" OR srcEnable="all".
#   It does NOT blindly overwrite or recreate sources. This preserves "audio" sources if 108_layer_multicam_audio
#   has already run.
#
# Header Notes:
# - Compatible with timelines produced by 115_timeline_builder.py.
# - Input clock must be remapped (timeline seconds), e.g., from 119_remap_angle_changes.py output.

import argparse
import copy
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------
# Utilities: FCPXML time helpers
# ---------------------------

def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if not s.endswith('s'):
        raise ValueError(f"Invalid FCPXML time literal: {s!r}")
    body = s[:-1]
    if '/' in body:
        n, d = body.split('/')
        return Fraction(int(n), int(d))
    return Fraction(int(body), 1)

def frames_to_fcpx_frac(frames: int, tb_num: int, tb_den: int) -> str:
    # frames * (tb_den/tb_num) = seconds as fraction
    sec = Fraction(frames * tb_den, tb_num)
    return f"{sec.numerator}/{sec.denominator}s"

def frac_to_frames(frac_sec: Fraction, tb_num: int, tb_den: int) -> int:
    # frames = seconds * (tb_num/tb_den)
    x = frac_sec * Fraction(tb_num, tb_den)
    # round to nearest; ensure non-negative
    return max(0, int(round(x.numerator / x.denominator)))

def float_seconds_to_frames(sec: float, tb_num: int, tb_den: int) -> int:
    return frac_to_frames(Fraction(sec).limit_denominator(10_000_000), tb_num, tb_den)

def fcpx_frac_to_float_seconds(s: str) -> float:
    f = parse_fcpx_frac(s)
    return float(f.numerator) / float(f.denominator)

# ---------------------------
# XML helpers
# ---------------------------

def load_fcpxml(path: str) -> Tuple[ET.ElementTree, ET.Element, ET.Element, ET.Element]:
    tree = ET.parse(path)
    root = tree.getroot()
    resources = root.find('.//resources')
    seq = root.find('.//library/event/project/sequence')
    if resources is None or seq is None:
        raise RuntimeError("Could not locate <resources> or <sequence> in FCPXML.")
    spine = seq.find('./spine')
    if spine is None:
        raise RuntimeError("Could not locate <spine> in <sequence>.")
    return tree, root, resources, seq

def timebase_from_sequence(resources_root: ET.Element, seq_elem: ET.Element) -> Tuple[int, int]:
    fmt_id = seq_elem.get('format')
    if not fmt_id:
        raise RuntimeError("Sequence missing 'format' attribute.")
    fmt = resources_root.find(f".//format[@id='{fmt_id}']")
    if fmt is None:
        raise RuntimeError(f"Could not find <format id='{fmt_id}'> in resources.")
    fd = fmt.get('frameDuration')
    if not fd:
        raise RuntimeError("Format missing 'frameDuration'.")
    frac = parse_fcpx_frac(fd)
    # frameDuration = seconds per frame = N/D s ; timebase = D/N fps
    return (frac.denominator, frac.numerator)  # tb_num, tb_den

class SpineClip:
    def __init__(self, el: ET.Element, tb_num: int, tb_den: int):
        self.el = el
        self.offset = parse_fcpx_frac(el.get('offset', '0/1s'))
        self.duration = parse_fcpx_frac(el.get('duration', '0/1s'))
        self.ofr = frac_to_frames(self.offset, tb_num, tb_den)
        self.dfr = frac_to_frames(self.duration, tb_num, tb_den)

    def contains_abs_time_frames(self, t_frames: int) -> bool:
        return self.ofr <= t_frames < (self.ofr + self.dfr)

def get_mcclip_containing_time(spine: ET.Element, t_frames: int, tb_num: int, tb_den: int) -> Optional[Tuple[SpineClip, int]]:
    """
    Returns (SpineClip, index_in_spine) for the mc-clip containing timeline frame t_frames.
    Only considers <mc-clip> elements.
    """
    mc_elems = [el for el in spine if el.tag == 'mc-clip' and el.get('duration')]
    running_index = 0
    for idx, el in enumerate(mc_elems):
        sc = SpineClip(el, tb_num, tb_den)
        if sc.contains_abs_time_frames(t_frames):
            return sc, idx
        running_index += 1
    return None

def compute_mc_domain_delta_frames(mc_clip_el: ET.Element, cut_abs_frames: int, tb_num: int, tb_den: int) -> int:
    """
    delta_frames = cut_abs_frames - mc_clip.offset_frames
    """
    mc_offset = parse_fcpx_frac(mc_clip_el.get('offset', '0/1s'))
    mc_off_frames = frac_to_frames(mc_offset, tb_num, tb_den)
    return max(0, cut_abs_frames - mc_off_frames)

def find_media_by_ref(resources: ET.Element, ref_id: str) -> ET.Element:
    media = resources.find(f".//media[@id='{ref_id}']")
    if media is None:
        raise RuntimeError(f"Referenced <media id='{ref_id}'> not found in <resources>.")
    return media

def build_angle_map(media_el: ET.Element) -> Tuple[Dict[str, str], List[str]]:
    """
    Returns:
      - name_to_id: {angle name -> angleID}
      - ordered_ids: angleIDs in encountered order
    """
    name_to_id: Dict[str, str] = {}
    order: List[str] = []
    multicam = media_el.find('multicam')
    if multicam is None:
        raise RuntimeError("Referenced media is not a <multicam>.")
    for mc_angle in multicam.findall('mc-angle'):
        aid = mc_angle.get('angleID', '')
        nm = mc_angle.get('name', '')
        if aid:
            order.append(aid)
        if nm and aid:
            name_to_id[nm] = aid
    return name_to_id, order

def get_video_source_element(mc_clip_el: ET.Element) -> ET.Element:
    """
    Smart finder for the mc-source responsible for video.
    Priority: srcEnable='video', then srcEnable='all', then first found.
    """
    # 1. Explicit video
    vid = mc_clip_el.find("mc-source[@srcEnable='video']")
    if vid is not None: return vid
    
    # 2. Combined all (standard default)
    all_src = mc_clip_el.find("mc-source[@srcEnable='all']")
    if all_src is not None: return all_src
    
    # 3. Fallback: create default 'all' if missing completely
    mc_source = ET.SubElement(mc_clip_el, 'mc-source', {'angleID': '', 'srcEnable': 'all'})
    ET.SubElement(mc_source, 'adjust-transform', {'scale': '1 1', 'position': '0 0', 'anchor': '0 0'})
    return mc_source

def get_current_angle_id(mc_clip_el: ET.Element) -> str:
    # Use the smart finder
    src = get_video_source_element(mc_clip_el)
    return src.get('angleID', "") or ""

# ---------------------------
# Core operation
# ---------------------------

def apply_single_angle_change(
    fcpxml_in: str,
    fcpxml_out: str,
    cut_seconds: float,
    target_angle_name: Optional[str],
    target_angle_index: Optional[int],
) -> None:
    """
    Split the containing mc-clip at the given absolute timeline seconds and
    set the RIGHT segment's video mc-source.angleID to the target.
    """
    # Load XML and timeline info
    tree, root, resources, seq = load_fcpxml(fcpxml_in)
    spine = seq.find('./spine')
    if spine is None:
        raise RuntimeError("No <spine> found in sequence.")

    tb_num, tb_den = timebase_from_sequence(resources, seq)

    # Convert cut to frames in absolute timeline domain
    cut_frames = float_seconds_to_frames(cut_seconds, tb_num, tb_den)

    found = get_mcclip_containing_time(spine, cut_frames, tb_num, tb_den)
    if not found:
        # Build a diagnostic dump
        diag = collect_timeline_diag(spine, tb_num, tb_den)
        raise RuntimeError(
            json.dumps({
                "error": "Cut time not inside any <mc-clip>.",
                "cut_seconds": cut_seconds,
                "cut_frames": cut_frames,
                "spine_mc_clips": diag
            }, indent=2)
        )

    containing_sc, mc_idx = found
    mc_el = containing_sc.el

    # Resolve angle target
    ref_media_id = mc_el.get('ref', '')
    if not ref_media_id:
        raise RuntimeError("mc-clip missing 'ref' to <media>.")
    media_el = find_media_by_ref(resources, ref_media_id)
    name_to_id, ordered_ids = build_angle_map(media_el)

    current_angle_id = get_current_angle_id(mc_el)

    target_angle_id = ""
    if target_angle_name:
        target_angle_id = name_to_id.get(target_angle_name, "")
    if not target_angle_id and target_angle_index is not None:
        # index is 1-based in CLI to be human-friendly
        idx0 = max(0, target_angle_index - 1)
        if 0 <= idx0 < len(ordered_ids):
            target_angle_id = ordered_ids[idx0]
    if not target_angle_id:
        # Fallback to current/default (explicitly permitted)
        target_angle_id = current_angle_id

    # Compute delta in mc-domain frames and validate split feasibility
    delta_frames = compute_mc_domain_delta_frames(mc_el, cut_frames, tb_num, tb_den)
    if delta_frames <= 0 or delta_frames >= containing_sc.dfr:
        # Degenerate split; treat as no-op with diagnostics and review.log entry
        diag = dump_mc_clip_diag(mc_el, tb_num, tb_den, cut_seconds, cut_frames, delta_frames)

        project_dir = Path(fcpxml_out).resolve().parent
        project_name = project_dir.name

        record: Dict[str, Any] = {
            "status": "skipped_degenerate",
            "reason": "Cut lies at or outside clip boundaries (degenerate split).",
            "fcpxml_in": str(Path(fcpxml_in)),
            "fcpxml_out": str(Path(fcpxml_out)),
            "project": project_name,
            "target_angle_name": target_angle_name,
            "target_angle_index": target_angle_index,
            "current_angle_id": current_angle_id,
            "target_angle_id": target_angle_id,
            "timestamp_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        record.update(diag)

        # Write timeline unchanged to output path
        pretty_write_xml(tree, fcpxml_out)

        # Append review record
        review_log_path = project_dir / "review.log"
        try:
            with review_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

        # Emit diagnostics to stdout and exit gracefully
        print(json.dumps(record, indent=2))
        return

    # Prepare LEFT and RIGHT mc-clip elements
    left_el = mc_el
    right_el = copy.deepcopy(mc_el)

    # Times as frames
    left_ofr = containing_sc.ofr  # unchanged
    left_dfr = delta_frames
    right_ofr = containing_sc.ofr + delta_frames
    right_dfr = containing_sc.dfr - delta_frames

    # Update LEFT duration only
    left_el.set('duration', frames_to_fcpx_frac(left_dfr, tb_num, tb_den))

    # Update RIGHT offset/duration and 'start' (source time into multicam)
    right_el.set('offset', frames_to_fcpx_frac(right_ofr, tb_num, tb_den))
    right_el.set('duration', frames_to_fcpx_frac(right_dfr, tb_num, tb_den))

    # 'start' is a source time into the multicam subclip; add delta to RIGHT
    start_frac = parse_fcpx_frac(mc_el.get('start', '0/1s'))
    start_frames = frac_to_frames(start_frac, tb_num, tb_den)
    right_start_frames = start_frames + delta_frames
    right_el.set('start', frames_to_fcpx_frac(right_start_frames, tb_num, tb_den))

    # RIGHT mc-source must switch to target angle
    # V2.2.0 CHANGE: Find the specific video source to modify instead of wiping/creating generic
    right_mc_source = get_video_source_element(right_el)
    right_mc_source.set('angleID', target_angle_id)
    # Important: Do NOT force srcEnable to 'all' here if it was 'video'. Keep it as is.
    # But if it was newly created by get_video_source_element (rare), it defaults to 'all'.

    # LEFT mc-source remains as-is

    # Insert RIGHT after LEFT in spine
    spine_children = list(spine)
    actual_index = None
    for i, child in enumerate(spine_children):
        if child is mc_el:
            actual_index = i
            break
    if actual_index is None:
        raise RuntimeError("Internal error: could not locate mc-clip index in spine children.")

    # Replace existing left in-place (already updated), then insert right immediately after
    spine.insert(actual_index + 1, right_el)

    # Write out
    pretty_write_xml(tree, fcpxml_out)

    # Report
    print(json.dumps({
        "status": "ok",
        "applied_change": {
            "cut_seconds": cut_seconds,
            "cut_frames": cut_frames,
            "target_angle_id": target_angle_id
        },
        "affected_mc_clip": {
            "ref_media_id": ref_media_id,
            "left": {
                "offset_frames": left_ofr,
                "duration_frames": left_dfr,
                "start_frames": start_frames
            },
            "right": {
                "offset_frames": right_ofr,
                "duration_frames": right_dfr,
                "start_frames": right_start_frames
            }
        },
        "output": fcpxml_out
    }, indent=2))

# ---------------------------
# Diagnostics & Writing
# ---------------------------

def collect_timeline_diag(spine: ET.Element, tb_num: int, tb_den: int) -> List[Dict[str, Any]]:
    diag: List[Dict[str, Any]] = []
    for el in spine:
        if el.tag != 'mc-clip':
            continue
        off = parse_fcpx_frac(el.get('offset', '0/1s'))
        dur = parse_fcpx_frac(el.get('duration', '0/1s'))
        diag.append({
            "name": el.get('name', ''),
            "ref": el.get('ref', ''),
            "offset_s": fcpx_frac_to_float_seconds(el.get('offset', '0/1s')),
            "duration_s": fcpx_frac_to_float_seconds(el.get('duration', '0/1s')),
            "offset_frames": frac_to_frames(off, tb_num, tb_den),
            "duration_frames": frac_to_frames(dur, tb_num, tb_den)
        })
    return diag

def dump_mc_clip_diag(mc_el: ET.Element, tb_num: int, tb_den: int, cut_seconds: float, cut_frames: int, delta_frames: int) -> Dict[str, Any]:
    off = parse_fcpx_frac(mc_el.get('offset', '0/1s'))
    dur = parse_fcpx_frac(mc_el.get('duration', '0/1s'))
    start = parse_fcpx_frac(mc_el.get('start', '0/1s'))
    return {
        "cut_seconds": cut_seconds,
        "cut_frames": cut_frames,
        "delta_frames": delta_frames,
        "mc_clip": {
            "name": mc_el.get('name', ''),
            "ref": mc_el.get('ref', ''),
            "offset_s": fcpx_frac_to_float_seconds(mc_el.get('offset', '0/1s')),
            "duration_s": fcpx_frac_to_float_seconds(mc_el.get('duration', '0/1s')),
            "start_s": fcpx_frac_to_float_seconds(mc_el.get('start', '0/1s')),
            "offset_frames": frac_to_frames(off, tb_num, tb_den),
            "duration_frames": frac_to_frames(dur, tb_num, tb_den),
            "start_frames": frac_to_frames(start, tb_num, tb_den),
        }
    }

def pretty_write_xml(tree: ET.ElementTree, out_path: str):
    # Produce compact, stable XML with normalized whitespace (no blank lines)
    raw = ET.tostring(tree.getroot(), encoding='utf-8')
    try:
        from xml.dom import minidom
        parsed = minidom.parseString(raw)
        ugly = parsed.toprettyxml(indent="    ", newl="\n", encoding="utf-8")
        lines = [ln for ln in ugly.decode('utf-8').splitlines() if ln.strip()]
        text = "\n".join(lines)
    except Exception:
        # Fallback: write raw if pretty fails
        text = raw.decode('utf-8')
    Path(out_path).write_text(text, encoding='utf-8')

# ---------------------------
# CLI parsing
# ---------------------------

def main():
    parser = argparse.ArgumentParser(description="Apply a single angle change to an FCPXML timeline by splitting an mc-clip at a remapped cut time.")
    parser.add_argument('--fcpxml', required=True, help="Input FCPXML path.")
    parser.add_argument('--out', required=True, help="Output FCPXML path.")

    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('apply-angle-change', help='Split containing mc-clip at cut time and set RIGHT segment to target angle.')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--cut-seconds', type=float, help='Absolute timeline cut time (seconds) already remapped to the clean-takes timeline.')
    g.add_argument('--change-json', type=str, help='Path to a JSON file with fields: remapped_cut_time, and either target_angle_name or target_angle_index.')

    p.add_argument('--target-angle-name', type=str, default=None, help='Target angle name (preferred).')
    p.add_argument('--target-angle-index', type=int, default=None, help='Target angle index (1-based, fallback if name not provided).')

    args = parser.parse_args()

    # Resolve inputs
    cut_seconds: Optional[float] = None
    target_angle_name: Optional[str] = args.target_angle_name
    target_angle_index: Optional[int] = args.target_angle_index

    if args.change_json:
        data = json.loads(Path(args.change_json).read_text(encoding='utf-8'))
        if 'remapped_cut_time' not in data or data['remapped_cut_time'] is None:
            raise RuntimeError(json.dumps({
                "error": "Missing remapped_cut_time in change JSON.",
                "file": args.change_json,
                "hint": "Run 119_remap_angle_changes.py first; or provide --cut-seconds directly."
            }, indent=2))
        cut_seconds = float(data['remapped_cut_time'])
        if target_angle_name is None:
            target_angle_name = data.get('target_angle_name')
        if target_angle_index is None and 'target_angle_index' in data and data['target_angle_index'] is not None:
            target_angle_index = int(data['target_angle_index'])
    else:
        cut_seconds = float(args.cut_seconds)

    if cut_seconds is None:
        raise RuntimeError("No cut time provided after argument resolution.")

    try:
        apply_single_angle_change(
            fcpxml_in=args.fcpxml,
            fcpxml_out=args.out,
            cut_seconds=cut_seconds,
            target_angle_name=target_angle_name,
            target_angle_index=target_angle_index,
        )
    except Exception as e:
        # Hard abort with detailed context for genuine errors.
        msg = str(e)
        # If already JSON-ish, pass through; otherwise wrap
        try:
            json.loads(msg)
            print(msg)
        except Exception:
            print(json.dumps({"error": "Unhandled exception applying angle change.", "detail": msg}, indent=2))
        sys.exit(1)

if __name__ == '__main__':
    main()