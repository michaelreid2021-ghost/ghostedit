#!/usr/bin/env python3
# 005_aroll_compile_watcher.py
# v3.0: Simplified to trigger a single, comprehensive doer script.
import logging
import sys
import time
import subprocess
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from project_paths import get_projects_root

# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "005_aroll_compile_watcher.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(sys.stdout)
    ]
)

def main():
    try:
        projects_base_dir = Path(get_projects_root())
        watch_dir = projects_base_dir.parent / "in_progress"
    except Exception as e:
        logging.critical(f"Could not determine project paths. Error: {e}")
        sys.exit(1)
    
    doer_script_path = script_dir / "006_compile_and_drop_aroll.py"
    if not doer_script_path.exists():
        logging.critical(f"FATAL: Doer script not found: {doer_script_path}")
        sys.exit(1)

    watch_dir.mkdir(parents=True, exist_ok=True)
    logging.info(f"A-Roll Compile Watcher started. Monitoring: {watch_dir}")

    while True:
        try:
            for staged_dir in watch_dir.iterdir():
                if not staged_dir.is_dir():
                    continue

                trigger_file = staged_dir / ".aroll_processing_complete"
                success_file = staged_dir / ".aroll_submit_review_complete"
                failure_file = staged_dir / ".aroll_compile_failed"
                
                if success_file.exists() or failure_file.exists():
                    continue

                if trigger_file.exists():
                    project_name = staged_dir.name
                    final_project_path = projects_base_dir / project_name

                    if not final_project_path.exists():
                        logging.warning(f"Trigger for '{project_name}' found, but dir missing. Skipping.")
                        continue

                    logging.info(f"Detected Compile trigger for: '{project_name}'")
                    
                    doer_command = [
                        sys.executable,
                        str(doer_script_path),
                        "--project-dir", str(final_project_path)
                    ]

                    try:
                        logging.info(f"Executing Compile & Drop Doer for project: '{project_name}'")
                        subprocess.run(doer_command, check=True, capture_output=True, text=True, encoding='utf-8')
                        # The doer script now handles its own success logging and flagging.
                        logging.info(f"Compile & Drop Doer process completed for '{project_name}'.")

                    except subprocess.CalledProcessError as e:
                        logging.error(f"Compile & Drop Doer FAILED for '{project_name}'.")
                        logging.error(f"STDERR: {e.stderr}")
                        failure_file.touch()
            
            time.sleep(30)

        except Exception as e:
            logging.error(f"An unexpected error occurred in the watcher loop: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    main()