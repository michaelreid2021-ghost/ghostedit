
import subprocess
import sys
import time
from pathlib import Path
import os
import fcntl
import argparse

def main():
    """
    Launches the watcher scripts for the GhostEdit Assembly Line.
    Monitors them and exits if any watcher fails.
    """
    parser = argparse.ArgumentParser(description="Run the GhostEdit watchers.")
    parser.add_argument("--ffmpeg-dir", type=Path, required=True, help="Path to the directory containing ffmpeg and ffprobe.")
    args = parser.parse_args()

    project_root = Path(__file__).parent
    pipeline_dir = project_root / "fcpxml" / "pipeline"
    
    # Define assembly line directories
    todo_dir = project_root / "assembly_line" / "TODO"
    in_progress_dir = project_root / "assembly_line" / "IN_PROGRESS"
    review_aroll_dir = project_root / "assembly_line" / "REVIEW_AROLL"
    # The doer script will create the project structure inside the in_progress_dir
    projects_base_dir = in_progress_dir

    # Create directories if they don't exist
    todo_dir.mkdir(parents=True, exist_ok=True)
    in_progress_dir.mkdir(parents=True, exist_ok=True)
    review_aroll_dir.mkdir(parents=True, exist_ok=True)

    # --- Launch Station 1: Intake Watcher ---
    intake_watcher_script = pipeline_dir / "001_intake_watcher.py"
    intake_doer_script = pipeline_dir / "002_setup_project.py"
    
    intake_cmd = [
        sys.executable,
        str(intake_watcher_script),
        "--watch-dir", str(todo_dir),
        "--in-progress-dir", str(in_progress_dir),
        "--doer-script-path", str(intake_doer_script),
        "--projects-base-dir", str(projects_base_dir),
        "--ffmpeg-dir", str(args.ffmpeg_dir),
    ]
    
    print("Starting Intake Watcher...")
    intake_process = subprocess.Popen(intake_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(f"Intake Watcher PID: {intake_process.pid}")

    # --- Launch Station 2: A-Roll Watcher ---
    aroll_watcher_script = pipeline_dir / "003_aroll_watcher.py"
    aroll_doer_script = pipeline_dir / "004_aroll_doer.py"

    aroll_cmd = [
        sys.executable,
        str(aroll_watcher_script),
        "--watch-dir", str(in_progress_dir),
        "--review-dir", str(review_aroll_dir),
        "--doer-script-path", str(aroll_doer_script),
        "--ffmpeg-dir", str(args.ffmpeg_dir),
    ]

    print("Starting A-Roll Watcher...")
    aroll_process = subprocess.Popen(aroll_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(f"A-Roll Watcher PID: {aroll_process.pid}")

    # Set stdout to non-blocking
    if intake_process.stdout:
        fd = intake_process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    if aroll_process.stdout:
        fd = aroll_process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


    print("\n--- Watchers are running in the background. ---")
    print(f"  Intake Watcher watching: {todo_dir}")
    print(f"  A-Roll Watcher watching: {in_progress_dir}")
    print("\nMonitoring watcher output. Press Ctrl+C to stop.")

    try:
        while True:
            # Check Intake Watcher
            if intake_process.poll() is not None:
                exit_code = intake_process.poll()
                if exit_code != 0:
                    print("\n--- FATAL: Intake Watcher exited with an error. ---", file=sys.stderr)
                    # Print any remaining output
                    if intake_process.stdout:
                        for line in intake_process.stdout.readlines():
                            print(f"[Intake Watcher ERROR]: {line.strip()}", file=sys.stderr)
                    aroll_.terminate()
                    sys.exit(1)
                else:
                    print("Intake Watcher exited cleanly.")
                    # If intake watcher exits cleanly, we can probably exit the whole thing.
                    aroll_process.terminate()
                    sys.exit(0)


            # Check A-Roll Watcher
            if aroll_process.poll() is not None:
                exit_code = aroll_process.poll()
                if exit_code != 0:
                    print("\n--- FATAL: A-Roll Watcher exited with an error. ---", file=sys.stderr)
                    if aroll_process.stdout:
                        for line in aroll_process.stdout.readlines():
                            print(f"[A-Roll Watcher ERROR]: {line.strip()}", file=sys.stderr)
                    intake_process.terminate()
                    sys.exit(1)
                else:
                    print("A-Roll Watcher exited cleanly.")
                    intake_process.terminate()
                    sys.exit(0)

            # Print output if available
            try:
                if intake_process.stdout:
                    intake_output = intake_process.stdout.read()
                    if intake_output:
                        print(f"[Intake Watcher]: {intake_output.strip()}")
                
                if aroll_process.stdout:
                    aroll_output = aroll_process.stdout.read()
                    if aroll_output:
                        print(f"[A-Roll Watcher]: {aroll_output.strip()}")
            except TypeError:
                pass

            # Small sleep to prevent busy-waiting
            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n--- Shutting down watchers... ---")
        intake_process.terminate()
        aroll_process.terminate()
        intake_process.wait()
        aroll_process.wait()
        print("Watchers stopped.")

if __name__ == "__main__":
    main()
