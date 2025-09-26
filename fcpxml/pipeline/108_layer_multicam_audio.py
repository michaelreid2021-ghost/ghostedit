#!/usr/bin/env python3
# 108_layer_multicam_audio.py
# v1.3 (2025-08-21)
# Purpose: Rebuild FCPXML multicam sections so that each contiguous sequence of <mc-clip>
#          sharing the same ref id has a single, continuous audio bed (srcEnable="all") on V1,
#          with all visual cutaways duplicated as video-only (srcEnable="video") on lane="1".
# Notes:
# - Self-contained: supports either --base (auto paths) or explicit --fcpxml/--out.
# - In-place replacement: only multicam runs are rebuilt; everything else in the spine is preserved.

import argparse
import copy
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import List, Optional, Tuple


# ------------------------- Utilities -------------------------

def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if not s.endswith('s'):
        raise ValueError(f"Invalid FCPXML time format (must end with 's'): {s!r}")
    body = s[:-1]
    if '/' in body:
        n, d = body.split('/')
        return Fraction(int(n), int(d))
    # integer or decimal seconds are rare, but support integer seconds defensively
    return Fraction(body)


def sum_durations_str(clips: List[ET.Element]) -> str:
    total = Fraction(0, 1)
    for c in clips:
        total += parse_fcpx_frac(c.get('duration', '0/1s'))
    return f"{total.numerator}/{total.denominator}s"


def get_mc_source(el: ET.Element) -> Optional[ET.Element]:
    return el.find('mc-source')


def get_src_enable(mc_source: ET.Element) -> str:
    # FCPXML defaults to "all" if not explicitly "video"
    val = mc_source.get('srcEnable')
    return val if val in ('all', 'video') else 'all'


def find_spine(root: ET.Element) -> ET.Element:
    spine = root.find('.//spine')
    if spine is None:
        raise RuntimeError("Could not locate <spine> in FCPXML.")
    return spine


def contiguous_mc_runs(spine: ET.Element) -> List[List[ET.Element]]:
    """
    Return lists of adjacent <mc-clip> elements that all share the same ref id
    and are contiguous (i.e., no non-mc element in between).
    Only runs of length >= 2 are interesting for rebuild.
    """
    runs: List[List[ET.Element]] = []
    cur: List[ET.Element] = []
    cur_ref: Optional[str] = None
    for el in spine:
        if el.tag == 'mc-clip':
            ref = el.get('ref')
            if cur and ref == cur_ref:
                cur.append(el)
            else:
                if len(cur) >= 2:
                    runs.append(cur)
                cur = [el]
                cur_ref = ref
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
            cur_ref = None
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def first_clip_with_audio(clips: List[ET.Element]) -> ET.Element:
    """
    Choose the first clip in the run whose mc-source is NOT explicitly video-only.
    If all are video-only, fall back to the first clip.
    """
    for c in clips:
        ms = get_mc_source(c)
        if ms is None:
            continue
        if get_src_enable(ms) != 'video':
            return c
    return clips[0]


def clone_with_lane(el: ET.Element, lane_value: Optional[str]) -> ET.Element:
    dup = copy.deepcopy(el)
    if lane_value is None:
        if 'lane' in dup.attrib:
            del dup.attrib['lane']
    else:
        dup.set('lane', lane_value)
    return dup


@dataclass(frozen=True)
class ProjectPaths:
    base: Path  # project root

    @property
    def info_fcpxml(self) -> Path:
        return self.base / "Info.fcpxml"

    @property
    def out_layered(self) -> Path:
        return self.base / "ai_working_files" / "108_layered_audio_final.fcpxml"


def resolve_io_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    """
    Supports either:
      --base /path/to/project        (reads base/Info.fcpxml, writes base/ai_working_files/108_layered_audio_final.fcpxml)
    OR
      --fcpxml in.fcpxml --out out.fcpxml
    """
    if args.base:
        p = ProjectPaths(Path(args.base).resolve())
        return (p.info_fcpxml, p.out_layered)
    if args.fcpxml and args.out:
        return (Path(args.fcpxml).resolve(), Path(args.out).resolve())
    raise SystemExit("Specify either --base OR both --fcpxml and --out.")


