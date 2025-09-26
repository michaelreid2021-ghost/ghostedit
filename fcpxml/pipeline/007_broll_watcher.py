#!/usr/bin/env python3
# 007_broll_watcher.py
import logging
import sys
import time
import subprocess
import shutil
from pathlib import Path

# Add parent directory to path to import shared project_paths module
sys.path.append(str(Path(__file__).resolve().parent.parent))
from project_paths import get_projects_root

# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "007_broll_watcher.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)

def main():
    """
    Watches for an approved FCPXML, triggers the B-Roll pipeline,
    and archives the project upon completion.
    """
    try:
        projects_base_dir = Path(get_projects_root())
        assembly_line_root = projects_base_dir.parent
        approved_dir = assembly_line_root / "AROLL_APPROVED"
        in_progress_dir = assembly_line_root / "in_progress"
        completed_dir = assembly_line_root / "completed"
    except Exception as e:
        logging.critical(f"Could not determine project paths. Is an active project set? Error: {e}")
        sys.exit(1)

    doer_script_path = script_dir / "008_broll_doer.py"
    if not doer_script_path.exists():
        logging.critical(f"FATAL: Doer script not found at: {doer_script_path}")
        sys.exit(1)

    for dir_path in [approved_dir, in_progress_dir, completed_dir]:
        dir_path.mkdir(parents=True, exist_ok=True)

    logging.info(f"B-Roll Watcher started. Monitoring: {approved_dir}")

    while True:
        try:
            # --- Part 1: Trigger new B-Roll processing jobs ---
            for proj_dir in approved_dir.iterdir():
                if not proj_dir.is_dir(): continue

                success_file = proj_dir / ".broll_processing_complete"
                failure_file = proj_dir / ".broll_processing_failed"
                if success_file.exists() or failure_file.exists():
                    continue
                
                # TRIGGER: Watch for the Info.fcpxml file only.
                fcpxml_trigger = proj_dir / "Info.fcpxml"
                if fcpxml_trigger.exists():
                    project_name = proj_dir.name
                    target_project_path = projects_base_dir / project_name
                    
                    if not target_project_path.exists():
                        logging.warning(f"Trigger for '{project_name}' found, but project dir missing at '{target_project_path}'. Skipping.")
                        continue
                    
                    try:
                        logging.info(f"Triggering B-Roll Doer for: '{project_name}'")
                        doer_command = [sys.executable, str(doer_script_path), "--project-dir", str(target_project_path)]
                        subprocess.run(doer_command, check=True, capture_output=True, text=True)
                        
                        logging.info(f"B-Roll Doer finished successfully for '{project_name}'.")
                        success_file.touch()

                    except subprocess.CalledProcessError as e:
                        logging.error(f"Processing FAILED for '{project_name}'. Error:\n{e.stderr}")
                        failure_file.touch()

            # --- Part 2: Archive fully completed projects ---
            for flag_file in approved_dir.glob("*/.broll_processing_complete"):
                approved_proj_dir = flag_file.parent
                project_name = approved_proj_dir.name
                source_in_progress = in_progress_dir / project_name
                dest_completed = completed_dir / project_name

                if dest_completed.exists(): continue
                if not source_in_progress.exists(): continue

                logging.info(f"Archiving '{project_name}': Moving from '{source_in_progress}' to '{completed_dir}'.")
                shutil.move(str(source_in_progress), str(dest_completed))
                shutil.rmtree(approved_proj_dir)

            time.sleep(30)

        except Exception as e:
            logging.error(f"An unexpected error occurred in the watcher loop: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    main()