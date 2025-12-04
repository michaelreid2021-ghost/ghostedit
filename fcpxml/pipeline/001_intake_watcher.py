#!/usr/bin/env python3
# 001_intake_watcher.py (v1.2.0 - Pipeline Lock Architecture)
# Logic:
#   1. Checks Global Lock.
#   2. IF LOCKED: Skips intake. Checks 'In-Progress' folder to ensure Doer ran. Sleeps.
#   3. IF UNLOCKED: Scans 'Watch Dir'.
#      - Found Work -> CREATES LOCK -> Moves to 'In-Progress' -> Runs Doer.
#   4. NOTE: This script DOES NOT delete the lock. Step 010 is responsible for unlocking.

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- Constants ---
WAIT_SECONDS = 60  # Wait for 60 seconds of folder stability

# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "001_watcher.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[logging.FileHandler(log_file_path), logging.StreamHandler(sys.stdout)]
)

def get_last_activity_time(directory: Path) -> float:
    try:
        last_activity = directory.stat().st_mtime
    except FileNotFoundError:
        return 0
        
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
    parser.add_argument("--global-lock", type=Path, help="Path to the global pipeline lock file.")
    args = parser.parse_args()

    args.watch_dir.mkdir(parents=True, exist_ok=True)
    args.in_progress_dir.mkdir(parents=True, exist_ok=True)
    
    # Locate Doer Script
    doer_script_path = script_dir / "002_latest_setup_and_build.py"
    if not doer_script_path.exists():
        logging.critical(f"FATAL: Doer script not found at '{doer_script_path}'")
        sys.exit(1)
    
    # Locate FFprobe (Diagnostic only, Doer handles usage)
    ffprobe_exe_path = args.ffmpeg_dir / "ffprobe"
    if not ffprobe_exe_path.exists() and not (args.ffmpeg_dir / "ffprobe.exe").exists():
        logging.warning(f"Could not find 'ffprobe' in {args.ffmpeg_dir}. Ensure Doer can find it.")

    logging.info(f"Watcher started. Using Doer: {doer_script_path.name}")
    logging.info(f"Monitoring: {args.watch_dir}")
    if args.global_lock:
        logging.info(f"Global Lock enabled at: {args.global_lock}")

    # --- Main Loop ---
    while True:
        processed_work_this_cycle = False
        
        try:
            # ==============================================================================
            # PHASE 1: INTAKE (Guarded by Global Lock)
            # ==============================================================================
            
            pipeline_is_locked = args.global_lock and args.global_lock.exists()

            if not pipeline_is_locked:
                # Pipeline is FREE. Scan for new work.
                try:
                    new_subdirs = [d for d in args.watch_dir.iterdir() if d.is_dir()]
                    
                    for work_dir in new_subdirs:
                        # Check stability
                        if (time.time() - get_last_activity_time(work_dir)) >= WAIT_SECONDS:
                            logging.info(f"Detected stable work request: '{work_dir.name}'")
                            in_progress_path = args.in_progress_dir / work_dir.name
                            
                            # A. Handle Duplicates (Clean up TODO if it's already in progress)
                            if in_progress_path.exists():
                                logging.warning(f"'{work_dir.name}' exists in IN_PROGRESS. Deleting from TODO.")
                                try:
                                    shutil.rmtree(work_dir)
                                except Exception as e:
                                    logging.error(f"Could not remove redundant folder '{work_dir}': {e}")
                                continue

                            # B. CLAIM THE LOCK (Double Check)
                            if args.global_lock and not args.global_lock.exists():
                                try:
                                    logging.info(f"Claiming Global Lock for project: {work_dir.name}")
                                    args.global_lock.write_text(f"Locked by {work_dir.name}", encoding='utf-8')
                                except Exception as e:
                                    logging.error(f"Failed to write lock file: {e}")
                                    continue # Skip if we failed to lock
                            elif args.global_lock:
                                # Race condition lost
                                continue

                            # C. MOVE THE WORK
                            try:
                                logging.info(f"Moving '{work_dir}' to '{in_progress_path}'")
                                shutil.move(str(work_dir), str(in_progress_path))
                            except Exception as e:
                                logging.error(f"Failed to move '{work_dir.name}': {e}")
                                # If move failed, release the lock we just took
                                if args.global_lock and args.global_lock.exists():
                                    args.global_lock.unlink()
                                continue
                            
                            # D. STOP SCANNING
                            # We ingested one item and locked the pipeline. Proceed to Phase 2.
                            break 
                
                except Exception as e:
                    logging.error(f"Error during TODO scan: {e}", exc_info=True)

            # ==============================================================================
            # PHASE 2: PROCESSING (Always runs to handle the project we just moved)
            # ==============================================================================
            try:
                work_items = [d for d in args.in_progress_dir.iterdir() if d.is_dir()]
                
                for in_progress_path in work_items:
                    # Check flags to see if 002 has already finished for this item
                    setup_success_marker = in_progress_path / ".project_ready_for_assembly"
                    setup_failure_marker = in_progress_path / ".setup_failed"

                    if (setup_success_marker.exists() or setup_failure_marker.exists()):
                        # 002 is done. We assume downstream watchers are handling it now.
                        # Since we are locked, we just loop and wait for 010 to delete the lock.
                        continue

                    processed_work_this_cycle = True

                    # --- Run the doer script (002) ---
                    try:
                        logging.info(f"Calling doer script for '{in_progress_path.name}'")
                        doer_command = [
                            sys.executable, str(doer_script_path),
                            "--work-request-dir", str(in_progress_path),
                            "--projects-base-dir", str(args.projects_base_dir),
                            "--shared-scripts-dir", str(script_dir.parent)
                        ]

                        logging.info(f"Executing: {' '.join(doer_command)}")
                        result = subprocess.run(
                            doer_command,
                            check=True,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                        )

                        logging.info(f"Successfully processed setup for: '{in_progress_path.name}'")
                        if result.stdout:
                            logging.info(f"[{in_progress_path.name} STDOUT]: {result.stdout.strip()}")

                    except subprocess.CalledProcessError as e:
                        logging.error(f"Doer script failed for '{in_progress_path.name}'.")
                        if e.stdout: logging.error(f"STDOUT: {e.stdout.strip()}")
                        if e.stderr: logging.error(f"STDERR: {e.stderr.strip()}")
                    
                    except Exception as e:
                        logging.error(f"Unknown error processing '{in_progress_path.name}': {e}", exc_info=True)

                    # NOTE: We do NOT release the lock here. 
                    # The pipeline is now busy with this project until Step 010.

            except Exception as e:
                logging.error(f"Error during IN_PROGRESS scan: {e}", exc_info=True)

            # --- Sleep logic ---
            # If we did work, loop fast. If idle (or locked and waiting), sleep.
            if not processed_work_this_cycle:
                time.sleep(30)
            
        except KeyboardInterrupt:
            logging.info("Watcher stopped by user.")
            break
        except Exception as e:
            logging.error(f"Fatal error in main loop: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    main()