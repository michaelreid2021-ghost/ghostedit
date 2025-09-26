#!/usr/bin/env python3
# 008_broll_doer.py
import argparse
import logging
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import os

# Assume project_paths is in a reachable path
from project_paths import ProjectPaths

# --- Logging Setup ---
script_dir = Path(__file__).resolve().parent
log_file_path = script_dir / "008_broll_doer.log"
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

def run_pipeline_step(command: list, is_powershell: bool = False):
    """Runs a command as a subprocess, logging its execution and handling errors."""
    cmd_str = ' '.join(map(str, command))
    logger.info(f"Executing: {cmd_str}")
    pwsh_exe = shutil.which('pwsh') or shutil.which('powershell')
    final_command = [pwsh_exe, "-ExecutionPolicy", "Bypass", "-File"] + command if is_powershell and pwsh_exe else command
    if is_powershell and not pwsh_exe: raise RuntimeError("PowerShell not found.")
    try:
        result = subprocess.run(final_command, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.stdout: logger.info(f"STDOUT:\n{result.stdout}")
        if result.stderr: logger.warning(f"STDERR:\n{result.stderr}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Pipeline step FAILED: {cmd_str}\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}")
        raise

def get_timeline_name_from_fcpxml(fcpxml_path: Path) -> str:
    """Parses FCPXML to find the main sequence/timeline name."""
    tree = ET.parse(fcpxml_path)
    root = tree.getroot()
    sequence = root.find('.//library/event/project/sequence/name')
    if sequence is not None and sequence.text:
        return sequence.text.strip()
    sequence = root.find('.//sequence/name')
    if sequence is not None and sequence.text:
        return sequence.text.strip()
    return "timeline"

def main():
    parser = argparse.ArgumentParser(description="Station 3 Doer: Loads timeline, generates transcript, and applies B-Roll.")
    parser.add_argument("--project-dir", required=True, type=Path)
    args = parser.parse_args()
    p = ProjectPaths(base=args.project_dir, workspace=(args.project_dir / "ai_working_files"))

    try:
        logger.info(f"=== B-Roll Doer Started for {args.project_dir.name} ===")

        # --- Step 1: Backup and copy the approved A-Roll timeline (only if not already done) ---
        assembly_line_root = p.base.parent.parent
        approved_project_dir = assembly_line_root / "AROLL_APPROVED" / p.base.name
        source_fcpxml = approved_project_dir / "Info.fcpxml"
        dest_fcpxml = p.info_fcpxml
        copy_complete_flag = p.workspace / ".aroll_copy_complete"

        if not copy_complete_flag.exists():
            if not source_fcpxml.exists(): raise FileNotFoundError(f"Source FCPXML not found at {source_fcpxml}")
            if dest_fcpxml.exists():
                timestamp = time.strftime("%Y%m%d-%H%M%S")
                backup_path = p.base / f"Info.pre_broll.{timestamp}.fcpxml"
                logger.info(f"Backing up current timeline to {backup_path}")
                shutil.copy(dest_fcpxml, backup_path)
            logger.info(f"Copying approved A-Roll timeline from '{source_fcpxml}' to '{dest_fcpxml}'")
            shutil.copy(str(source_fcpxml), str(dest_fcpxml))
            copy_complete_flag.touch()
        else:
            logger.info("A-Roll timeline already copied in a previous run. Skipping copy step.")

        # --- Step 2: Run the full B-Roll pipeline ---
        pipeline_scripts = [
            "200_load_timeline.py",
            "210_generate_and_export_subtitles.py",
            "41_production_order.py",
            "45_srt_based_pacing_test.py",
            "46_srt_b_roll_suggester.py",
            "59_fake_broll.py",
            "118_apply_broll_placeholders.ps1",
        ]

        # --- NEW: Hybrid Check Setup ---
        # This check is a fallback for older runs that didn't create .success flags for subtitle steps.
        final_srt_exists = (p.workspace / "subtitle 1.srt").exists()
        if final_srt_exists:
            logger.info("Found 'subtitle 1.srt', will use it to skip early steps if their .success flags are missing.")

        for script_name in pipeline_scripts:
            script_stem = Path(script_name).stem
            success_flag = p.workspace / f"{script_stem}.success"

            # Primary Check: Look for the modern .success flag.
            if success_flag.exists():
                logger.info(f"Skipping already completed step: {script_name} (success flag found)")
                continue

            # Fallback Check: For subtitle steps, if the final SRT exists, we can skip them.
            if final_srt_exists and script_name in ["200_load_timeline.py", "210_generate_and_export_subtitles.py"]:
                logger.info(f"Skipping step '{script_name}' because final artifact 'subtitle 1.srt' exists.")
                continue

            # Special handling for the SRT rename step
            if "210_generate_and_export_subtitles" in script_name:
                run_pipeline_step([sys.executable, str(script_dir / script_name)])
                
                logger.info("--- Moving transcript into place for AI analysis ---")
                timeline_name = p.base.name
                generated_srt_name = f"{timeline_name}_subtitles.srt".replace(os.sep, "_")
                source_srt = p.workspace / generated_srt_name
                dest_srt = p.workspace / "subtitle 1.srt"
                
                if not source_srt.exists():
                    raise FileNotFoundError(f"Expected transcript '{source_srt.name}' was not found after running 210 script.")
                
                logger.info(f"Renaming '{source_srt.name}' to '{dest_srt.name}'")
                source_srt.rename(dest_srt)
                success_flag.touch()
                continue

            # Standard script execution for all other scripts
            is_ps = script_name.endswith('.ps1')
            script_path = script_dir / script_name
            if not script_path.exists(): raise FileNotFoundError(f"Pipeline script '{script_path}' not found.")
            
            command = [sys.executable, str(script_path)] if not is_ps else [str(script_path)]
            run_pipeline_step(command, is_powershell=is_ps)
            
            # If the script runs without error, create its success flag
            success_flag.touch()

        logger.info("=== B-Roll Doer Finished Successfully ===")

    except Exception as e:
        logger.error(f"B-Roll Doer FAILED for {args.project_dir.name}. Error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()