#!/usr/bin/env python
# -----------------------------------------------------------------------------
# Version: 1.1.0
# Last Modified: 2025-09-03
# Purpose: Resolve Studio automation to export the current timeline as an
#          FCPXML file to the user's home directory.
#
# -----------------------------------------------------------------------------

import os
import sys
from typing import NoReturn

try:
    # Prefer the helper if present in your environment
    from python_get_resolve import GetResolve
    def _get_resolve():
        return GetResolve()
except ImportError:
    # Fallback to the official module name if helper isn't available
    import DaVinciResolveScript as dvr_script
    def _get_resolve():
        return dvr_script.scriptapp("Resolve")


def _fail(msg: str) -> NoReturn:
    """Prints an error message and exits the script with a failure code."""
    print(f"Error: {msg}")
    sys.exit(1)


def main():
    """Main execution function."""
    resolve = _get_resolve()
    if resolve is None:
        _fail("Cannot connect to DaVinci Resolve scripting API.")

    projectManager = resolve.GetProjectManager()
    project = projectManager.GetCurrentProject()
    if not project:
        _fail("No project is loaded. Open a project and retry.")

    timeline = project.GetCurrentTimeline()
    if not timeline:
        _fail("No active timeline. Open a timeline and retry.")

    print(f"Connected: Resolve v{resolve.GetVersionString()}")
    print(f"Project: '{project.GetName()}'")
    print(f"Timeline: '{timeline.GetName()}'")
    print("-" * 28)

    # --- Export Timeline to FCPXML ---
    print("--- Exporting Timeline to FCPXML ---")

    out_name = "info.fcpxml"
    
    # CORRECTED: Set the output path to the user's home directory.
    out_path = os.path.join(os.path.expanduser("~"), out_name)

    try:
        # Use the FCPXML version constant as the main exportType.
        export_type = resolve.EXPORT_FCPXML_1_10
    except AttributeError:
        _fail("Your Resolve API build is missing the EXPORT_FCPXML_1_10 enum.")

    print(f"Exporting to: {out_path}")
    
    # CORRECTED: Use the two-argument version of the Export call for FCPXML.
    ok = timeline.Export(out_path, export_type)

    if not ok:
        _fail("Timeline.Export() for FCPXML failed.")
    
    # Final check to ensure the file was actually created.
    if os.path.exists(out_path):
        print("FCPXML exported successfully.")
    else:
        _fail("Export reported success, but the output file was not found. (Silent failure)")

    print("-" * 28)
    print("--- Done ---")


if __name__ == "__main__":
    main()

# -----------------------------------------------------------------------------
# Changelog
# 1.1.0 (2025-09-03): Corrected output path to user's home directory and
#                      ensured two-argument Export call to fix silent failure.
# 1.0.3 (2025-09-03): Final test with documented 1.10 version.
# 1.0.2 (2025-09-03): Updated FCPXML subtype to 1.13.
# 1.0.1 (2025-09-03): Changed output path to current working directory.
# 1.0.0 (2025-09-03): Initial version.
# -----------------------------------------------------------------------------