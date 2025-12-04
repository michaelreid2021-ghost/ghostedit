#!/usr/bin/env python3
# 002_setup_project.py (v18.7 - Restore v17 Start Logic, Fix nominal_fps)
# Last modified: 2025-11-07
# Purpose: Build clean-takes FCPXML for 23.976 .braw/.mov inputs using Jinja template.
# - ...
# - v18.5: Restored robust multi-location timecode parsing from v17.
# - v18.6: Added .strip() to rendered XML string to prevent whitespace parse error.
# - v18.7: Fixed nominal_fps to use round().
# - v18.7: Restored v17 start_frames logic (fallback to v_stream start_time).

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
from typing import Optional, Dict, Any, List
import xml.etree.ElementTree as ET
from xml.dom import minidom
from jinja2 import Template  # User can install if needed: pip install jinja2
from project_paths import save_project_config, set_active_project_name

# --- Constants ---
REVISION = "v18.7 - Restore v17 Start Logic, Fix nominal_fps"
DEFAULT_WORKSPACE_SUBDIR = "ai_working_files"
DEFAULT_ALIGN_MODE = "median"
DEFAULT_GAP_FRAMES = 0
TC_START_STR = "18018/5s"  # Industry standard 1-hour offset at 23.976 NDF

# --- Jinja Template String (strictly based on user's template, looped for multiple takes) ---
TEMPLATE_STR = """
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
    <resources>
        <format height="1080" name="FFVideoFormat1080p2398" width="1920" frameDuration="{{ frame_duration }}" id="r0"/>
        {% for take in takes %}
        <asset name="{{ take.cam1.filename }}" format="r0" start="{{ take.cam1.start }}s" hasVideo="1" audioSources="1" id="{{ take.cam1.id }}" duration="{{ take.cam1.duration }}s" hasAudio="1" audioChannels="2">
            <media-rep kind="original-media" src="{{ take.cam1.src }}"/>
        </asset>
        <asset name="{{ take.cam2.filename }}" format="r0" start="{{ take.cam2.start }}s" hasVideo="1" audioSources="1" id="{{ take.cam2.id }}" duration="{{ take.cam2.duration }}s" hasAudio="1" audioChannels="2">
            <media-rep kind="original-media" src="{{ take.cam2.src }}"/>
        </asset>
        <media name="{{ take.name }}" id="{{ take.media_id }}">
            <multicam tcFormat="NDF" format="r0" tcStart="{{ take.start }}s">
                <mc-angle name="Angle 1" angleID="{{ take.angle1_id }}">
                    <asset-clip name="{{ take.cam1.filename }}" tcFormat="NDF" format="r0" offset="0/1s" start="{{ take.cam1.start }}s" enabled="1" duration="{{ take.max_duration }}s" ref="{{ take.cam1.id }}">
                        <adjust-transform scale="1 1" anchor="0 0" position="0 0"/>
                    </asset-clip>
                </mc-angle>
                <mc-angle name="Angle 2" angleID="{{ take.angle2_id }}">
                    <asset-clip name="{{ take.cam2.filename }}" tcFormat="NDF" format="r0" offset="0/1s" start="{{ take.cam2.start }}s" enabled="1" duration="{{ take.max_duration }}s" ref="{{ take.cam2.id }}">
                        <adjust-transform scale="1 1" anchor="0 0" position="0 0"/>
                    </asset-clip>
                </mc-angle>
            </multicam>
        </media>
        {% endfor %}
    </resources>
    <library>
        <event name="Timeline 1 (Resolve)">
            <project name="Timeline 1 (Resolve)">
                <sequence tcFormat="NDF" format="r0" duration="{{ total_duration }}s" tcStart="{{ tc_start }}">
                    <spine>
                        {% for take in takes %}
                        <mc-clip name="{{ take.name }}" offset="{{ take.offset }}s" start="{{ take.start }}s" duration="{{ take.max_duration }}s" ref="{{ take.media_id }}">
                            <mc-source srcEnable="all" angleID="{{ take.angle1_id }}">
                                <adjust-transform scale="1 1" anchor="0 0" position="0 0"/>
                            </mc-source>
                        </mc-clip>
                        {% endfor %}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""

# ---------- Logger Setup (Overwrite mode, verbose) ----------
script_directory = Path(__file__).resolve().parent
script_name = Path(__file__).stem
log_file_path = script_directory / f"{script_name}.log"
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s")
log = logging.getLogger(script_name)
log.setLevel(logging.INFO)
# Overwrite log file each run
fh = logging.FileHandler(log_file_path, mode='w', encoding='utf-8')
fh.setFormatter(log_formatter)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(log_formatter)
log.addHandler(fh)
log.addHandler(sh)

log.info(f"Starting {REVISION}")

# ---------- Utilities ----------
def frames_to_fcpx(frames: int, tb_num: int, tb_den: int) -> str:
    num = frames * tb_den
    den = tb_num
    f = Fraction(num, den)
    result = f"{f.numerator}/{f.denominator}s"
    log.info(f"frames_to_fcpx: frames={frames}, tb_num={tb_num}, tb_den={tb_den} -> {result}")
    return result

def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if not s.endswith('s'):
        raise ValueError(f"Bad FCPXML time: {s}")
    body = s[:-1]
    if '/' in body:
        n, d = body.split('/')
        f = Fraction(int(n), int(d))
    else:
        f = Fraction(int(body), 1)
    log.info(f"parse_fcpx_frac: {s} -> {f}")
    return f

def frac_to_frames(frac_sec: Fraction, tb_num: int, tb_den: int) -> int:
    x = frac_sec * Fraction(tb_num, tb_den)
    result = max(0, x.numerator // x.denominator)  # No rounding, use floor division
    log.info(f"frac_to_frames: frac_sec={frac_sec}, tb_num={tb_num}, tb_den={tb_den} -> {result}")
    return result

def pretty_write_xml(tree: ET.ElementTree, out_path: Path):
    rough = ET.tostring(tree.getroot(), encoding='unicode')
    log.info(f"Full XML before pretty-write:\n{rough}")
    pretty = minidom.parseString(rough.encode('utf-8')).toprettyxml(indent="    ", newl="\n", encoding="utf-8").decode('utf-8')
    lines = [ln for ln in pretty.splitlines() if ln.strip()]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Wrote pretty XML to {out_path}")

def nominal_fps(tb_num: int, tb_den: int) -> int:
    # --- FIX v18.7 ---
    # Use round() instead of // to get correct nominal FPS (e.g., 24, not 23)
    result = round(tb_num / tb_den)
    log.info(f"nominal_fps: tb_num={tb_num}, tb_den={tb_den} -> {result}")
    return result

def tmcd_to_frames(tc: str, tb_num: int, tb_den: int) -> int:
    if not tc:
        # --- FIX v18.7 ---
        # Match v17 behavior: return 0 instead of raising error,
        # to allow fallback logic to work.
        log.info(f"tmcd_to_frames: No timecode provided, returning 0.")
        return 0
    df = ';' in tc and (tb_num, tb_den) in ((30000, 1001), (60000, 1001))
    p = tc.replace(';', ':').split(':')
    if len(p) != 4:
        # --- FIX v18.7 ---
        # Match v17 behavior: return 0 instead of raising error.
        log.warning(f"Invalid timecode format: {tc}, returning 0.")
        return 0
    h, m, s, f = map(int, p)
    nfps = nominal_fps(tb_num, tb_den)
    total_minutes = 60 * h + m
    base = (h * 3600 + m * 60 + s) * nfps + f
    if df:
        drop_per_min = 2 if nfps == 30 else (4 if nfps == 60 else 0)
        drops = drop_per_min * (total_minutes - total_minutes // 10)
        base -= drops
    log.info(f"tmcd_to_frames: tc={tc}, df={df}, nfps={nfps}, base={base}")
    return base

def align_frames(values: List[int], mode: str = "median") -> int:
    if not values:
        raise ValueError("align_frames(): empty values list provided.")
    log.info(f"align_frames: values={values}, mode={mode}")
    if mode == "exact":
        if len(set(values)) > 1:
            raise ValueError(f"Start frames mismatch in 'exact' mode: {values}")
        return values[0]
    if mode == "first": return values[0]
    if mode == "min": return min(values)
    if mode == "max": return max(values)
    result = int(median(values))
    log.info(f"align_frames result: {result}")
    return result

def get_media_info(file_path: Path, ffprobe_path: str, ffprobe_out_dir: Path, max_duration_seconds: Optional[float] = None) -> Dict[str, Any]:
    resolved_path = file_path.resolve()
    log.info(f"[ffprobe] Analyzing: {resolved_path}")
    cmd = [ffprobe_path, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(resolved_path)]
    log.info(f"ffprobe cmd: {' '.join(cmd)}")
    result_json = subprocess.check_output(cmd, text=True, encoding="utf-8")
    log.info(f"ffprobe raw output: {result_json}")
    ffprobe_out_dir.mkdir(parents=True, exist_ok=True)
    out_json_path = ffprobe_out_dir / f"{file_path.stem}.ffprobe.json"
    out_json_path.write_text(result_json, encoding="utf-8")
    data = json.loads(result_json)
    v_stream = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if not v_stream:
        raise ValueError("[ffprobe] No video stream")
    if "nb_frames" not in v_stream:
        raise ValueError("[ffprobe] Missing nb_frames in video stream")
    r_frame_rate = v_stream["r_frame_rate"]
    if '/' not in r_frame_rate:
        raise ValueError(f"[ffprobe] Invalid r_frame_rate: {r_frame_rate}")
    tb_num_str, tb_den_str = r_frame_rate.split('/')
    tb_num, tb_den = int(tb_num_str), int(tb_den_str)
    duration_frames = int(v_stream["nb_frames"])
    
    # Robust timecode finding logic
    tmcd_stream = next((s for s in data["streams"] if s.get("codec_tag_string") == "tmcd" or s.get("codec_name") == "tmcd"), None)
    tc_source, tc_value = "none", None

    if tmcd_stream and (tmcd_stream.get("tags") or {}).get("timecode"):
        tc_value, tc_source = tmcd_stream["tags"]["timecode"], "tmcd.tags.timecode"
    elif (v_stream.get("tags") or {}).get("timecode"):
        tc_value, tc_source = v_stream["tags"]["timecode"], "video.tags.timecode"
    elif (data.get("format", {}).get("tags") or {}).get("timecode"):
        tc_value, tc_source = data["format"]["tags"]["timecode"], "format.tags.timecode"
    elif data.get("format", {}).get("start_timecode"):
        tc_value, tc_source = data["format"]["start_timecode"], "format.start_timecode"

    # --- FIX v18.7 ---
    # Restore v17 logic: Use tc_value if it exists, OTHERWISE
    # fall back to the video stream's start_time.
    if tc_value:
        log.info(f"Found timecode '{tc_value}' from {tc_source}. Using tmcd_to_frames.")
        start_frames = tmcd_to_frames(tc_value, tb_num, tb_den)
    else:
        stream_start_time_str = str(v_stream.get("start_time", 0))
        log.info(f"No valid timecode found. Falling back to v_stream start_time: '{stream_start_time_str}'.")
        start_frames = int(Fraction(stream_start_time_str) * Fraction(tb_num, tb_den))
    # --- End FIX v18.7 ---

    if max_duration_seconds is not None:
        max_frames_frac = Fraction(max_duration_seconds) * Fraction(tb_num, tb_den)
        max_frames = max_frames_frac.numerator // max_frames_frac.denominator
        duration_frames = min(duration_frames, max_frames)
        log.info(f"Capped duration: original_frames={int(v_stream['nb_frames'])}, capped_frames={duration_frames}")
    audio_ch = next((s.get("channels", 0) for s in data["streams"] if s.get("codec_type") == "audio"), 0)
    result = {
        "start_frames_nominal": start_frames,
        "duration_frames": duration_frames,
        "tb_num": tb_num, "tb_den": tb_den,
        "tc_source": tc_source, "tc_value": tc_value,
        "audio_ch": audio_ch
    }
    log.info(f"get_media_info result: {result}")
    return result

# ---------- Main ----------
def main():
    log.info(f"Parsing args")
    parser = argparse.ArgumentParser(description="Build clean-takes FCPXML for 23.976 .braw/.mov inputs.")
    parser.add_argument('--work-request-dir', required=True, type=Path)
    parser.add_argument('--projects-base-dir', required=True, type=Path)
    parser.add_argument('--shared-scripts-dir', required=True, type=Path)
    parser.add_argument('--align-start', default=DEFAULT_ALIGN_MODE, choices=["exact", "first", "min", "median", "max"])
    parser.add_argument('--gap-frames', default=DEFAULT_GAP_FRAMES, type=int)
    parser.add_argument('--max-duration-seconds', type=float, default=None, help="Cap clip duration in seconds")
    args = parser.parse_args()
    log.info(f"Args: {vars(args)}")

    project_name = args.work_request_dir.name
    log.info(f"Project name: {project_name}")
    project_root = args.projects_base_dir / project_name
    workspace_dir = project_root / DEFAULT_WORKSPACE_SUBDIR
    ffprobe_out_dir = workspace_dir / "ffprobe_dumps"
    proxies_dir = project_root / "proxies"
    ffprobe_path = args.shared_scripts_dir / "ffmpeg" / "ffprobe.exe"  # Adjust if needed
    log.info(f"Paths: project_root={project_root}, workspace_dir={workspace_dir}, ffprobe_out_dir={ffprobe_out_dir}, proxies_dir={proxies_dir}, ffprobe_path={ffprobe_path}")

    try:
        log.info("Creating directories")
        project_root.mkdir(parents=True, exist_ok=True)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        proxies_dir.mkdir(parents=True, exist_ok=True)
        ffprobe_out_dir.mkdir(parents=True, exist_ok=True)

        # Fix for save_project_config: Add config dict as per v17
        new_project_config = {
            "name": project_name,
            "base_project_directory": str(project_root),
            "workspace_subdir": DEFAULT_WORKSPACE_SUBDIR
        }
        save_project_config(project_name, new_project_config)
        set_active_project_name(project_name)

        # Scan clips
        log.info("Scanning for clips")
        all_clips: List[Dict] = []
        tb_num = None
        tb_den = None
        for source_path in sorted(args.work_request_dir.glob("*.[bB][rR][aA][wW]")) + sorted(args.work_request_dir.glob("*.[mM][oO][vV]")):
            log.info(f"Found file: {source_path}")
            # Match against the full .name, not the .stem, as the regex includes the extension.
            match = re.match(r'(.+?) CAM (\d+) (\d+)(?:_HD)?\.(braw|mov)', source_path.name, re.IGNORECASE)
            if not match:
                log.info(f"Skipping non-matching file: {source_path}")
                continue
            subject, cam_num, take_num, ext = match.groups()
            log.info(f"Matched: subject={subject}, cam_num={cam_num}, take_num={take_num}, ext={ext}")
            info = get_media_info(source_path, str(ffprobe_path), ffprobe_out_dir, args.max_duration_seconds)
            if tb_num is None:
                tb_num, tb_den = info["tb_num"], info["tb_den"]
            elif (info["tb_num"], info["tb_den"]) != (tb_num, tb_den):
                raise ValueError(f"Timebase mismatch: {source_path} has {info['tb_num']}/{info['tb_den']}, expected {tb_num}/{tb_den}")
            clip = {
                'source_path': source_path,
                'subject': subject,
                'camera_number': int(cam_num),
                'take_number': int(take_num),
                **info
            }
            log.info(f"Added clip: {clip}")
            all_clips.append(clip)

        if not all_clips:
            raise RuntimeError(f"002 No valid clips found in work request dir. all_clips:{all_clips} ")

        # Group by take
        by_take: Dict[int, list] = defaultdict(list)
        for c in all_clips:
            by_take[c['take_number']].append(c)
        log.info(f"Takes grouped: {list(by_take.keys())}")

        # Calculate initial pad frames
        pad_frac = parse_fcpx_frac(TC_START_STR)
        pad_frames = frac_to_frames(pad_frac, tb_num, tb_den)
        log.info(f"Pad frames for 1-hour offset: {pad_frames}")

        # Prepare takes data for Jinja
        takes: List[Dict] = []
        r_id = 1
        cumulative_offset_frames = pad_frames  # Start offsets at pad
        manifest_sequence = []
        frame_duration = f"{tb_den}/{tb_num}s"
        for take_number in sorted(by_take.keys()):
            take_clips = sorted(by_take[take_number], key=lambda x: x['camera_number'])
            if len(take_clips) != 2 or take_clips[0]['camera_number'] != 1 or take_clips[1]['camera_number'] != 2:
                raise ValueError(f"Take {take_number}: Expected exactly CAM 1 and CAM 2")
            log.info(f"[take {take_number:02d}] Clips: {len(take_clips)}, alignment={args.align_start}")

            starts_frames = [c['start_frames_nominal'] for c in take_clips]
            unified_start_frames = align_frames(starts_frames, args.align_start)
            unified_start_str = frames_to_fcpx(unified_start_frames, tb_num, tb_den)[:-1]  # without 's'

            take_max_duration_frames = max(c['duration_frames'] for c in take_clips)
            max_duration_str = frames_to_fcpx(take_max_duration_frames, tb_num, tb_den)[:-1]

            subject = take_clips[0]['subject']
            name = f"{subject} CAM 1 {take_number:02d}_HD Multicam"

            angle1_id = str(uuid.uuid4())
            angle2_id = str(uuid.uuid4())

            cam1 = take_clips[0]
            cam1_id = f"r{r_id}"; r_id += 1
            cam1_start_str = frames_to_fcpx(cam1['start_frames_nominal'], tb_num, tb_den)[:-1]
            cam1_duration_str = frames_to_fcpx(cam1['duration_frames'], tb_num, tb_den)[:-1]
            cam1_filename = cam1['source_path'].name
            dst1 = (proxies_dir / cam1_filename).resolve()
            if not dst1.exists() or dst1.stat().st_mtime < cam1['source_path'].stat().st_mtime:
                log.info(f"Copying {cam1['source_path']} to {dst1}")
                shutil.copy(str(cam1['source_path'].resolve()), str(dst1))
            cam1_src = dst1.as_uri().replace('file:///', 'file://localhost/')

            cam2 = take_clips[1]
            cam2_id = f"r{r_id}"; r_id += 1
            cam2_start_str = frames_to_fcpx(cam2['start_frames_nominal'], tb_num, tb_den)[:-1]
            cam2_duration_str = frames_to_fcpx(cam2['duration_frames'], tb_num, tb_den)[:-1]
            cam2_filename = cam2['source_path'].name
            dst2 = (proxies_dir / cam2_filename).resolve()
            if not dst2.exists() or dst2.stat().st_mtime < cam2['source_path'].stat().st_mtime:
                log.info(f"Copying {cam2['source_path']} to {dst2}")
                shutil.copy(str(cam2['source_path'].resolve()), str(dst2))
            cam2_src = dst2.as_uri().replace('file:///', 'file://localhost/')

            media_id = f"r{r_id}"; r_id += 1

            offset_str = frames_to_fcpx(cumulative_offset_frames, tb_num, tb_den)[:-1]

            take_dict = {
                'name': name,
                'start': unified_start_str,
                'max_duration': max_duration_str,
                'media_id': media_id,
                'angle1_id': angle1_id,
                'angle2_id': angle2_id,
                'cam1': {
                    'id': cam1_id,
                    'filename': cam1_filename,
                    'start': cam1_start_str,
                    'duration': cam1_duration_str,
                    'src': cam1_src
                },
                'cam2': {
                    'id': cam2_id,
                    'filename': cam2_filename,
                    'start': cam2_start_str,
                    'duration': cam2_duration_str,
                    'src': cam2_src
                },
                'offset': offset_str
            }
            takes.append(take_dict)

            # Manifest entries
            for c, cam_key in zip(take_clips, ['cam1', 'cam2']):
                manifest_entry = {
                    "take_number": take_number, "camera_number": c['camera_number'], "asset_id": take_dict[cam_key]['id'],
                    "source_filename": c['source_path'].name, "start_frames_original": c['start_frames_nominal'],
                    "start_frames_unified": unified_start_frames, "duration_frames": c['duration_frames'],
                    "tc_source": c.get("tc_source"), "tc_value": c.get("tc_value")
                }
                log.info(f"Manifest entry: {manifest_entry}")
                manifest_sequence.append(manifest_entry)

            cumulative_offset_frames += take_max_duration_frames
            log.info(f"Updated cumulative_offset_frames after take {take_number}: {cumulative_offset_frames}")
            if args.gap_frames > 0:
                cumulative_offset_frames += args.gap_frames
                log.info(f"Added gap: new cumulative_offset_frames={cumulative_offset_frames}")

        # Adjust total frames (content only, exclude pad and extra gaps)
        total_content_frames = cumulative_offset_frames - pad_frames
        if args.gap_frames > 0 and len(takes) > 1:
            total_content_frames -= args.gap_frames * (len(takes) - 1)
        total_duration_str = frames_to_fcpx(total_content_frames, tb_num, tb_den)[:-1]
        log.info(f"Final sequence duration frames={total_content_frames}, str={total_duration_str}s")

        # Render XML with Jinja
        log.info("Rendering XML with Jinja")
        template = Template(TEMPLATE_STR)
        xml_str = template.render(
            takes=takes,
            total_duration=total_duration_str,
            tc_start=TC_START_STR,
            frame_duration=frame_duration
        )
        log.info(f"Rendered raw XML:\n{xml_str}")

        # Pretty-write XML
        # Strip leading/trailing whitespace (like newlines) from the rendered
        # template before passing it to the XML parser.
        tree = ET.ElementTree(ET.fromstring(xml_str.strip()))
        final_fcpxml_path = project_root / "Info.fcpxml"
        pretty_write_xml(tree, final_fcpxml_path)

        # Manifest & Touch File
        manifest_sequence.sort(key=lambda x: (x['take_number'], x['camera_number']))
        manifest = {
            "project": project_name, "frame_rate": f"{tb_num}/{tb_den}",
            "align_mode": args.align_start, "gap_frames": args.gap_frames,
            "clips": manifest_sequence
        }
        manifest_path = workspace_dir / "asset_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log.info(f"Wrote manifest: {manifest_path}")

        (args.work_request_dir / ".project_ready_for_assembly").touch()
        log.info("--- Project Build Complete ---")

    except Exception as e:
        log.error(f"Error for '{project_name}': {e}", exc_info=True)
        (args.work_request_dir / ".setup_failed").touch()
        sys.exit(1)

if __name__ == "__main__":
    main()