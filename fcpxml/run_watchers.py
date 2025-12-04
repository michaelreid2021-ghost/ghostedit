#!/usr/bin/env python3
# v1.3.0 — Added Single-Threaded Pipeline Locking
import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

ProcessMap = Dict[str, subprocess.Popen]

def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] - %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

def launch_process(label: str, cmd: Tuple[str, ...]) -> Tuple[subprocess.Popen, threading.Thread]:
    logging.info("Starting %s...", label)
    logging.debug("Command: %s", " ".join(cmd))
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    logging.info("%s PID: %s", label, process.pid)
    thread = threading.Thread(target=_stream_output, args=(label, process), daemon=True)
    thread.start()
    return process, thread

def _stream_output(label: str, process: subprocess.Popen) -> None:
    if not process.stdout: return
    for line in process.stdout:
        logging.info("[%s] %s", label, line.rstrip())

def _tail_log(filename: str) -> None:
    label = Path(filename).stem
    try:
        with open(filename, 'r') as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    logging.info("[%s] %s", label, line.rstrip())
                else:
                    time.sleep(0.1)
    except Exception as e:
        logging.error("Error tailing %s: %s", filename, e)

def terminate_processes(processes: ProcessMap) -> None:
    for label, process in processes.items():
        if process.poll() is None:
            logging.info("Stopping %s (PID %s)...", label, process.pid)
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

