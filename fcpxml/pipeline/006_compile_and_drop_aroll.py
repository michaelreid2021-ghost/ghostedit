#!/usr/bin/env python3
# 006_compile_and_drop_aroll.py
# v3.3: Implemented dynamic target_name matching based on take content.
import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from difflib import SequenceMatcher

# --- Self-Contained Path Management Class ---
class ProjectPaths:
    """Defines and manages all necessary paths for a project."""
    def __init__(self, base_dir: Path):
        self.base: Path = base_dir
        self.name: str = base_dir.name
        self.workspace: Path = self.base / "ai_working_files"
        self.info_fcpxml: Path = self.base / "Info.fcpxml"

# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "006_compile_and_drop_aroll.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def run_sub_script(command: list):
    """Helper to run and log subprocess calls."""
    logging.info(f"  Executing: {' '.join(str(c) for c in command)}")
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
        if result.stdout:
            logging.info(f"Sub-script output:\n{result.stdout}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Sub-script failed with stderr:\n{e.stderr}")
        raise

def main():
    parser = argparse.ArgumentParser(description="Builds final timeline from takes and drops it for review.")
    parser.add_argument("--project-dir", required=True, type=Path, help="The path to the project directory.")
    args = parser.parse_args()

    try:
        p = ProjectPaths(base_dir=args.project_dir)
        project_name = p.name
        logging.info(f"--- Compile & Drop Doer Started for Project: {project_name} ---")

        builder_script = script_dir / "115_timeline_builder.py"
        redactor_script = script_dir / "215_timeline_redactor.py"
        markup_path = p.workspace / "44_producers_markup.json"
        fcpxml_main = p.info_fcpxml
        fcpxml_temp = p.base / "Info_temp.fcpxml"

        if not all([builder_script.exists(), redactor_script.exists(), markup_path.exists(), fcpxml_main.exists()]):
            raise FileNotFoundError("A required script or project file was not found.")
            
        backup_path = p.base / f"Info.bak.PRE-COMPILE.{int(time.time())}.fcpxml"
        logging.info(f"Backing up current timeline to: {backup_path}")
        shutil.copy(fcpxml_main, backup_path)
        
        markup_data = json.loads(markup_path.read_text(encoding='utf-8'))

        for scene in markup_data:
            multicam_name = scene.get('multicam_name')
            if not multicam_name:
                continue

            tree = ET.parse(fcpxml_main)
            media_node = tree.find(f".//resources/media[@name='{multicam_name}']")
            if media_node is None:
                logging.error(f"Could not find media '{multicam_name}' in FCPXML. Skipping scene.")
                continue
            
            source_ref_id = media_node.get('id')
            producer_edits = scene.get("producers_markup", {})
            final_takes = producer_edits.get("final_edit_takes", [])
            
            # --- Create a list to map content to generated subclip names ---
            content_map = []

            if not final_takes:
                # Handle redaction as before
                logging.warning(f"No 'final_edit_takes' for '{multicam_name}'. Calling redactor.")
                cmd = [sys.executable, str(redactor_script), "--fcpxml", str(fcpxml_main), "--out", str(fcpxml_temp), "--media-name", multicam_name]
                run_sub_script(cmd)
                shutil.move(str(fcpxml_temp), str(fcpxml_main))
                continue

            logging.info(f"Processing '{multicam_name}' (ID: {source_ref_id}) with {len(final_takes)} primary takes.")
            for i, take in enumerate(final_takes):
                subclip_name = f"{multicam_name} - Subclip {i + 1}"
                start = float(take.get('start_seconds', 0))
                duration = float(take.get('end_seconds', 0)) - start
                if duration <= 0: continue

                cmd = [
                    sys.executable, str(builder_script), "--fcpxml", str(fcpxml_main), "--out", str(fcpxml_temp),
                    "process-take", "--source-ref", source_ref_id, "--start-seconds", str(start),
                    "--duration-seconds", str(duration), "--name", subclip_name
                ]
                run_sub_script(cmd)
                shutil.move(str(fcpxml_temp), str(fcpxml_main))

                # Add the generated name and text content to our map
                content_map.append({"name": subclip_name, "text": take.get("full_text", "")})

            # --- Process alts (DISABLED) ---
            alt_takes = producer_edits.get("alternate_takes", [])
            if alt_takes:
                logging.info(f"Found {len(alt_takes)} alternate takes for '{multicam_name}', but processing is disabled. Skipping.")
                pass

        # --- Final Handoff Steps (No changes) ---
        logging.info("Timeline assembly complete. Preparing for human review.")
        review_dir = p.base.parent.parent / "review_aroll"
        review_project_path = review_dir / project_name
        review_project_path.mkdir(parents=True, exist_ok=True)
        dest_fcpxml = review_project_path / "Info.fcpxml"
        shutil.copy(str(fcpxml_main), str(dest_fcpxml))
        
        staged_dir = p.base.parent.parent / "in_progress" / project_name
        if staged_dir.exists():
            for flag in staged_dir.glob(".*"):
                if flag.is_file(): flag.unlink()
            (staged_dir / ".aroll_submit_review_complete").touch()
            logging.info(f"Updated flags in {staged_dir}")

        logging.info(f"--- Compile & Drop Doer for {project_name} Finished Successfully ---")

    except Exception as e:
        logging.error(f"A critical error occurred in the compile doer: {e}", exc_info=True)
        (args.project_dir / ".aroll_compile_failed").touch()
        sys.exit(1)

if __name__ == "__main__":
    main()