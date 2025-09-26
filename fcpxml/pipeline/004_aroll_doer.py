#!/usr/bin/env python3
#004_aroll_doer.py
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
import copy
from pathlib import Path
from fractions import Fraction
from xml.dom import minidom
import xml.etree.ElementTree as ET
from typing import List, Tuple


# --- Setup based on self-location ---
# All scripts are co-located, this is the root for all paths.
script_directory = Path(__file__).resolve().parent
# Assume project_paths is also in the same directory or a reachable path
from project_paths import ProjectPaths

# --- Logging Setup ---
log_file_path = script_directory / "004_aroll_doer.log"
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    logger.addHandler(stream_handler)

# --- FCPXML & Time Utilities ---
def parse_fcpx_frac(s: str) -> Fraction:
    s = s.strip()
    if not s.endswith('s'): raise ValueError(f"Invalid FCPXML time format: {s}")
    body = s[:-1]
    if '/' in body: n, d = body.split('/'); return Fraction(int(n), int(d))
    return Fraction(body)

def frames_to_fcpx_frac(frames: int, tb_num: int, tb_den: int) -> str:
    f = Fraction(frames * tb_den, tb_num)
    return f"{f.numerator}/{f.denominator}s"

def frac_to_frames(frac_sec: Fraction, tb_num: int, tb_den: int) -> int:
    x = frac_sec * Fraction(tb_num, tb_den)
    return int(round(x.numerator / x.denominator))

def timebase_from_sequence(resources_root: ET.Element, seq_elem: ET.Element) -> Tuple[int, int]:
    fmt_id = seq_elem.get('format')
    if not fmt_id: raise RuntimeError("Sequence missing 'format' attribute.")
    fmt = resources_root.find(f".//format[@id='{fmt_id}']")
    if fmt is None: raise RuntimeError(f"Could not find <format id='{fmt_id}'> in resources.")
    fd = fmt.get('frameDuration')
    if not fd: raise RuntimeError("Format missing 'frameDuration'.")
    frac = parse_fcpx_frac(fd)
    return (frac.denominator, frac.numerator)

def get_max_resource_id(resources_root: ET.Element) -> int:
    max_id = 0
    for el in resources_root.findall(".//*[@id]"):
        rid = el.get('id', '')
        m = re.match(r'r(\d+)', rid)
        if m: max_id = max(max_id, int(m.group(1)))
    return max_id

def pretty_write_xml(tree: ET.ElementTree, out_path: Path):
    rough_string = ET.tostring(tree.getroot(), 'utf-8')
    reparsed = minidom.parseString(rough_string)
    ugly_xml = reparsed.toprettyxml(indent="    ", newl="\n", encoding="utf-8")
    text_lines = [line for line in ugly_xml.decode('utf-8').split('\n') if line.strip()]
    clean_xml = '\n'.join(text_lines)
    out_path.write_text(clean_xml, encoding="utf-8")

# --- Core Logic Functions ---

def run_pipeline_step(command: List[str]):
    """Runs a command as a subprocess, logging and raising errors."""
    logger.info(f"Executing: {' '.join(command)}")
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.stdout:
            logger.info(f"STDOUT: {result.stdout}")
        if result.stderr:
            logger.warning(f"STDERR: {result.stderr}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Pipeline step FAILED: {' '.join(command)}")
        logger.error(f"Return Code: {e.returncode}")
        logger.error(f"STDOUT: {e.stdout}")
        logger.error(f"STDERR: {e.stderr}")
        raise