def resolve_process_running(process_names: Sequence[str]) -> Tuple[bool, Optional[str]]:
    for name in process_names:
        if sys.platform.startswith("win"):
            cmd = ["tasklist", "/FI", f"IMAGENAME eq {name}"]
        elif sys.platform.startswith("darwin"):
            cmd = ["pgrep", "-if", name]
        else:
            cmd = ["pgrep", "-f", name]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            continue
        output = (result.stdout or "").strip()
        if sys.platform.startswith("win"):
            if output and "INFO: No tasks are running" not in output and name.lower() in output.lower():
                return True, name
        else:
            if result.returncode == 0 and output:
                return True, name
    return False, None

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GhostEdit watchers.")
    parser.add_argument("--ffmpeg-dir", type=Path, default="c:\\ffmpeg\\bin")
    parser.add_argument("--log-dir", type=Path, default="e:\\ghostedit\\fcpxml\\logs")
    parser.add_argument("--workspace-dir", type=Path, default="e:\\ghostedit\\fcpxml\\workspace")
    parser.add_argument("--shared-root", type=Path, default="E:\ghost_edit_client_projects\harvest")
    parser.add_argument("--todo-dir", type=Path, default="E:\ghost_edit_client_projects\harvest\TODO")
    parser.add_argument("--review-aroll-dir", type=Path, default=None)
    parser.add_argument("--aroll-approved-dir", type=Path, default=None)
    parser.add_argument("--broll-review-dir", type=Path, default=None)
    parser.add_argument("--in-progress-dir", type=Path, default=None)
    parser.add_argument("--projects-dir", type=Path, default=None)
    parser.add_argument("--completed-dir", type=Path, default=None)
    parser.add_argument("--resolve-process", action="append", default=None)
    parser.add_argument("--base-dir", type=Path, default=None)

    args = parser.parse_args()
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    pipeline_dir = repo_root / "fcpxml" / "pipeline"

    workspace_candidate = args.workspace_dir or args.base_dir or (repo_root / "assembly_line")
    workspace_dir = Path(workspace_candidate).resolve()
    shared_candidate = args.shared_root or workspace_dir
    shared_root = Path(shared_candidate).resolve()

    # --- DEFINE GLOBAL LOCK PATH ---
    # This file determines if the system is busy.
    global_lock_path = workspace_dir / ".pipeline.lock"

    todo_dir = (args.todo_dir or (shared_root / "TODO")).resolve()
    aroll_review_dir = (args.review_aroll_dir or (shared_root / "AROLL_REVIEW")).resolve()
    aroll_approved_dir = (args.aroll_approved_dir or (shared_root / "AROLL_APPROVED")).resolve()
    broll_review_dir = (args.broll_review_dir or (shared_root / "BROLL_REVIEW")).resolve()
    in_progress_dir = (args.in_progress_dir or (workspace_dir / "IN_PROGRESS")).resolve()
    projects_base_dir = (args.projects_dir or (workspace_dir / "projects")).resolve()
    completed_dir = (args.completed_dir or (workspace_dir / "completed")).resolve()

    for p in (todo_dir, aroll_review_dir, aroll_approved_dir, broll_review_dir, in_progress_dir, projects_base_dir, completed_dir):
        p.mkdir(parents=True, exist_ok=True)

    log_file_path = (args.log_dir or script_dir).resolve() / "run_watchers.log"
    configure_logging(log_file_path)

    resolve_processes = args.resolve_process or (["Resolve.exe"] if sys.platform.startswith("win") else ["Resolve"])
    resolve_running, _ = resolve_process_running(resolve_processes)
    if not resolve_running:
        logging.error("Please Launch DaVinci Resolve before starting Watchers.")
        return 1

    logging.info("--- GHOSTEDIT PIPELINE ORCHESTRATOR ---")
    logging.info(f"Global Lock File: {global_lock_path}")
    
    # Define script paths
    intake_watcher = pipeline_dir / "001_intake_watcher.py"
    aroll_watcher = pipeline_dir / "003_aroll_watcher.py"
    broll_watcher = pipeline_dir / "007_broll_watcher.py"
    final_watcher = pipeline_dir / "009_final_review_watcher.py"

    watchers: Dict[str, Tuple[str, ...]] = {
        "Intake": (
            sys.executable, str(intake_watcher),
            "--watch-dir", str(todo_dir),
            "--in-progress-dir", str(in_progress_dir),
            "--projects-base-dir", str(projects_base_dir),
            "--ffmpeg-dir", str(args.ffmpeg_dir),
            "--global-lock", str(global_lock_path) # Pass lock to intake
        ),
        "A-Roll": (
            sys.executable, str(aroll_watcher),
            "--watch-dir", str(in_progress_dir),
            "--review-dir", str(aroll_review_dir),
            "--projects-base-dir", str(projects_base_dir),
        ),
        "B-Roll": (
            sys.executable, str(broll_watcher),
            "--approved-dir", str(aroll_approved_dir),
            "--projects-base-dir", str(projects_base_dir),
            "--in-progress-dir", str(in_progress_dir)
        ),
        "Final": (
            sys.executable, str(final_watcher),
            "--completed-dir", str(completed_dir),
            "--review-dir", str(broll_review_dir),
            "--projects-base-dir", str(projects_base_dir),
            "--global-lock", str(global_lock_path) # Pass lock to final to release it
        ),
    }

    processes: ProcessMap = {}
    threads: Dict[str, threading.Thread] = {}
    tail_threads: Dict[Path, threading.Thread] = {}
    tailed_files: set[Path] = set()

    for label, cmd in watchers.items():
        process, thread = launch_process(label, cmd)
        processes[label] = process
        threads[label] = thread

    exit_code = 0
    try:
        while True:
            # Tail logs logic (unchanged)...
            for log_file in pipeline_dir.glob("*.log"):
                if log_file == log_file_path or log_file in tailed_files: continue
                logging.info("Starting to tail %s", log_file)
                t = threading.Thread(target=_tail_log, args=(str(log_file),), daemon=True)
                t.start()
                tail_threads[log_file] = t
                tailed_files.add(log_file)

            # Monitor subprocesses
            for label, process in processes.items():
                if process.poll() is not None:
                    logging.error(f"{label} died unexpectedly! Code: {process.returncode}")
                    exit_code = 1
                    break
            if exit_code != 0: break
            time.sleep(0.5)

    except KeyboardInterrupt:
        logging.info("Shutting down pipeline...")
    finally:
        terminate_processes(processes)
    
    return exit_code

if __name__ == "__main__":
    sys.exit(main())