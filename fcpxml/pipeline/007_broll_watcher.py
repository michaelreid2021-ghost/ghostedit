#!/usr/bin/env python3

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parent.parent))
from project_paths import get_projects_root

script_dir = Path(__file__).parent
log_file_path = script_dir / "007_broll_watcher.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)

TRIGGER_FILENAME = "Info.fcpxml"
SUCCESS_FLAG = ".broll_processing_complete"
FAILURE_FLAG = ".broll_processing_failed"

def safe_remove_tree(path: Path) -> None:
    # This function is no longer called by main() but is kept
    # in case it's needed for other utility purposes.
    if not path.exists():
        return
    try:
        time.sleep(5)
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except PermissionError as exc:
        logging.warning("Could not remove %s (permission denied): %s", path, exc)
    except OSError as exc:
        logging.warning("Could not remove %s: %s", path, exc)

def _resolve_directory(explicit: Optional[Path], *candidates: Path) -> Path:
    if explicit:
        return explicit.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return candidates[0].resolve()

def get_srt_path(project_dir: Path) -> Path:
    broll_plans_dir = project_dir / "ai_working_files" / "46_ai_b_roll_plans"
    default_srt = project_dir / "ai_working_files" / 'subtitle 1.srt'
    
    if not broll_plans_dir.exists():
        logging.debug("No B-roll plans directory found at %s, using default SRT path: %s", broll_plans_dir, default_srt)
        return default_srt
    
    latest_plan = max(broll_plans_dir.glob("*_broll_plan.json"), key=lambda x: x.stat().st_mtime, default=None)
    if not latest_plan:
        logging.debug("No B-roll plan JSON found in %s, using default SRT path: %s", broll_plans_dir, default_srt)
        return default_srt
    
    try:
        with open(latest_plan, 'r', encoding='utf-8') as f:
            plan = json.load(f)
        source_srt = plan.get("source_srt")
        if source_srt and Path(source_srt).exists():
            logging.debug("Found source_srt in B-roll plan: %s", source_srt)
            return Path(source_srt)
        logging.debug("source_srt %s not found or invalid, using default SRT path: %s", source_srt, default_srt)
    except Exception as exc:
        logging.warning("Failed to read B-roll plan %s: %s, using default SRT path: %s", latest_plan, exc, default_srt)
    
    return default_srt

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watcher that kicks off B-Roll processing when A-Roll is approved.")
    parser.add_argument("--approved-dir", type=Path, help="Directory containing client-approved A-Roll folders.")
    parser.add_argument("--projects-base-dir", type=Path, help="Workspace projects directory.")
    parser.add_argument("--in-progress-dir", type=Path, help="Workspace IN_PROGRESS directory for tracking live projects.")
    parser.add_argument("--trigger-filename", default=TRIGGER_FILENAME, help="Filename that signals B-Roll should run.")
    parser.add_argument("--success-flag", default=SUCCESS_FLAG, help="Flag file written on success.")
    parser.add_argument("--failure-flag", default=FAILURE_FLAG, help="Flag file written on failure.")
    parser.add_argument("--poll-interval", type=float, default=30.0, help="Seconds to wait between scans.")
    return parser.parse_args()



