
import argparse
import shutil
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Resets a project by moving it from IN_PROGRESS to TODO.")
    parser.add_argument("--project-name", required=True, help="The name of the project to reset.")
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent # /e/ghostedit
    in_progress_dir = project_root / "assembly_line" / "IN_PROGRESS"
    todo_dir = project_root / "assembly_line" / "TODO"

    source_path = in_progress_dir / args.project_name
    destination_path = todo_dir / args.project_name

    if not source_path.exists():
        print(f"Error: Project '{args.project_name}' not found in IN_PROGRESS: {source_path}")
        return

    print(f"Moving project '{args.project_name}' from IN_PROGRESS to TODO...")
    try:
        shutil.move(str(source_path), str(destination_path))
        print(f"Successfully moved '{args.project_name}' to {destination_path}")
        
        # Clean up any potential trigger files or error markers in the new TODO location
        # (though ideally these would be handled by the doer on failure)
        trigger_file = destination_path / ".project_ready_for_assembly"
        if trigger_file.exists():
            trigger_file.unlink()
            print(f"Removed trigger file: {trigger_file}")

        aroll_failed_file = destination_path / ".aroll_failed"
        if aroll_failed_file.exists():
            aroll_failed_file.unlink()
            print(f"Removed error marker: {aroll_failed_file}")

    except Exception as e:
        print(f"Error moving project '{args.project_name}': {e}")

if __name__ == "__main__":
    main()
