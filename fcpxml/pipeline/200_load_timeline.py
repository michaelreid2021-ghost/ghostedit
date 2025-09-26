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
    args = parser.parse_args()
    p = from_args(args)

    fcpxml_path = p.info_fcpxml
    if not fcpxml_path.exists():
        sys.exit(f"Error: FCPXML not found at expected location: {fcpxml_path}")

    resolve = _get_resolve()
    if not resolve:
        sys.exit("Error: Cannot connect to DaVinci Resolve. Please ensure it is running.")

    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        sys.exit("Error: No project is loaded in DaVinci Resolve.")

    timeline_name = p.base.name
    print(f"Connected to Resolve v{resolve.GetVersionString()}")
    print(f"Ensuring timeline '{timeline_name}' is loaded in project '{project.GetName()}'.")

    # --- Make the timeline loading idempotent and non-destructive ---
    existing_timeline = None
    for i in range(1, int(project.GetTimelineCount()) + 1):
        timeline = project.GetTimelineByIndex(i)
        if timeline.GetName() == timeline_name:
            existing_timeline = timeline
            break

    if existing_timeline:
        print(f"Timeline '{timeline_name}' already exists. Setting it as active.")
        project.SetCurrentTimeline(existing_timeline)
        new_timeline = existing_timeline
    else:
        print(f"Timeline '{timeline_name}' not found. Importing from '{fcpxml_path.name}'.")
        mediaPool = project.GetMediaPool()
        import_options = {"timelineName": timeline_name, "importSourceClips": True}
        new_timeline = mediaPool.ImportTimelineFromFile(str(fcpxml_path), import_options)
        if not new_timeline:
            time.sleep(2) # Retry after a short delay
            new_timeline = mediaPool.ImportTimelineFromFile(str(fcpxml_path), import_options)
            if not new_timeline:
                sys.exit(f"Error: Failed to import timeline from '{fcpxml_path}'.")

    print(f"Successfully loaded timeline '{new_timeline.GetName()}'.")
    
    if project.SetCurrentTimeline(new_timeline):
        print(f"Successfully set '{new_timeline.GetName()}' as the active timeline.")
    else:
        # By this point, the timeline should exist, so this is a critical failure.
        sys.exit("Error: Could not set the imported timeline as active.")
    
    print("--- Timeline load complete ---")

if __name__ == "__main__":
    main()