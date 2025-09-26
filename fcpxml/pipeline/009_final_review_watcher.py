#!/usr/bin/env python3
# 009_final_review_watcher.py
import logging
import sys
import time
import subprocess
from pathlib import Path
# Add parent directory to path to import shared project_paths module
sys.path.append(str(Path(__file__).resolve().parent.parent))
from project_paths import get_projects_root
# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "009_final_review_watcher.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def main():
    """
    Watches for fully processed projects in the 'completed' directory
    and triggers the final review package generator.
    """
    projects_base_dir = Path(get_projects_root())
    assembly_line_root = projects_base_dir.parent
    completed_dir = assembly_line_root / "completed"
    review_dir = assembly_line_root / "BROLL_REVIEW"
    
    doer_script_path = script_dir / "010_final_review_doer.py"
    if not doer_script_path.exists():
        logging.critical(f"FATAL: Doer script not found at: {doer_script_path}")
        sys.exit(1)

    for dir_path in [completed_dir, review_dir]:
        dir_path.mkdir(parents=True, exist_ok=True)

    logging.info(f"Final Review Watcher started. Monitoring: {completed_dir}")

    while True:
        try:
            for proj_dir in completed_dir.iterdir():
                if not proj_dir.is_dir(): continue

                project_name = proj_dir.name
                success_file = proj_dir / ".final_review_complete"
                failure_file = proj_dir / ".final_review_failed"
                
                if success_file.exists() or failure_file.exists():
                    continue
                
                try:
                    logging.info(f"Triggering Final Review Doer for: '{project_name}'")
                    # Note: We pass the project's path in the 'completed' dir
                    doer_command = [sys.executable, str(doer_script_path), "--project-dir", str(proj_dir)]
                    subprocess.run(doer_command, check=True, capture_output=True, text=True, encoding='utf-8')
                    
                    logging.info(f"Final Review Doer finished successfully for '{project_name}'.")
                    success_file.touch()

                except subprocess.CalledProcessError as e:
                    logging.error(f"Final Review FAILED for '{project_name}'. Error:\n{e.stderr}")
                    failure_file.touch()
            
            time.sleep(30)

        except Exception as e:
            logging.error(f"An unexpected error occurred in the watcher loop: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    main()