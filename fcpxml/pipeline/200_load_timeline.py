#!/usr/bin/env python3
# 200_load_timeline.py
import sys
import time
import argparse
from pathlib import Path

# Pipeline integration
from project_paths import add_base_arg, from_args

try:
    import DaVinciResolveScript as dvr_script
    def _get_resolve(): return dvr_script.scriptapp("Resolve")
except ImportError:
    print("FATAL: DaVinciResolveScript module not found.", file=sys.stderr)
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Load a project's FCPXML into the current Resolve project.")
    add_base_arg(parser)
    # No extra arguments required. We infer context from the base path.
    args = parser.parse_args()
    p = from_args(args)

    fcpxml_path = p.info_fcpxml
    if not fcpxml_path.exists():
        sys.exit(f"Error: FCPXML not found at expected location: {fcpxml_path}")

    resolve = _get_resolve()
    if not resolve:
        sys.exit("Error: Cannot connect to DaVinci Resolve. Please ensure it is running.")

    # INFER Project Name from the pipeline base name (e.g., "rebecca_green_general")
    target_name = p.base.name 
    project_manager = resolve.GetProjectManager() # [cite: 28]
    
    print(f"Connected to Resolve v{resolve.GetVersionString()}") # [cite: 35]

    # --- 1. Ensure Correct Project is Open ---
    current_project = project_manager.GetCurrentProject() # [cite: 54]
    
    if current_project and current_project.GetName() == target_name:
        print(f"Project '{target_name}' is already active.")
        project = current_project
    else:
        print(f"Attempting to load project '{target_name}'...")
        project = project_manager.LoadProject(target_name) # [cite: 52]
        
        if not project:
            print(f"Project '{target_name}' not found. Creating it...")
            project = project_manager.CreateProject(target_name) # [cite: 51]
            if not project:
                sys.exit(f"Error: Failed to create or load project '{target_name}'.")

    # --- 2. Clean Slate Timeline ---
    # We want to 'unload' (delete) the timeline if it exists so we can reload the FCPXML fresh.
    media_pool = project.GetMediaPool() # [cite: 81]
    timeline_count = project.GetTimelineCount() # [cite: 82]
    
    # Check if the timeline already exists
    existing_timeline = None
    for i in range(1, timeline_count + 1):
        t = project.GetTimelineByIndex(i) # [cite: 83]
        if t.GetName() == target_name:
            existing_timeline = t
            break
    
    if existing_timeline:
        print(f"Timeline '{target_name}' exists. Unloading (deleting) to force fresh import...")
        # DeleteTimelines takes a list of timeline objects
        if not media_pool.DeleteTimelines([existing_timeline]): # 
            sys.exit(f"Error: Failed to remove stale timeline '{target_name}'.")
    else:
        print(f"Timeline '{target_name}' not found (Clean slate confirmed).")

    # --- 3. Import FCPXML ---
    print(f"Importing from '{fcpxml_path.name}'...")
    
    # We enforce the timeline name to match our project/base name
    import_options = {
        "timelineName": target_name, 
        "importSourceClips": True
    } # [cite: 154, 155]
    
    new_timeline = media_pool.ImportTimelineFromFile(str(fcpxml_path), import_options) # 

    if not new_timeline:
        # Retry once for stability
        time.sleep(1)
        new_timeline = media_pool.ImportTimelineFromFile(str(fcpxml_path), import_options)
        if not new_timeline:
             sys.exit(f"Error: Import failed. DaVinci could not read '{fcpxml_path}'. check file validity.")

    # --- 4. Set Active ---
    if project.SetCurrentTimeline(new_timeline): # [cite: 84]
        print(f"Success: '{new_timeline.GetName()}' is now the active timeline.")
    else:
        sys.exit("Error: Imported timeline could not be set as active.")

if __name__ == "__main__":
    main()