#!/usr/bin/env python
# v1.2.0
# Last modified: 2025-09-03
# Purpose: Export current DaVinci Resolve timeline to AAF, CSV, and FCPXML using strict, non-fallback behavior.

import os
import sys
from python_get_resolve import GetResolve

def export_strict(timeline, path, export_type):
    # DaVinci is picky about relative paths; always use absolute.
    abs_path = path
    print(f"abs_path: {abs_path}")
    out_dir = os.path.dirname(abs_path)
    print(f"out_dir: {out_dir}")
    #if out_dir and not os.path.exists(out_dir):
    #    os.makedirs(out_dir, exist_ok=True)
    #    print(f"make dir: {out_dir}")
    ok = bool(timeline.Export(abs_path, export_type))
    print(f"{'SUCCESS' if ok else 'FAIL'}: {abs_path}")
    return ok

def main():
    resolve = GetResolve()
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        print("No project is loaded")
        sys.exit(1)

    timeline = project.GetCurrentTimeline()
    if not timeline:
        print("No timeline is active")
        sys.exit(1)

    # Absolute, deterministic paths
    home = os.path.expanduser("~")
    aaf_path = os.path.join(home, "sampleExp.aaf")
    csv_path = os.path.join(home, "sampleExp.csv")

    # Write FCPXML next to this script (strict, no fallbacks)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"script: {script_dir}")
    fcpxml_path = os.path.join(script_dir, "phase_2")
    print(f"script: {fcpxml_path}")
    # Exports (no subtype, no version fallback)
    fcpxml_ok = export_strict(timeline, fcpxml_path, resolve.EXPORT_FCPXML_1_10)

    # Exit non-zero if any failed (strict behavior)
    if not (fcpxml_ok):
        sys.exit(2)

if __name__ == "__main__":
    main()

# Changelog:
# 1.2.0 (2025-09-03): Removed all fallbacks; force absolute paths; strict non-zero exit on any failure.
