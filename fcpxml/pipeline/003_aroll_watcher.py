#!/usr/bin/env python3
import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Add parent fcpxml directory to path to import project_paths
# This allows the script to find your shared modules
sys.path.append(str(Path(__file__).resolve().parent.parent))
from project_paths import get_projects_root

# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "003_aroll_watcher.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(sys.stdout)
    ]
)

def main():
    # --- NO ARGUMENTS NEEDED ---
    # This watcher is now smart and derives all necessary paths.
    
    try:
        # Intelligently locate directories based on the active project config.
        # Assumes a structure like .../assembly_line/projects
        projects_base_dir = Path(get_projects_root())
        assembly_line_root = projects_base_dir.parent
        
        watch_dir = assembly_line_root / "in_progress"
        review_dir = assembly_line_root / "review_aroll"
    except Exception as e:
        logging.critical(f"Could not determine project paths. Is an active project set? Error: {e}")
        sys.exit(1)
    
    # The doer is ALWAYS 004, located in the same directory as this script.
    doer_script_path = script_dir / "004_aroll_doer.py"
    if not doer_script_path.exists():
        logging.critical(f"FATAL: Doer script not found at expected location: {doer_script_path}")
        sys.exit(1)

    watch_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)

    logging.info(f"A-Roll Watcher started. Monitoring: {watch_dir}")

    while True:
        try:
            staged_project_dirs = [d for d in watch_dir.iterdir() if d.is_dir()]
            
            for staged_dir in staged_project_dirs:
                trigger_file = staged_dir / ".project_ready_for_assembly"
                success_file = staged_dir / ".aroll_processing_complete"
                failure_file = staged_dir / ".aroll_failed"
                
                # Ignore this folder if it has already been processed or has failed
                if failure_file.exists() or success_file.exists():
                    continue

                if trigger_file.exists():
                    project_name = staged_dir.name
                    final_project_path = projects_base_dir / project_name

                    if not final_project_path.exists():
                        logging.warning(f"Trigger found for '{project_name}', but final project directory not found at '{final_project_path}'. Skipping for now.")
                        continue

                    logging.info(f"Detected A-Roll trigger for: '{project_name}'")
                    
                    doer_command = [
                        sys.executable,
                        str(doer_script_path),
                        "--project-dir", str(final_project_path)
                    ]

                    try:
                        logging.info(f"Executing A-Roll Doer for project at '{final_project_path}'...")
                        result = subprocess.run(doer_command, check=True, capture_output=True, text=True)
                        
                        logging.info(f"A-Roll Doer finished successfully for '{project_name}'.")
                        if result.stdout: logging.info(f"Doer STDOUT:\n{result.stdout}")

                        # --- CORRECTED SUCCESS LOGIC ---
                        # 1. Create the destination review folder
                        review_project_path = review_dir / project_name
                        review_project_path.mkdir(parents=True, exist_ok=True)

                        # 2. Define source and destination for the FCPXML file
                        source_fcpxml = final_project_path / "Info.fcpxml"
                        dest_fcpxml = review_project_path / "Info.fcpxml"
                        
                        # 3. Copy the FCPXML for human review
                        logging.info(f"Copying '{source_fcpxml}' to '{dest_fcpxml}' for review.")
                        shutil.copy(str(source_fcpxml), str(dest_fcpxml))

                        # 4. Create the success flag in the IN_PROGRESS directory to prevent reprocessing
                        success_flag_path = staged_dir / ".aroll_processing_complete"
                        logging.info(f"Creating success flag at '{success_flag_path}'")
                        success_flag_path.touch()

                        # 5. Remove the original trigger file so this loop doesn't pick it up again
                        logging.info(f"Removing trigger file '{trigger_file}'")
                        trigger_file.unlink()

                    except subprocess.CalledProcessError as e:
                        logging.error(f"A-Roll Doer script FAILED for '{project_name}'.")
                        logging.error(f"STDERR: {e.stderr}")
                        (staged_dir / ".aroll_failed").touch()
            
            time.sleep(30)

        except Exception as e:
            logging.error(f"An unexpected error occurred in the A-Roll watcher loop: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    main()