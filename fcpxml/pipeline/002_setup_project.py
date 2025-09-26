#!/usr/bin/env python3
# 002_setup_and_build.py (v15 - Corrected Asset Offsetting)
# Last modified: 2025-09-07
# Purpose:
#   Build a multicam FCPXML that can place multiple multicam clips (one per take) back-to-back.
#   - TMCD (HH:MM:SS:FF) -> frames via NOMINAL fps (24/30/60).
#   - Drop-frame support for 29.97/59.94 when TC uses ';'.
#   - Emits starts as frames * (tb_den / tb_num), matching Resolve's working N/Ds.
#   - Alignment modes when angles disagree: exact|first|min|median|max.
#   - Optional gap between takes.
#   - Verbose ffprobe dumps + manifest for auditing.
#
# Usage example:
#   python 002_setup_and_build.py \
#     --work-request-dir "E:/ghostedit/assembly_line/IN_PROGRESS/watches_watcher" \
#     --projects-base-dir "E:/ghostedit/assembly_line/projects" \
#     --shared-scripts-dir "E:/ghostedit/fcpxml/shared" \
#     --align-start median \
#     --gap-frames 24

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from fractions import Fraction
from collections import defaultdict
from statistics import median
from typing import Optional, Dict, Any, Tuple, List
import xml.etree.ElementTree as ET
from xml.dom import minidom
import datetime
from types import SimpleNamespace
from project_paths import save_project_config, set_active_project_name

# --- Constants ---
DEFAULT_WORKSPACE_SUBDIR = "ai_working_files"

# ---------- Logger (file + stdout) ----------
try:
    script_directory = Path(__file__).resolve().parent
    script_name = Path(__file__).stem
    log_file_path = script_directory / f"{script_name}.log"
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s")
    log = logging.getLogger(script_name)
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
        fh.setFormatter(log_formatter)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(log_formatter)
        log.addHandler(fh)
        log.addHandler(sh)
except Exception as e:
    logging.basicConfig(level=logging.INFO)
    logging.critical(f"FATAL: Logger setup failed: {e}", exc_info=True)
    sys.exit(1)

# ---------- Utilities ----------
def frames_to_fcpx(frames: int, tb_num: int, tb_den: int) -> str:
    """
    Return a frame-locked time string (e.g., 'N/24000s').
    Directly constructs the fraction string to ensure the denominator
    matches the project timebase. Using fractions.Fraction auto-simplifies
    (e.g., 12000/24000s -> 1/2s), which can cause import errors.
    """
    return f"{int(frames) * tb_den}/{tb_num}s"

def timecode_to_frames(h: int, m: int, s: int, tb_num: int, tb_den: int) -> int:
    """
    Convert a display timecode (H:M:S) to an absolute frame count using the nominal FPS.
    This is for setting timeline start points, not for parsing embedded TMCD.
    For 23.976 (24000/1001), nominal is 24. 01:00:00 -> 86400 frames.
    """
    nfps = nominal_fps(tb_num, tb_den)
    return (h * 3600 + m * 60 + s) * nfps

def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if not s.endswith('s'):
        raise ValueError(f"Bad FCPXML time: {s}")
    body = s[:-1]
    if '/' in body:
        n, d = body.split('/')
        return Fraction(int(n), int(d))
    return Fraction(int(body), 1)

def pretty_write_xml(tree: ET.ElementTree, out_path: Path):
    rough = ET.tostring(tree.getroot(), 'utf-8')
    pretty = minidom.parseString(rough).toprettyxml(indent="    ", newl="\n", encoding="utf-8").decode('utf-8')
    lines = [ln for ln in pretty.splitlines() if ln.strip()]
    out_path.write_text("\n".join(lines), encoding="utf-8")

def log_multiline(prefix: str, text: str):
    for line in text.splitlines():
        log.info(f"{prefix}{line}")

def choose_timebase(v_stream: Dict[str, Any]) -> Tuple[int, int, str]:
    afr = v_stream.get('avg_frame_rate')
    rfr = v_stream.get('r_frame_rate')
    sel = afr if afr and afr != "0/0" else (rfr if rfr and rfr != "0/0" else "24000/1001")
    try:
        num, den = map(int, sel.split('/'))
    except Exception:
        num, den = 24000, 1001
        sel = "24000/1001"
    # Snap near-23.976 to exact 24000/1001 to avoid drift
    if abs((num/den) - (24000/1001)) < 1e-6 or num*1001 == den*24000:
        num, den = 24000, 1001
        sel = "24000/1001"
    return num, den, sel