# ------------------------- Core Rebuild Logic -------------------------

def rebuild_multicam_runs(tree: ET.ElementTree) -> bool:
    """
    For each contiguous mc-clip run with same ref, replace that run with:
      - one audio bed clip (srcEnable="all") at V1 covering full run duration
      - each original segment duplicated as video-only (srcEnable="video") at lane="1"
    Returns True if any changes made.
    """
    root = tree.getroot()
    spine = find_spine(root)
    runs = contiguous_mc_runs(spine)
    if not runs:
        return False

    # We'll operate run-by-run, replacing in-place at the index of the run's first clip.
    for run in runs:
        first = run[0]
        try:
            insert_index = list(spine).index(first)
        except ValueError:
            # Shouldn't happen, but if it does, skip this run safely
            continue

        # Determine audio anchor clip (the one that carried audio)
        anchor_clip = first_clip_with_audio(run)
        anchor_ms = get_mc_source(anchor_clip)
        if anchor_ms is None:
            # If somehow missing, skip safely
            continue

        # Calculate span for audio bed
        total_duration = sum_durations_str(run)
        start_offset = run[0].get('offset')
        start_time = run[0].get('start')

        # Build audio bed from the anchor clip's structure
        audio_bed = copy.deepcopy(anchor_clip)
        audio_bed.set('duration', total_duration)
        if start_offset is not None:
            audio_bed.set('offset', start_offset)
        if start_time is not None:
            audio_bed.set('start', start_time)

        audio_bed_ms = get_mc_source(audio_bed)
        if audio_bed_ms is None:
            # If the duplicate somehow lost mc-source, hard abort this run.
            continue
        # Ensure audio bed is "all"
        audio_bed_ms.set('srcEnable', 'all')
        # Ensure the anchor angle is retained
        if anchor_ms.get('angleID'):
            audio_bed_ms.set('angleID', anchor_ms.get('angleID'))

        # Prepare video-only duplicates on lane 1
        video_only_clips: List[ET.Element] = []
        for seg in run:
            dup = clone_with_lane(seg, "1")
            dup_ms = get_mc_source(dup)
            if dup_ms is not None:
                dup_ms.set('srcEnable', 'video')
            video_only_clips.append(dup)

        # Remove the original run
        for old in run:
            try:
                spine.remove(old)
            except ValueError:
                pass  # already removed; continue

        # Insert newly layered structure at the original position:
        # Insert in reverse so final order is audio_bed, then each video segment following.
        for dup in reversed(video_only_clips):
            spine.insert(insert_index, dup)
        spine.insert(insert_index, audio_bed)

    return True


# ------------------------- CLI -------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Restructure multicam edits so each run has a continuous audio bed (V1, srcEnable=all) "
                    "and all visual cutaways as video-only inserts (lane=1, srcEnable=video)."
    )
    p.add_argument("--base", help="Project base directory (expects Info.fcpxml in this folder).")
    p.add_argument("--fcpxml", help="Explicit input FCPXML path.")
    p.add_argument("--out", help="Explicit output FCPXML path.")
    return p


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    in_path, out_path = resolve_io_paths(args)
    if not in_path.exists():
        print(f"[!] FATAL: Input FCPXML not found: {in_path}", file=sys.stderr)
        sys.exit(2)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    tree = ET.parse(str(in_path))
    changed = rebuild_multicam_runs(tree)

    tree.write(str(out_path), encoding='utf-8', xml_declaration=True)
    if changed:
        print(f"[+] Rebuilt multicam audio layering. Wrote: {out_path}")
    else:
        print(f"[i] No qualifying multicam runs found. Copied unchanged to: {out_path}")


if __name__ == "__main__":
    main()

# Changelog:
# v1.3 (2025-08-21): Self-contained IO options; robust audio-anchor selection; safe in-place run replacement.
