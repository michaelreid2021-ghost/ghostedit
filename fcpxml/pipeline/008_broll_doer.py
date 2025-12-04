#!/usr/bin/env python3

import argparse
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
import json
import os

# Set up paths
script_dir = Path(__file__).resolve().parent
sys.path.append(str(script_dir.parent))
from project_paths import get_projects_root

# Set up logging
log_file_path = script_dir / "008_broll_doer.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)

def run_script(script_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Helper function to run a script and handle logging."""
    # Build command based on file extension
    if str(script_path).endswith(".ps1"):
        command = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script_path)] + list(args)
    else:
        # Python script
        command = [sys.executable, str(script_path)] + list(args)
    
    logging.info("Running command: %s", " ".join(command))
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        if result.stdout:
            logging.info("Script STDOUT: %s", result.stdout)
        if result.stderr:
            logging.warning("Script STDERR: %s", result.stderr)
        return result
    except subprocess.CalledProcessError as e:
        logging.error("Script failed: %s", script_path.name)
        if e.stdout:
            logging.error("STDOUT: %s", e.stdout)
        if e.stderr:
            logging.error("STDERR: %s", e.stderr)
        raise e
    except Exception as e:
        logging.error(f"Failed to run script {script_path.name}: {e}")
        raise e

def get_srt_path(project_dir: Path) -> Path:
    """Finds the source SRT, checking B-roll plans first."""
    broll_plans_dir = project_dir / "ai_working_files" / "46_ai_b_roll_plans"
    default_srt = project_dir / "ai_working_files" / 'subtitle 1.srt'
    
    if not broll_plans_dir.exists():
        return default_srt
    
    latest_plan = max(broll_plans_dir.glob("*_broll_plan.json"), key=lambda x: x.stat().st_mtime, default=None)
    if not latest_plan:
        return default_srt
    
    try:
        with open(latest_plan, 'r', encoding='utf-8') as f:
            plan = json.load(f)
        source_srt = plan.get("source_srt")
        if source_srt and Path(source_srt).exists():
            return Path(source_srt)
    except Exception as exc:
        logging.warning("Failed to read B-roll plan %s: %s", latest_plan, exc)
    
    return default_srt

def main() -> None:
    parser = argparse.ArgumentParser(description="Orchestrates B-Roll asset generation and FCPXML modification.")
    parser.add_argument("--project-dir", type=Path, required=True, help="The specific project directory.")
    parser.add_argument("--approved-dir", type=Path, required=True, help="Directory containing client-approved A-Roll folders.")
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    approved_dir = args.approved_dir.resolve()
    project_name = project_dir.name
    
    logging.info(f"Starting B-Roll Doer for project: {project_name}")

    # --- Script Paths ---
    load_timeline_script = script_dir / "200_load_timeline.py" 
    subtitle_script = script_dir / "210_generate_and_export_subtitles.py" 
    prod_order_script = script_dir / "41_production_order.py"
    pacing_script = script_dir / "45_srt_based_pacing_test.py"
    update_angles_script = script_dir / "240_update_angles.ps1"
    suggester_script = script_dir / "46_srt_b_roll_suggester.py"
    generator_script = script_dir / "60_generate_image_broll.py"
    
    # Switcher: Comment out the one you don't want
    # placeholder_script = script_dir / "118_apply_broll_placeholders.ps1" # Legacy (XML Manipulation)
    placeholder_script = script_dir / "119_apply_broll_resolve_api.py"   # Modern (Resolve API)

    # --- Project File Paths ---
    working_files_dir = project_dir / "ai_working_files"
    broll_plans_dir = working_files_dir / "46_ai_b_roll_plans"
    broll_assets_dir = working_files_dir / "60_generated_b_roll"
    
    approved_fcpxml_src = approved_dir / project_name / "Info.fcpxml"
    target_fcpxml = project_dir / "Info.fcpxml" 

    # --- Success Flags ---
    flag_load_timeline = working_files_dir / "200_load_timeline.success"
    flag_subtitle_gen = working_files_dir / "210_generate_and_export_subtitles.success"
    flag_subtitle_rename = working_files_dir / "008_srt_rename.success"
    flag_prod_order = working_files_dir / "41_production_order.success"
    flag_pacing = working_files_dir / "45_srt_based_pacing_test.success"
    flag_update_angles = working_files_dir / "240_update_angles.success"
    flag_suggester = working_files_dir / "46_srt_b_roll_suggester.success"
    flag_generator = working_files_dir / "60_generate_image_broll.success"
    flag_placeholder = working_files_dir / "119_apply_broll_resolve_api.success"
    flag_finalize = working_files_dir / "008_finalize_broll_v1.success"
    
    # Define intermediate/final file paths
    final_fcpxml_name = f"{project_name}_BROLL_v1.fcpxml"
    final_fcpxml_path = project_dir / final_fcpxml_name

    try:
        # Create directories
        broll_plans_dir.mkdir(parents=True, exist_ok=True)
        broll_assets_dir.mkdir(parents=True, exist_ok=True)
        
        # --- 1. Get Approved A-Roll FCPXML ---
        if not target_fcpxml.exists():
            if not approved_fcpxml_src.exists():
                raise FileNotFoundError(f"Approved FCPXML not found at {approved_fcpxml_src} or {target_fcpxml}")
            logging.info("Copying approved A-Roll FCPXML from watcher drop...")
            shutil.copy(approved_fcpxml_src, target_fcpxml)
        else:
            logging.info(f"Using existing A-Roll FCPXML: {target_fcpxml}")

        # --- 2a. Load Timeline ---
        if flag_load_timeline.exists():
            logging.info("Skipping Load Timeline (200) (.success flag found)")
        else:
            logging.info("Running Load Timeline (200)...")
            run_script(load_timeline_script)
            flag_load_timeline.touch()

        # --- 2b. Generate Subtitles ---
        if flag_subtitle_gen.exists():
            logging.info("Skipping Subtitle Generation (210) (.success flag found)")
        else:
            logging.info("Running Subtitle Generation (210)...")
            run_script(subtitle_script)
            flag_subtitle_gen.touch()

        # --- 3. Rename Subtitles ---
        srt_path = working_files_dir / "subtitle 1.srt"
        if flag_subtitle_rename.exists():
            logging.info("Skipping Subtitle Rename (.success flag found)")
        else:
            logging.info("--- Moving transcript into place for AI analysis ---")
            timeline_name = project_name
            generated_srt_name = f"{timeline_name}_subtitles.srt".replace(os.sep, "_")
            source_srt = working_files_dir / generated_srt_name
            
            if not source_srt.exists():
                if srt_path.exists():
                    logging.info("Found 'subtitle 1.srt' directly, no rename needed.")
                    flag_subtitle_rename.touch()
                else:
                    raise FileNotFoundError(f"Expected transcript '{source_srt.name}' OR '{srt_path.name}' was not found after running 210 script.")
            else:
                logging.info(f"Renaming '{source_srt.name}' to '{srt_path.name}'")
                source_srt.rename(srt_path)
                flag_subtitle_rename.touch()

        # --- 4. Find SRT ---
        srt_path = get_srt_path(project_dir) 
        if not srt_path.exists():
                raise FileNotFoundError(f"SRT file not found at {srt_path}. Generation or rename failed.")

        # --- 5. Run Production Order ---
        if flag_prod_order.exists():
            logging.info("Skipping Production Order (41) (.success flag found)")
        else:
            logging.info("Running Production Order (41)...")
            run_script(prod_order_script)
            flag_prod_order.touch()
            
        # --- 6. Run Pacing Test ---
        if flag_pacing.exists():
            logging.info("Skipping Pacing Test (45) (.success flag found)")
        else:
            logging.info("Running Pacing Test (45)...")
            run_script(pacing_script)
            flag_pacing.touch()
            
        # --- 7. Run Update Angles ---
        if flag_update_angles.exists():
            logging.info("Skipping Update Angles (240) (.success flag found)")
        else:
            logging.info("Running Update Angles (240)...")
            run_script(update_angles_script, "-ProjectDir", str(project_dir))
            flag_update_angles.touch()

        # --- 8. Run B-Roll Suggester ---
        if flag_suggester.exists():
            logging.info("Skipping B-Roll Suggester (46) (.success flag found)")
        else:
            logging.info("Running B-Roll Suggester (46)...")
            run_script(suggester_script)
            flag_suggester.touch()
            
        # --- 9. Run B-Roll Generator ---
        if flag_generator.exists():
            logging.info("Skipping B-Roll Generator (60) (.success flag found)")
        else:
            logging.info("Running B-Roll Generator (60)...")
            run_script(generator_script)
            
            # Validation
            generated_files = list(broll_assets_dir.glob("*.png")) + \
                              list(broll_assets_dir.glob("*.jpg")) + \
                              list(broll_assets_dir.glob("*.mp4"))
            if not generated_files:
                logging.warning("B-Roll Generator (60) ran but produced no assets. Continuing...")
            else:
                logging.info(f"B-Roll Generator (60) successfully created {len(generated_files)} assets.")
            flag_generator.touch()
        
        # --- 10. Run Placeholder Applier (118 or 119) ---
        if flag_placeholder.exists():
            logging.info("Skipping Placeholder Applier (.success flag found)")
        else:
            logging.info(f"Running Placeholder Applier ({placeholder_script.name})...")
            
            # --- AUTO-DETECT ARGUMENT STYLE ---
            if placeholder_script.suffix == ".ps1":
                # Legacy PowerShell: 118
                run_script(placeholder_script, "-ProjectDir", str(project_dir))
            else:
                # Modern Python/Argparse: 119
                # Uses project_paths.py convention which expects --base
                run_script(placeholder_script, "--base", str(project_dir))
            
            flag_placeholder.touch()
            
        # --- 11. Finalize FCPXML ---
        if flag_finalize.exists():
            logging.info("Skipping Finalize step (.success flag found)")
        else:
            logging.info("Finalizing B-Roll FCPXML...")
            if not target_fcpxml.exists() or target_fcpxml.stat().st_size == 0:
                # Note: 119 modifies the RESOLVE TIMELINE, not the FCPXML on disk.
                # So for 119, we just copy the original as the "base" record.
                pass 
            
            shutil.copy(target_fcpxml, final_fcpxml_path)
            flag_finalize.touch()
        
        logging.info(f"Successfully generated B-Roll FCPXML: {final_fcpxml_path}")

    except Exception as e:
        logging.error(f"B-Roll Doer failed for '{project_name}': {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()