def nominal_fps(tb_num: int, tb_den: int) -> int:
    if tb_num == 24000 and tb_den == 1001: return 24
    if tb_num == 30000 and tb_den == 1001: return 30
    if tb_num == 60000 and tb_den == 1001: return 60
    return int(round(tb_num / tb_den))

def is_dropframe_tc(tc: str, tb_num: int, tb_den: int) -> bool:
    return (';' in tc) and ((tb_num, tb_den) in ((30000, 1001), (60000, 1001)))

def tmcd_to_frames(tc: str, tb_num: int, tb_den: int) -> int:
    """
    Convert HH:MM:SS:FF (or HH:MM:SS;FF for DF) to absolute frames using NOMINAL fps.
    Drop-frame rule: for 29.97 drop 2 frames/min (except every 10th); for 59.94 drop 4/min.
    """
    if not tc: return 0
    df = is_dropframe_tc(tc, tb_num, tb_den)
    p = tc.replace(';', ':').split(':')
    if len(p) != 4: return 0
    h, m, s, f = map(int, p)
    nfps = nominal_fps(tb_num, tb_den)
    total_minutes = 60*h + m
    base = (h * 3600 + m * 60 + s) * nfps + f
    if df:
        drop_per_min = 2 if nfps == 30 else (4 if nfps == 60 else 0)
        drops = drop_per_min * (total_minutes - total_minutes // 10)
        base -= drops
    return base

def format_name_and_frameDuration(tb_num: int, tb_den: int) -> Tuple[str, str]:
    rate = tb_num / tb_den
    if tb_num == 24000 and tb_den == 1001:
        return "FFVideoFormat1080p2398", "1001/24000s"
    if tb_num == 30000 and tb_den == 1001:
        return "FFVideoFormat1080p2997", "1001/30000s"
    if tb_num == 60000 and tb_den == 1001:
        return "FFVideoFormat1080p5994", "1001/60000s"
    # Generic fallback
    return f"FFVideoFormat1080p{rate:.2f}", f"{tb_den}/{tb_num}s"

def align_frames(values: List[int], mode: str = "median") -> int:
    """
    Robustly determines a single start frame from multiple camera angles.
    Modes: median (default), first, min, max, exact.
    """
    if not values:
        raise ValueError("align_frames(): empty values list provided.")
    
    if mode == "exact":
        if len(set(values)) > 1:
            raise ValueError(f"Start frames mismatch in 'exact' mode: {values}")
        return values[0]
    
    if mode == "first":
        return values[0]
    if mode == "min":
        return min(values)
    if mode == "max":
        return max(values)
    
    # Default to median for all other cases, as it's robust to outliers.
    return int(median(values))

def get_media_info(file_path: Path, ffprobe_path: str, ffprobe_out_dir: Path) -> Optional[Dict[str, Any]]:
    resolved_path = file_path.resolve()
    log.info("[ffprobe] Analyzing media: %s", resolved_path)
    try:
        cmd = [
            ffprobe_path, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(resolved_path),
        ]
        result_json = subprocess.check_output(cmd, text=True, encoding="utf-8")
        ffprobe_out_dir.mkdir(parents=True, exist_ok=True)
        out_json_path = ffprobe_out_dir / f"{file_path.stem}.ffprobe.json"
        out_json_path.write_text(result_json, encoding="utf-8")

        data = json.loads(result_json)
        v_stream = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
        if not v_stream:
            log.error("[ffprobe] No video stream in '%s'", resolved_path)
            return None

        tb_num, tb_den, rate_str = choose_timebase(v_stream)
        
        tmcd_stream = next((s for s in data["streams"] if s.get("codec_tag_string") == "tmcd" or s.get("codec_name") == "tmcd"), None)
        tc_source = "none"
        tc_value = None
        if tmcd_stream and (tmcd_stream.get("tags") or {}).get("timecode"):
            tc_value = tmcd_stream["tags"]["timecode"]; tc_source = "tmcd.tags.timecode"
        elif (v_stream.get("tags") or {}).get("timecode"):
            tc_value = v_stream["tags"]["timecode"]; tc_source = "video.tags.timecode"
        elif (data.get("format", {}).get("tags") or {}).get("timecode"):
            tc_value = data["format"]["tags"]["timecode"]; tc_source = "format.tags.timecode"

        if tc_value:
            start_frames_nominal = tmcd_to_frames(tc_value, tb_num, tb_den)
        else:
            st = v_stream.get("start_time") or data.get("format", {}).get("start_time") or 0
            start_frames_nominal = int(Fraction(str(st)) * Fraction(tb_num, tb_den))
        
        dur_frames: Optional[int] = None
        dur_sec = (data.get("format") or {}).get("duration")
        if dur_sec is not None:
            dur_frames = int(Fraction(str(dur_sec)) * Fraction(tb_num, tb_den))
        else:
            dur_frames = int(v_stream.get("nb_frames", 0))

        if dur_frames is None:
            dur_frames = 0
        
        audio_ch = next((s.get("channels", 0) for s in data["streams"] if s.get("codec_type") == "audio"), 0)

        return {
            "tb_num": tb_num,
            "tb_den": tb_den,
            "rate_str": rate_str,
            "start_frames_nominal": start_frames_nominal,
            "duration_frames": dur_frames,
            "audio_ch": audio_ch,
            "tc_source": tc_source,
            "tc_value": tc_value,
        }

    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        log.error("[ffprobe] Failed for '%s': %s", resolved_path, e, exc_info=True)
        return None

def parse_filename(filename: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"^(.*?)[\s_]?CAM[\s_]?(\d+)[\s_]?(\d+)", filename, re.IGNORECASE)
    if m:
        return {
            "subject": m.group(1).replace("_", " ").strip(),
            "camera_number": int(m.group(2)),
            "take_number": int(m.group(3))
        }
    return None

# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser(description="Build multicam FCPXML with nominal-fps TMCD, DF support, multi-MC, and start alignment.")
    parser.add_argument("--work-request-dir", required=True, type=Path)
    parser.add_argument("--projects-base-dir", required=True, type=Path)
    parser.add_argument("--shared-scripts-dir", required=True, type=Path)
    parser.add_argument("--ffprobe-path", default="ffprobe", type=str)
    parser.add_argument("--align-start", choices=["exact", "first", "min", "median", "max"], default="median",
                        help="How to unify angle starts within a take when TMCD differs.")
    parser.add_argument("--gap-frames", type=int, default=0,
                        help="Number of empty frames to insert between successive takes on the spine.")
    args = parser.parse_args()

    project_name = args.work_request_dir.name
    project_root = args.projects_base_dir.resolve() / project_name
    workspace_dir = project_root / DEFAULT_WORKSPACE_SUBDIR
    proxies_dir = project_root / "proxies"
    ffprobe_dump_dir = workspace_dir / "ffprobe"

    log.info(f"Initializing project '{project_name}' in the central config...")
    
    new_project_config = {
        "name": project_name,
        "base_project_directory": str(project_root),
        "workspace_subdir": DEFAULT_WORKSPACE_SUBDIR,
    }
    save_project_config(project_name, new_project_config)
    set_active_project_name(project_name)
    
    log.info(f"Project '{project_name}' has been configured and set as ACTIVE.")

    try:
        log.info("--- Station 1: Setup Project & Build Timeline ---")
        workspace_dir.mkdir(parents=True, exist_ok=True)
        proxies_dir.mkdir(exist_ok=True)
        ffprobe_dump_dir.mkdir(parents=True, exist_ok=True)

        video_files = sorted([f for f in args.work_request_dir.iterdir() if f.suffix.lower() in (".mp4", ".mov", ".braw")])
        if not video_files:
            raise FileNotFoundError(f"No video files found in: {args.work_request_dir}")

        all_clips = []
        for vf in video_files:
            meta = parse_filename(vf.name)
            info = get_media_info(vf, args.ffprobe_path, ffprobe_dump_dir)
            if not meta:
                log.warning(f"[skip] Name parse failed/unsupported for '{vf.name}' (expects ... CAM <num> <take>)")
            if not info:
                log.warning(f"[skip] Media info failed for '{vf.name}'")
            if meta and info:
                all_clips.append({"source_path": vf, **meta, **info})

        if not all_clips:
            raise ValueError("No valid media files could be processed.")

        # Load template and clear placeholders
        template_fcpxml_path = args.shared_scripts_dir / "template.fcpxml"
        tree = ET.parse(template_fcpxml_path)
        root = tree.getroot()
        resources = root.find('resources')
        sequence = root.find('.//sequence')
        spine = sequence.find('spine')
        if any(x is None for x in (resources, sequence, spine)):
            raise ValueError("Template missing required elements (resources/sequence/spine).")

        for e in list(resources): resources.remove(e)
        for e in list(spine): spine.remove(e)
        log.info("Cleared placeholder content from template.")

        # Timebase from first clip
        tb_num, tb_den = all_clips[0]['tb_num'], all_clips[0]['tb_den']
        fmt_name, frameDuration = format_name_and_frameDuration(tb_num, tb_den)
        format_el = ET.Element('format', id='r0', name=fmt_name, frameDuration=frameDuration, width="1920", height="1080")
        resources.append(format_el)

        # Sequence header (Frame-Locked)
        tc_start_frames = timecode_to_frames(1, 0, 0, tb_num, tb_den)
        production_tc_start_str = frames_to_fcpx(tc_start_frames, tb_num, tb_den)
        sequence.set('tcStart', production_tc_start_str)
        sequence.set('format', 'r0')
        log.info(f"[emit] <sequence tcStart='{production_tc_start_str}' ({tc_start_frames} frames)> format='r0'")
        
        by_take: Dict[int, list] = defaultdict(list)
        for c in all_clips:
            by_take[c['take_number']].append(c)

        manifest_sequence = []
        r_id = 1
        cumulative_offset_frames = 0
        
        for take_number in sorted(by_take.keys()):
            take_clips = sorted(by_take[take_number], key=lambda x: x['camera_number'])
            log.info(f"[take {take_number:02d}] Processing {len(take_clips)} angles with alignment mode '{args.align_start}'")

            starts_frames = [c['start_frames_nominal'] for c in take_clips]
            unified_start_frames = align_frames(starts_frames, args.align_start)
            log.info(f"[take {take_number:02d}] Original starts (frames): {starts_frames} -> Unified start: {unified_start_frames}")

            take_max_duration_frames = max(c['duration_frames'] for c in take_clips)
            
            # Create assets and build asset_ids map
            asset_ids: Dict[int, str] = {}
            for c in take_clips:
                asset_id = f"r{r_id}"; r_id += 1
                dst = (proxies_dir / c['source_path'].name).resolve()
                if not dst.exists() or dst.stat().st_mtime < c['source_path'].stat().st_mtime:
                     shutil.copy(str(c['source_path'].resolve()), str(dst))

                # The <asset> 'start' attribute MUST reflect the file's true embedded timecode.
                asset_start_str = frames_to_fcpx(c['start_frames_nominal'], tb_num, tb_den)
                asset_el = ET.Element(
                    'asset',
                    id=asset_id,
                    name=c['source_path'].name,
                    start=asset_start_str,
                    duration=frames_to_fcpx(c['duration_frames'], tb_num, tb_den),
                    hasVideo="1", format='r0', hasAudio="1", audioSources="1",
                    audioChannels=str(c['audio_ch'])
                )

                src_uri = dst.as_uri().replace('file:///', 'file://localhost/')
                ET.SubElement(asset_el, 'media-rep', kind="original-media", src=src_uri)
                resources.append(asset_el)
                asset_ids[c['camera_number']] = asset_id
                manifest_sequence.append({
                    "take_number": take_number, "camera_number": c['camera_number'], "asset_id": asset_id,
                    "source_filename": c['source_path'].name, "start_frames_original": c['start_frames_nominal'],
                    "start_frames_unified": unified_start_frames, "duration_frames": c['duration_frames'],
                    "tc_source": c.get("tc_source"), "tc_value": c.get("tc_value")
                })
                log.info(f"[emit] <asset id='{asset_id}' name='{c['source_path'].name}'> start={asset_start_str} (original)")

            # Multicam resource for this take
            multicam_media_id = f"r{r_id}"; r_id += 1
            multicam_name = f"{take_clips[0]['subject']} Take {take_number:02d} Multicam"
            unified_start_str = frames_to_fcpx(unified_start_frames, tb_num, tb_den)
            
            media_el = ET.Element('media', {'id': multicam_media_id, 'name': multicam_name})
            mcel = ET.SubElement(media_el, 'multicam', {'format': 'r0', 'tcStart': unified_start_str})

            default_angle_id = ''
            for c in take_clips:
                angle_id = str(uuid.uuid4())
                if c['camera_number'] == 1: default_angle_id = angle_id
                
                angle_el = ET.SubElement(mcel, 'mc-angle', {'name': f"Angle {c['camera_number']}", 'angleID': angle_id})
                
                # Calculate the offset needed to align this clip within the multicam container.
                # offset = clip's_start - multicam's_start
                angle_offset_frames = c['start_frames_nominal'] - unified_start_frames
                angle_offset_str = frames_to_fcpx(angle_offset_frames, tb_num, tb_den)
                
                ET.SubElement(angle_el, 'asset-clip',
                              ref=asset_ids[c['camera_number']],
                              name=c['source_path'].name,
                              duration=frames_to_fcpx(c['duration_frames'], tb_num, tb_den),
                              start=frames_to_fcpx(c['start_frames_nominal'], tb_num, tb_den),
                              offset=angle_offset_str,
                              format='r0', enabled="1", tcFormat="NDF")
                log.info(f"... angle {c['camera_number']} offset is {angle_offset_frames} frames ({angle_offset_str})")

            resources.append(media_el)
            log.info(f"[emit] <media id='{multicam_media_id}' name='{multicam_name}'> tcStart={unified_start_str}")

            # Timeline mc-clip for this take
            mc_offset_str = frames_to_fcpx(cumulative_offset_frames, tb_num, tb_den)
            take_dur_str = frames_to_fcpx(take_max_duration_frames, tb_num, tb_den)
            mc_clip_el = ET.Element('mc-clip', {
                'name': multicam_name, 'ref': multicam_media_id, 'offset': mc_offset_str,
                'duration': take_dur_str, 'start': unified_start_str
            })
            mc_source_el = ET.SubElement(mc_clip_el, 'mc-source', {'angleID': default_angle_id, 'srcEnable': 'all'})
            ET.SubElement(mc_source_el, 'adjust-transform', {'scale': '1 1', 'position': '0 0', 'anchor': '0 0'})
            spine.append(mc_clip_el)
            log.info(f"[emit] <mc-clip name='{multicam_name}'> offset={mc_offset_str} duration={take_dur_str}")

            cumulative_offset_frames += take_max_duration_frames
            if args.gap_frames > 0:
                cumulative_offset_frames += args.gap_frames

        # Finalize sequence duration
        total_content_frames = cumulative_offset_frames
        if args.gap_frames > 0 and len(by_take) > 0:
            total_content_frames -= args.gap_frames

        total_content_frames = max(0, total_content_frames)
        sequence.set('duration', frames_to_fcpx(total_content_frames, tb_num, tb_den))
        log.info(f"Final sequence duration updated to {total_content_frames} frames.")

        # Write manifest & FCPXML
        manifest_sequence.sort(key=lambda x: (x['take_number'], x['camera_number']))
        manifest = {
            "project": project_name, "frame_rate": f"{tb_num}/{tb_den}",
            "align_mode": args.align_start, "gap_frames": args.gap_frames,
            "clips": manifest_sequence
        }
        manifest_path = workspace_dir / "asset_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log.info(f"[emit] Wrote asset manifest: {manifest_path}")

        final_fcpxml_path = project_root / "Info.fcpxml"
        pretty_write_xml(tree, final_fcpxml_path)
        log.info(f"[emit] Wrote FCPXML: {final_fcpxml_path}")

        (args.work_request_dir / ".project_ready_for_assembly").touch()
        log.info("--- Project Intake and Build Complete ---")

    except Exception as e:
        log.error(f"A critical error occurred for '{project_name}': {e}", exc_info=True)
        (args.work_request_dir / ".setup_failed").touch()
        sys.exit(1)

if __name__ == "__main__":
    main()