#!/usr/bin/env python3
# 004_aroll_doer.py (v2.4.0 - UTF Scrubbed & Ghost Buster)
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
from typing import List, Tuple, Dict, Any

# --- FORCE UTF-8 OUTPUT (Windows Fix) ---
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# --- Setup based on self-location ---
script_directory = Path(__file__).resolve().parent
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

# --- Scrubbing Utility ---
def scrub_text(text: str) -> str:
    """
    Sanitizes text to prevent UTF/Encoding crashes in subprocess or XML.
    Replaces smart quotes, em-dashes, and strips non-printable chars.
    """
    if not text:
        return ""
    
    # Map common "Word" characters to ASCII safe versions
    replacements = {
        "\u201c": '"', "\u201d": '"',  # Smart Quotes
        "\u2018": "'", "\u2019": "'",  # Smart Single Quotes
        "\u2013": "-", "\u2014": "-",  # Dashes
        "\u2026": "...",               # Ellipsis
    }
    
    for char, rep in replacements.items():
        text = text.replace(char, rep)
        
    # Final safety: encode/decode to strip surrogates
    return text.encode('utf-8', 'ignore').decode('utf-8').strip()

# --- Core Logic Functions ---
def run_pipeline_step(command: List[str]):
    """Runs a command as a subprocess, logging and raising errors."""
    # Scrub command args just in case
    safe_command = [scrub_text(str(arg)) for arg in command]
    
    logger.info(f"Executing: {' '.join(safe_command)}")
    try:
        # We explicitly set encoding to utf-8 to handle any surviving chars
        result = subprocess.run(safe_command, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.stdout:
            logger.info(f"STDOUT: {result.stdout}")
        if result.stderr:
            logger.warning(f"STDERR: {result.stderr}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Pipeline step FAILED: {' '.join(safe_command)}")
        logger.error(f"Return Code: {e.returncode}")
        logger.error(f"STDOUT: {e.stdout}")
        logger.error(f"STDERR: {e.stderr}")
        raise

def run_pipeline(pipeline_scripts, p):

        for script_name in pipeline_scripts:
            script_stem = Path(script_name).stem
            success_flag = p.workspace / f"{script_stem}.success"

            if success_flag.exists():
                logger.info(f"Skipping already completed step: {script_name} (success flag found)")
                continue
            
            command = [sys.executable, str(script_directory / script_name)]
            logger.info(f"--- starting: {command}\n ")
            run_pipeline_step(command)
            logger.info(f"--- finished: {command}\n ")
            
            logger.info(f"--- success for script: {script_name} | --setting success flag--")
            success_flag.touch()
        
        return




# --- Main Function ---
def main():
    parser = argparse.ArgumentParser(description="Station 2 Doer: Assembles A-Roll and runs the main AI pipeline.")
    parser.add_argument("--project-dir", required=True, type=Path, help="The path to the project directory.")
    args = parser.parse_args()

    p = ProjectPaths(base=args.project_dir, workspace=(args.project_dir / "ai_working_files"))

    try:
        logger.info(f"=== Station 2: A-Roll Doer Started for {args.project_dir.name} ===")
        
        pipeline_scripts = [
            "01_dehydrate.py",
            "03_transcription.py",
            "04a_waveform_annotator.py",
            "43_generate_transcript.py",
            "44_producers_markup.py",
        ]

        run_pipeline(pipeline_scripts, p)

        # --- Step 2: Rebuild timeline from clean takes (115 logic) ---
        timeline_builder_success_flag = p.workspace / "115_timeline_builder.success"
        
        markup_path = p.workspace / "44_producers_markup.json"
        if not markup_path.exists():
            raise FileNotFoundError("Producers markup file not found, cannot build final timeline.")

        if timeline_builder_success_flag.exists():
            logger.info("Skipping timeline rebuilding (115 logic), success flag found.")
        else:
            logger.info("--- Rebuilding timeline from clean takes (115 logic) ---")
            
            # 1. Clean Slate Restoration (The Fix for 'RuntimeError: Could not find placeholder')
            pre_edit_backup = p.base / "Info.full_multicam_pre-edit.fcpxml"
            if pre_edit_backup.exists():
                logger.info("Found pre-edit backup. Restoring Info.fcpxml to clean state before processing.")
                shutil.copy(pre_edit_backup, p.info_fcpxml)
            else:
                logger.info("Creating pre-edit backup of Info.fcpxml.")
                shutil.copy(p.info_fcpxml, pre_edit_backup)

            markup_data = json.loads(markup_path.read_text(encoding='utf-8'))
            timeline_builder_script = script_directory / "114_timeline_builder.py"
            final_timeline_path = p.info_fcpxml
            
            # Identify all unique refs involved
            source_refs_in_markup = {scene.get('multicam_id') for scene in markup_data if scene.get('multicam_id')}
            
            for source_ref in source_refs_in_markup:
                # Get the scene data for this ref
                scene = next((s for s in markup_data if s.get('multicam_id') == source_ref), None)
                if not scene: continue
                
                final_takes = scene.get("producers_markup", {}).get("final_edit_takes", [])
                
                # --- CASE 1: Empty Takes (Rejected Scene) ---
                if not final_takes:
                    logger.info(f"No final_edit_takes found for {source_ref}. Deleting placeholder from timeline.")
                    command = [
                        sys.executable, str(timeline_builder_script),
                        "--fcpxml", str(final_timeline_path),
                        "--out", str(final_timeline_path),
                        "delete-clip", "--source-ref", source_ref
                    ]
                    run_pipeline_step(command)
                    continue

                # --- CASE 2: Valid Takes ---
                # Scrub the Scene Name to prevent UTF errors in the Subclip Name
                raw_scene_name = scene.get('multicam_name', 'take')
                safe_scene_name = scrub_text(raw_scene_name)

                for j, take in enumerate(final_takes):
                    subclip_name = f"{safe_scene_name} - Subclip {j + 1}"
                    
                    start_sec = float(take['start_seconds'])
                    start_sec = start_sec - .5
                    end_sec = float(take['end_seconds'])
                    end_sec = end_sec + 1.0
                    duration_sec = end_sec - start_sec

                    logger.info(f"Processing {subclip_name}: {start_sec}s to {end_sec}s")
                    
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
            
            logger.info("--- success for timeline building (115 logic) | --setting success flag--")
            timeline_builder_success_flag.touch()
        
        #reset angles, all to angle 1.
        
        #final_touches = [
         #   "115_set_angles.py",
         #   "108_layer_multicam_audio.py",
         #   "200_load_timeline.py",
         #   "210a_generate_and_export_subtitles.py"
        #]

        #run_pipeline(final_touches, p)
        logger.info("=== Station 2: A-Roll Doer Finished Successfully ===")

    except Exception as e:
        logger.error(f"A-Roll Doer FAILED for {args.project_dir.name}. Error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()