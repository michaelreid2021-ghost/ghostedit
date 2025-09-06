#!/usr/bin/env python
# -----------------------------------------------------------------------------
# Version: 1.0.0
# Last Modified: 2025-09-03
# Purpose: Print the currently loaded DaVinci Resolve project (and related info).
# -----------------------------------------------------------------------------

import sys
import json
import argparse

def _get_resolve():
    try:
        from python_get_resolve import GetResolve  # preferred helper
        return GetResolve()
    except Exception:
        try:
            import DaVinciResolveScript as dvr_script  # official module
            return dvr_script.scriptapp("Resolve")
        except Exception:
            return None

def main():
    ap = argparse.ArgumentParser(description="Show the currently loaded Resolve project.")
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    args = ap.parse_args()

    resolve = _get_resolve()
    if resolve is None:
        msg = "Unable to connect to DaVinci Resolve scripting API."
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"Error: {msg}")
        sys.exit(2)

    pm = resolve.GetProjectManager()
    proj = pm.GetCurrentProject()

    # Gather basic context (safe to attempt even if no project loaded)
    version = ""
    try:
        version = resolve.GetVersionString()
    except Exception:
        version = ""

    db_info = {}
    try:
        db_info = pm.GetCurrentDatabase() or {}
    except Exception:
        db_info = {}

    folder_name = ""
    try:
        folder_name = pm.GetCurrentFolder() or ""
    except Exception:
        folder_name = ""

    if not proj:
        data = {
            "ok": False,
            "resolve_version": version,
            "database": db_info,
            "folder": folder_name,
            "project": None,
            "timeline": None,
            "message": "No project is currently loaded."
        }
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print("Resolve Version:", version or "(unknown)")
            if db_info:
                print("Database:", f"{db_info.get('DbType','?')}::{db_info.get('DbName','?')}",
                      f"@{db_info.get('IpAddress','127.0.0.1')}" if db_info.get("IpAddress") else "")
            print("Folder:", folder_name or "(unknown)")
            print("No project is currently loaded.")
        sys.exit(1)

    # Project is loaded; include current timeline if available
    timeline_name = None
    try:
        tl = proj.GetCurrentTimeline()
        timeline_name = tl.GetName() if tl else None
    except Exception:
        timeline_name = None

    data = {
        "ok": True,
        "resolve_version": version,
        "database": db_info,
        "folder": folder_name,
        "project": proj.GetName(),
        "timeline": timeline_name
    }

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print("Resolve Version:", version or "(unknown)")
        if db_info:
            print("Database:", f"{db_info.get('DbType','?')}::{db_info.get('DbName','?')}",
                  f"@{db_info.get('IpAddress','127.0.0.1')}" if db_info.get("IpAddress") else "")
        print("Folder:", folder_name or "(unknown)")
        print("Project:", data["project"])
        print("Timeline:", timeline_name or "(none)")

if __name__ == "__main__":
    main()