def build_initial_multicam_timeline(p: ProjectPaths):
    """Reads asset_manifest and bootstrapped FCPXML to build the initial multicam timeline."""
    logger.info("--- Building Initial Multicam Timeline ---")
    manifest_path = p.workspace / "asset_manifest.json"
    fcpxml_path = p.info_fcpxml
    proxies_dir = p.proxies

    if not manifest_path.exists() or not fcpxml_path.exists():
        raise FileNotFoundError(f"Missing manifest ({manifest_path}) or fcpxml ({fcpxml_path})")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clips_by_take = {}
    for clip in manifest['clip_sequence']:
        take = clip['take_number']
        if take not in clips_by_take:
            clips_by_take[take] = []
        clips_by_take[take].append(clip)

    tree = ET.parse(fcpxml_path)
    root = tree.getroot()
    resources = root.find('.//resources')
    spine = root.find('.//library/event/project/sequence/spine')
    sequence = root.find('.//library/event/project/sequence')
    if resources is None or spine is None or sequence is None:
        raise ValueError("Invalid FCPXML structure in Info.fcpxml")

    tb_num, tb_den = timebase_from_sequence(resources, sequence)
    for element in list(spine):
        spine.remove(element)
    logger.info("Cleared placeholder content from timeline spine.")

    for take_number in sorted(clips_by_take.keys()):
        logger.info(f"Processing Take {take_number}...") 
        cam1_clip = next((c for c in clips_by_take[take_number] if c['camera_number'] == 1), None)
        cam2_clip = next((c for c in clips_by_take[take_number] if c['camera_number'] == 2), None)

        if not cam1_clip or not cam2_clip:
            logger.warning(f"Skipping Take {take_number}, missing a camera pair.")
            continue
        
        cam2_proxy_path = proxies_dir / cam2_clip['source_filename']
        ignore_file_path = cam2_proxy_path.with_suffix(cam2_proxy_path.suffix + '.ai_ignore')
        logger.info(f"Creating ignore file for angle 2 proxy: {ignore_file_path}")
        ignore_file_path.touch(exist_ok=True)
        
        max_r_id = get_max_resource_id(resources)
        new_media_id = f"r{max_r_id + 1}"
        multicam_name = f"Take_{take_number:02d}_Multicam"
        asset1_format = resources.find(f".//asset[@id='{cam1_clip['asset_id']}']").get('format')
        media_el = ET.Element('media', {'id': new_media_id, 'name': multicam_name})
        multicam_el = ET.SubElement(media_el, 'multicam', {'format': asset1_format, 'tcFormat': 'NDF'})

        default_angle_id = str(uuid.uuid4())
        angle1 = ET.SubElement(multicam_el, 'mc-angle', {'name': 'Angle 1', 'angleID': default_angle_id})
        ET.SubElement(angle1, 'asset-clip', {'ref': cam1_clip['asset_id'], 'name': cam1_clip['source_filename'], 'duration': f"{int(cam1_clip['duration_sec'] * 24000)}/24000s", 'format': asset1_format})
        
        angle2_id = str(uuid.uuid4())
        angle2 = ET.SubElement(multicam_el, 'mc-angle', {'name': 'Angle 2', 'angleID': angle2_id})
        ET.SubElement(angle2, 'asset-clip', {'ref': cam2_clip['asset_id'], 'name': cam2_clip['source_filename'], 'duration': f"{int(cam2_clip['duration_sec'] * 24000)}/24000s", 'format': asset1_format})
        
        resources.append(media_el)

        total_duration_frames = frac_to_frames(parse_fcpx_frac(sequence.get('duration', '0s')), tb_num, tb_den)
        max_duration_sec = max(cam1_clip['duration_sec'], cam2_clip['duration_sec'])
        clip_duration_frames = frac_to_frames(parse_fcpx_frac(f"{int(max_duration_sec * 24000)}/24000s"), tb_num, tb_den)
        clip_attrs = {'name': multicam_name, 'ref': new_media_id, 'offset': frames_to_fcpx_frac(total_duration_frames, tb_num, tb_den), 'duration': frames_to_fcpx_frac(clip_duration_frames, tb_num, tb_den), 'start': '0s'}
        mc_clip_el = ET.Element('mc-clip', clip_attrs)
        mc_source_el = ET.SubElement(mc_clip_el, 'mc-source', {'angleID': default_angle_id, 'srcEnable': 'all'})
        ET.SubElement(mc_source_el, 'adjust-transform', {'scale': '1 1', 'position': '0 0', 'anchor': '0 0'})
        spine.append(mc_clip_el)

        sequence.set('duration', frames_to_fcpx_frac(total_duration_frames + clip_duration_frames, tb_num, tb_den))
        logger.info(f"Added fully-formed mc-clip for Take {take_number} to timeline.") 

        pretty_write_xml(tree, fcpxml_path)
        logger.info(f"Saved final FCPXML with {len(clips_by_take)} takes to {fcpxml_path.resolve()}") 

def main():
    # A "doer" script only needs to know which project it's working on.
    parser = argparse.ArgumentParser(description="Station 2 Doer: Assembles A-Roll and runs the main AI pipeline.")
    parser.add_argument("--project-dir", required=True, type=Path, help="The path to the project directory.")
    args = parser.parse_args()

    # Establish the context for this specific project run.
    p = ProjectPaths(base=args.project_dir, workspace=(args.project_dir / "ai_working_files"))

    try:
        logger.info(f"=== Station 2: A-Roll Doer Started for {args.project_dir.name} ===") 


        
        # The following scripts are now context-aware and don't need arguments
        # They are all expected to be in the same directory as this script.
        pipeline_scripts = [
            "01_dehydrate.py",
            "03c_whisperx_clean_inout.py",
            "04a_waveform_annotator.py",
            "40_classify_takes.py",
            "44_producers_markup.py",
        ]

        for script_name in pipeline_scripts:
            # Construct the command assuming co-location of all scripts.
            command = [sys.executable, str(script_directory / script_name)]
            run_pipeline_step(command)

        # Step 2: Build the final edited timeline using the markup
        logger.info("--- Rebuilding timeline from clean takes (115 logic) ---")
        markup_path = p.workspace / "44_producers_markup.json"
        if not markup_path.exists():
            raise FileNotFoundError("Producers markup file not found, cannot build final timeline.")
        
        markup_data = json.loads(markup_path.read_text(encoding='utf-8'))
        shutil.copy(p.info_fcpxml, p.base / "Info.full_multicam_pre-edit.fcpxml")

        timeline_builder_script = script_directory / "115_timeline_builder.py"
        final_timeline_path = p.info_fcpxml
        
        source_refs_in_markup = {scene.get('multicam_id') for scene in markup_data if scene.get('multicam_id')}
        
        for source_ref in source_refs_in_markup:
             scene = next((s for s in markup_data if s.get('multicam_id') == source_ref), None)
             if not scene: continue
             final_takes = scene.get("producers_markup", {}).get("final_edit_takes", [])
             if not final_takes:
                 logger.warning(f"No final_edit_takes found for {source_ref}.")

             for j, take in enumerate(final_takes):
                 subclip_name = f"{scene.get('multicam_name', 'take')} - Subclip {j + 1}"
                 start_sec = float(take['start_seconds'])
                 end_sec = float(take['end_seconds'])
                 duration_sec = end_sec - start_sec

                 command = [
                     sys.executable, str(timeline_builder_script),
                     "--fcpxml", str(final_timeline_path),
                     "--out", str(final_timeline_path),
                     "process-take", "--source-ref", source_ref,
                     "--start-seconds", str(start_sec),
                     "--duration-seconds", str(duration_sec),
                     "--name", subclip_name
                 ]
                 run_pipeline_step(command)

        logger.info("=== Station 2: A-Roll Doer Finished Successfully ===")

    except Exception as e:
        logger.error(f"A-Roll Doer FAILED for {args.project_dir.name}. Error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()