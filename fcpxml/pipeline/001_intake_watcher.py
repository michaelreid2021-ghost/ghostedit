#!/usr/bin/env python3
import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- Constants ---
WAIT_SECONDS = 30  # Wait for 30 seconds of inactivity

# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "001_watcher.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] - %(message)s", handlers=[logging.FileHandler(log_file_path), logging.StreamHandler(sys.stdout)])

def get_last_activity_time(directory: Path) -> float:
    # ... (this function remains the same) ...
    last_activity = directory.stat().st_mtime
    for root, _, files in os.walk(directory):
        for name in files:
            path = Path(root) / name
            try:
                last_activity = max(last_activity, path.stat().st_mtime)
            except FileNotFoundError:
                continue
    return last_activity

def main():
    parser = argparse.ArgumentParser(description="Monitors a directory for new work and triggers the project setup doer.")
    parser.add_argument("--watch-dir", required=True, type=Path, help="The 'TODO' directory to monitor for new project folders.")
    parser.add_argument("--in-progress-dir", required=True, type=Path, help="The staging directory for active processing.")
    parser.add_argument("--projects-base-dir", required=True, type=Path, help="The final output directory where projects are built.")
    parser.add_argument("--ffmpeg-dir", required=True, type=Path, help="Path to the directory containing ffmpeg binaries.")
    args = parser.parse_args()

    args.watch_dir.mkdir(parents=True, exist_ok=True)
    args.in_progress_dir.mkdir(parents=True, exist_ok=True)
    
    # The doer is assumed to be in the same directory as this watcher
    doer_script_path = script_dir / "002_setup_project.py"
    if not doer_script_path.exists():
        logging.critical(f"FATAL: Doer script not found at expected location: {doer_script_path}")
        sys.exit(1)

    logging.info(f"Watcher started. Monitoring: {args.watch_dir}")
    processed_folders = set()

    while True:
        try:
            subdirs = [d for d in args.watch_dir.iterdir() if d.is_dir()]
            for work_dir in subdirs:
                if work_dir.name in processed_folders:
                    continue
                
                if (time.time() - get_last_activity_time(work_dir)) >= WAIT_SECONDS:
                    logging.info(f"Detected stable work request: '{work_dir.name}'")
                    in_progress_path = args.in_progress_dir / work_dir.name
                    
                    try:
                        logging.info(f"Moving '{work_dir}' to '{in_progress_path}'")
                        shutil.move(str(work_dir), str(in_progress_path))
                        
                        logging.info(f"Calling doer script for '{in_progress_path.name}'")
                        doer_command = [
                            sys.executable, str(doer_script_path),
                            "--work-request-dir", str(in_progress_path),
                            "--projects-base-dir", str(args.projects_base_dir),
                            "--shared-scripts-dir", str(script_dir.parent)
                          
                        ]
                        subprocess.run(doer_command, check=True, capture_output=True, text=True)
                        logging.info(f"Successfully processed work request: '{in_progress_path.name}'")
                        
                    except subprocess.CalledProcessError as e:
                        logging.error(f"Doer script failed for '{in_progress_path.name}'. Log follows.")
                        logging.error(f"STDOUT: {e.stdout}")
                        logging.error(f"STDERR: {e.stderr}")
                    except Exception as e:
                        logging.error(f"Failed during processing of '{work_dir.name}': {e}")
                    
                    processed_folders.add(work_dir.name)

            time.sleep(30)
        except Exception as e:
            logging.error(f"An unexpected error occurred in the watcher loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()