def main() -> None:
    args = parse_args()

    try:
        if args.projects_base_dir:
            projects_base_dir = args.projects_base_dir.resolve()
        else:
            projects_base_dir = Path(get_projects_root()).resolve()

        assembly_root = projects_base_dir.parent

        approved_dir = _resolve_directory(
            args.approved_dir,
            assembly_root / "AROLL_APPROVED",
            assembly_root / "aroll_approved",
        )

        in_progress_dir = _resolve_directory(
            args.in_progress_dir,
            assembly_root / "IN_PROGRESS",
            assembly_root / "in_progress",
        )
    except Exception as exc:
        logging.critical("Could not determine B-Roll directories: %s", exc)
        sys.exit(1)

    doer_script_path = script_dir / "008_broll_doer.py"
    if not doer_script_path.exists():
        logging.critical("Doer script not found: %s", doer_script_path)
        sys.exit(1)

    logging.info("B-Roll Watcher monitoring: %s", approved_dir)
    logging.info("Projects base directory: %s", projects_base_dir)
    logging.info("In-progress directory: %s", in_progress_dir)

    trigger_name = args.trigger_filename
    success_name = args.success_flag
    failure_name = args.failure_flag

    # Track last logged state per project to avoid spam
    last_log_state = {}

    while True:
        try:
            for project_dir in approved_dir.iterdir():
                if not project_dir.is_dir():
                    continue

                project_name = project_dir.name
                
                # --- Define all paths ---
                lock_file = project_dir / ".broll_processing.lock"
                workspace_project_dir = in_progress_dir / project_name
                target_project_path = projects_base_dir / project_name
                success_file = workspace_project_dir / success_name
                failure_file = workspace_project_dir / failure_name
                trigger_file = project_dir / trigger_name

                # --- Check existence for all conditions ---
                has_workspace = workspace_project_dir.exists()
                has_trigger = trigger_file.exists()
                has_project = target_project_path.exists()
                has_lock = lock_file.exists()
                has_success = success_file.exists()
                has_failure = failure_file.exists()

                # --- Build verbose log message ---
                log_lines = [
                    f"  - Workspace ({workspace_project_dir.relative_to(assembly_root)}): {'EXISTS' if has_workspace else 'NOT FOUND'}",
                    f"  - Project ({target_project_path.relative_to(assembly_root)}): {'EXISTS' if has_project else 'NOT FOUND'}",
                    f"  - Trigger ({trigger_file.relative_to(approved_dir)}): {'EXISTS' if has_trigger else 'NOT FOUND'} TRIGGER_FULL_PATH: {trigger_file}",
                    f"  - Lock ({lock_file.relative_to(approved_dir)}): {'EXISTS' if has_lock else 'NOT FOUND'}",
                    f"  - Success ({success_file.relative_to(in_progress_dir)}): {'EXISTS' if has_success else 'NOT FOUND'}",
                    f"  - Failure ({failure_file.relative_to(in_progress_dir)}): {'EXISTS' if has_failure else 'NOT FOUND'}"
                ]

                # --- Determine decision based on checks ---
                decision = "call_008"
                decision_reason = ""
                
                if not has_workspace:
                    decision = "skip"
                    decision_reason = "FUBAR! Skipping (Workspace directory is missing)."
                elif has_lock:
                    decision = "skip"
                    decision_reason = "Skipping (Lock file exists)."
                elif has_success:
                    decision = "skip"
                    decision_reason = "Skipping (Success flag exists)."
                elif has_failure:
                    decision = "skip"
                    decision_reason = "Skipping (Failure flag exists)."
                elif not has_trigger:
                    decision = "skip"
                    decision_reason = "Skipping (Trigger file not found)."
                elif not has_project:
                    decision = "skip"
                    decision_reason = "Skipping (Project directory not found)."
                else:
                    decision_reason = "Proceeding to call 008."
                
                log_lines.append(f"  DECISION: {decision_reason}")
                current_log_state = "\n".join(log_lines)

                # --- Log only if state has changed ---
                if last_log_state.get(project_name) != current_log_state:
                    logging.info(f"Checking project '{project_name}':\n{current_log_state}")
                    last_log_state[project_name] = current_log_state

                # --- Execute decision (this logic is unchanged) ---
                if decision == "skip":
                    if not has_project and has_trigger:
                         # This specific warning is useful, so we keep it
                         logging.warning(
                            "Trigger for '%s' found, but project directory missing at %s. Skipping.",
                            project_name,
                            target_project_path,
                        )
                    continue

                # --- ARTIFACT CLEANUP BLOCK REMOVED ---

                # Copy approved FCPXML from A-Roll drop (Unchanged)
                approved_fcpxml = project_dir / trigger_name
                if approved_fcpxml.exists():
                    target_fcpxml = target_project_path / "Info.fcpxml"
                    if not target_fcpxml.exists() or approved_fcpxml.stat().st_mtime > target_fcpxml.stat().st_mtime:
                        logging.info("Copying/updating approved A-Roll FCPXML for %s", project_name)
                        shutil.copy(approved_fcpxml, target_fcpxml)

                lock_file.touch(exist_ok=True)

                command = [
                    sys.executable,
                    str(doer_script_path),
                    "--project-dir",
                    str(target_project_path),
                    "--approved-dir",
                    str(approved_dir),
                ]

                logging.info("Triggering B-Roll doer for '%s'", project_name)
                try:
                    result = subprocess.run(
                        command,
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                    )
                    if result.stdout:
                        logging.info("Doer STDOUT for '%s':\n%s", project_name, result.stdout)
                    if result.stderr:
                        logging.warning("Doer STDERR for '%s':\n%s", project_name, result.stderr)
                    
                    # --- MODIFIED VALIDATION CHECK (Unchanged) ---
                    final_fcpxml_name = f"{project_name}_BROLL_v1.fcpxml"
                    final_fcpxml_path = target_project_path / final_fcpxml_name
                    logging.debug("Validating B-Roll FCPXML post-doer for %s: %s", project_name, final_fcpxml_path)
                    
                    if not final_fcpxml_path.exists():
                        raise RuntimeError(f"Success flag would be set, but final FCPXML missing at {final_fcpxml_path}—potential incomplete run.")
                    # --- END MODIFICATION ---
                    
                    success_file.touch()
                    # --- REMOVED: Deletion of failure_file on success ---
                    
                except subprocess.CalledProcessError as err:
                    logging.error(
                        "B-Roll doer failed for '%s' (exit %s).", project_name, err.returncode
                    )
                    if err.stderr:
                        logging.error("Doer STDERR for '%s':\n%s", project_name, err.stderr)
                    
                    failure_file.touch()
                except Exception as exc:
                    logging.error(
                        "Unexpected error while running B-Roll doer for '%s': %s",
                        project_name,
                        exc,
                        exc_info=True,
                    )
                    failure_file.touch()
                # --- REMOVED: 'finally' block that auto-unlocked the file ---
            
            time.sleep(max(args.poll_interval, 3.0))
        except Exception as exc:
            logging.error("Unexpected error in B-Roll watcher: %s", exc, exc_info=True)
            time.sleep(max(args.poll_interval, 3.0))

if __name__ == "__main__":
    main()