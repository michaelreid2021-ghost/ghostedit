#!/usr/bin/env python3
"""Monitor completed projects and kick off the final review packaging."""

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parent.parent))
from project_paths import get_projects_root

script_dir = Path(__file__).parent
log_file_path = script_dir / "009_final_review_watcher.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

SUCCESS_FLAG = ".final_review_complete"
FAILURE_FLAG = ".final_review_failed"


def _resolve_directory(explicit: Optional[Path], *candidates: Path) -> Path:
    if explicit:
        return explicit.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return candidates[0].resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watcher that packages completed projects for final review."
    )
    parser.add_argument(
        "--completed-dir",
        type=Path,
        help="Directory containing completed projects that need review packages.",
    )
    parser.add_argument(
        "--review-dir",
        type=Path,
        help="Directory where final review packages should be written.",
    )
    parser.add_argument(
        "--projects-base-dir",
        type=Path,
        help="Workspace projects directory (used to resolve defaults).",
    )
    parser.add_argument(
        "--success-flag",
        default=SUCCESS_FLAG,
        help="Success marker filename placed inside the completed project directory.",
    )
    parser.add_argument(
        "--failure-flag",
        default=FAILURE_FLAG,
        help="Failure marker filename placed inside the completed project directory.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="Seconds to wait between directory scans.",
    )
    parser.add_argument("--global-lock", type=Path, help="Path to the global pipeline lock file.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        if args.projects_base_dir:
            projects_base_dir = args.projects_base_dir.resolve()
        else:
            projects_base_dir = Path(get_projects_root()).resolve()

        assembly_root = projects_base_dir.parent
        completed_dir = _resolve_directory(
            args.completed_dir, assembly_root / "completed"
        )
        review_dir = _resolve_directory(
            args.review_dir, assembly_root / "BROLL_REVIEW"
        )
    except Exception as exc:
        logging.critical("Could not determine final review directories: %s", exc)
        sys.exit(1)

    doer_script_path = script_dir / "010_final_review_doer.py"
    if not doer_script_path.exists():
        logging.critical("Final review doer script not found: %s", doer_script_path)
        sys.exit(1)

    for directory in (completed_dir, review_dir):
        directory.mkdir(parents=True, exist_ok=True)

    logging.info("Final Review Watcher monitoring: %s", completed_dir)
    logging.info("Review package output directory: %s", review_dir)
    logging.info("Projects base directory: %s", projects_base_dir)

    success_name = args.success_flag
    failure_name = args.failure_flag
    poll_interval = max(args.poll_interval, 1.0)

    while True:
        try:
            for project_dir in completed_dir.iterdir():
                if not project_dir.is_dir():
                    continue

                project_name = project_dir.name
                success_file = project_dir / success_name
                failure_file = project_dir / failure_name

                if success_file.exists() or failure_file.exists():
                    continue

                command = [
                    sys.executable,
                    str(doer_script_path),
                    "--project-dir",
                    str(project_dir),
                    "--review-dir",
                    str(review_dir),
                    "--projects-root",
                    str(projects_base_dir),
                ]
                # Pass lock if it exists
                if args.global_lock:
                    command.extend(["--global-lock", str(args.global_lock)])
                logging.info(
                    "Triggering Final Review Doer for '%s'", project_name
                )
                try:
                    result = subprocess.run(
                        command,
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    if result.stdout:
                        logging.info(
                            "Doer STDOUT for '%s':\n%s", project_name, result.stdout
                        )
                    if result.stderr:
                        logging.warning(
                            "Doer STDERR for '%s':\n%s", project_name, result.stderr
                        )
                    success_file.touch()
                except subprocess.CalledProcessError as err:
                    logging.error(
                        "Final Review Doer failed for '%s' (exit %s).",
                        project_name,
                        err.returncode,
                    )
                    if err.stdout:
                        logging.error(
                            "Doer STDOUT for '%s':\n%s", project_name, err.stdout
                        )
                    if err.stderr:
                        logging.error(
                            "Doer STDERR for '%s':\n%s", project_name, err.stderr
                        )
                    failure_file.touch()

            time.sleep(poll_interval)
        except Exception as exc:
            logging.error(
                "Unexpected error in Final Review watcher: %s", exc, exc_info=True
            )
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
