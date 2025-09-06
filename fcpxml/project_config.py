#!/usr/bin/env python
# -----------------------------------------------------------------------------
# Version: 1.3.0
# Last Modified: 2025-09-03
# Purpose: Manage per-project configuration for the pipeline. Supports init,
#          list, show, set-active, and update. Defaults workspace to ./ai_workspace
#          and defaults base directory to the current working directory.
# -----------------------------------------------------------------------------

import argparse
import json
from pathlib import Path

from project_paths import (
    CONFIG_DIR,
    save_project_config,
    set_active_project_name,
    get_active_project_name,
    list_projects,
    load_project_config,
)

# Default workspace folder name created under the chosen base directory.
DEFAULT_WORKSPACE_SUBDIR = "ai_workspace"


def cmd_init(args):
    # Resolve base directory; default to the current working directory if not provided.
    base = Path(args.base).resolve() if args.base else Path.cwd()
    if not base.exists() or not base.is_dir():
        raise SystemExit(f"--base '{base}' does not exist or is not a directory")

    name = args.name
    ws = args.workspace_subdir if args.workspace_subdir is not None else DEFAULT_WORKSPACE_SUBDIR

    cfg = {
        "name": name,
        "base_project_directory": str(base),
        "workspace_subdir": ws,
    }

    path = save_project_config(name, cfg)
    if args.set_active:
        set_active_project_name(name)

    print(f"[+] Saved project config: {path}")
    if args.set_active:
        print(f"[+] Set active project: {name}")
    print(f"[i] Base directory: {base}")
    print(f"[i] Workspace subdir: {ws} (full: {base / ws})")


def cmd_list(_args):
    names = list_projects()
    active = get_active_project_name()
    print("Projects directory:", CONFIG_DIR)
    for n in sorted(names):
        mark = "*" if n == active else " "
        print(f" {mark} {n}")
    if not names:
        print("(none)")


def cmd_show(args):
    name = args.name or get_active_project_name()
    if not name:
        print("No active project set.")
        return
    cfg = load_project_config(name)
    if not cfg:
        print(f"Project '{name}' not found.")
        return
    print(json.dumps(cfg, indent=2))


def cmd_set_active(args):
    if not load_project_config(args.name):
        raise SystemExit(f"Project '{args.name}' does not exist.")
    set_active_project_name(args.name)
    print(f"[+] Active project set to: {args.name}")


def cmd_update(args):
    name = args.name or get_active_project_name()
    if not name:
        raise SystemExit("No project name provided and no active project set.")
    cfg = load_project_config(name)
    if not cfg:
        raise SystemExit(f"Project '{name}' not found.")

    if args.base:
        base = Path(args.base).resolve()
        if not base.exists() or not base.is_dir():
            raise SystemExit(f"--base '{base}' does not exist or is not a directory")
        cfg["base_project_directory"] = str(base)

    if args.workspace_subdir is not None:
        cfg["workspace_subdir"] = args.workspace_subdir

    path = save_project_config(name, cfg)
    print(f"[+] Updated project '{name}' at {path}")
    print(f"[i] Base directory: {cfg.get('base_project_directory')}")
    print(f"[i] Workspace subdir: {cfg.get('workspace_subdir')} "
          f"(full: {Path(cfg.get('base_project_directory', '.')) / cfg.get('workspace_subdir', DEFAULT_WORKSPACE_SUBDIR)})")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Manage project configurations for the pipeline.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create a new project config")
    p_init.add_argument("--name", required=True, help="Project name")
    p_init.add_argument(
        "--base",
        default=None,
        help="Project base directory (defaults to current working directory)"
    )
    p_init.add_argument(
        "--workspace-subdir",
        default=None,
        help=f"Optional workspace subdirectory under base for outputs (default: {DEFAULT_WORKSPACE_SUBDIR})"
    )
    p_init.add_argument("--set-active", action="store_true", help="Mark this project as active")
    p_init.set_defaults(func=cmd_init)

    p_list = sub.add_parser("list", help="List known projects")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show project config (defaults to active)")
    p_show.add_argument("--name", default=None)
    p_show.set_defaults(func=cmd_show)

    p_active = sub.add_parser("set-active", help="Set the active project by name")
    p_active.add_argument("name")
    p_active.set_defaults(func=cmd_set_active)

    p_update = sub.add_parser("update", help="Update an existing project config or the active one")
    p_update.add_argument("--name", default=None)
    p_update.add_argument("--base", default=None)
    p_update.add_argument("--workspace-subdir", default=None)
    p_update.set_defaults(func=cmd_update)

    return ap


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

# -----------------------------------------------------------------------------
# Changelog
# 1.3.0 (2025-09-03): Defaulted --base to CWD and workspace_subdir to 'ai_workspace'.
#                     Added validation echoes for full workspace path; kept behavior
#                     otherwise stable to avoid implicit directory scaffolding.
# -----------------------------------------------------------------------------
