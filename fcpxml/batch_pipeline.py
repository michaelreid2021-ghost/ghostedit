#!/usr/bin/env python3
# batch_pipeline.py
# Version: 1.1.0
# Last Modified: 2025-08-18
#
# Orchestrates the GhostEdit pipeline in two phases.
# Now includes error-aware checks to halt the pipeline if a step fails
# or produces invalid output (e.g., an empty manifest).

import argparse
import subprocess
import sys
from pathlib import Path
import json
from datetime import datetime
import shutil
from typing import Dict, Any

# ---------- Project Resolution ----------

def get_script_dir() -> Path:
    return Path(__file__).resolve().parent

def read_active_project_name(projects_dir: Path) -> str:
    ap = projects_dir / "active_project.txt"
    if not ap.exists():
        raise RuntimeError(f"Active project file not found: {ap}")
    name = ap.read_text(encoding="utf-8").strip().strip("'\"")
    if not name:
        raise RuntimeError(f"Active project name is empty in: {ap}")
    return name

def read_project_config(projects_dir: Path, project_name: str) -> Dict[str, Any]:
    cfg = projects_dir / f"{project_name}.json"
    if not cfg.exists():
        raise RuntimeError(f"Project config not found: {cfg}")
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Failed to parse project config {cfg}: {e}") from e
    for k in ("base_project_directory", "workspace_subdir"):
        if k not in data or not str(data[k]).strip():
            raise RuntimeError(f"Missing '{k}' in project config: {cfg}")
    return data

def resolve_project_and_working_dirs() -> tuple[Path, Path]:
    script_dir = get_script_dir()
    projects_dir = script_dir / "projects"
    project_name = read_active_project_name(projects_dir)
    cfg = read_project_config(projects_dir, project_name)
    project_root = Path(cfg["base_project_directory"]).resolve()
    working_dir = (project_root / cfg["workspace_subdir"]).resolve()
    return project_root, working_dir

# ---------- Utilities ----------

def run_step(script: str, args: list[str], dry_run: bool = False) -> None:
    """Executes a pipeline step and raises an error if it fails."""
    script_path = get_script_dir() / script
    cmd = [sys.executable, str(script_path), *args]
    print(f"\n$ {' '.join([str(c) for c in cmd])}")
    if dry_run:
        print("[dry-run] skipped execution")
        return
    # The 'check=True' flag will raise CalledProcessError on non-zero exit codes.
    subprocess.run(cmd, check=True)

def check_manifest_validity(manifest_path: Path) -> None:
    """Checks if a manifest file is valid and raises an error if not."""
    print(f"[validate] Checking manifest: {manifest_path}")
    if not manifest_path.exists():
        raise RuntimeError(f"Manifest file was not created at the expected path.")
    
    try:
        content = manifest_path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError("Manifest file is empty.")
        
        data = json.loads(content)
        if not isinstance(data, list):
            raise ValueError("Manifest content is not a JSON list.")
        
        if not data:
            raise ValueError("Manifest is an empty list. No assets were cataloged.")
            
    except Exception as e:
        raise RuntimeError(f"Manifest validation failed: {e}")
    
    print("[validate] Manifest is valid and contains data.")


def backup_and_swap_fcpxml(working_dir: Path, dry_run: bool = False) -> None:
    info = working_dir / "Info.fcpxml"
    adjusted = working_dir / "26_adjusted.fcpxml"
    if not adjusted.exists():
        raise FileNotFoundError(f"Adjusted FCPXML not found: {adjusted}")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = working_dir / f"Info.pre25.{ts}.fcpxml"
    print(f"\n[backup] {info} -> {backup}")
    if not dry_run:
        if info.exists():
            shutil.copy2(info, backup)
        else:
            print("[warn] Info.fcpxml not found; skipping backup copy")
    print(f"[swap] {adjusted} -> {info}")
    if not dry_run:
        shutil.copy2(adjusted, info)

# ---------- Pipeline Phases ----------

def phase_a(args: argparse.Namespace, project_root: Path, working_dir: Path) -> None:
    # 01_dehydrate.py (uses working dir)
    run_step("01_dehydrate.py", ["--base", str(working_dir)], args.dry_run)
    if not args.dry_run:
        manifest_path = working_dir / "ai_working_files" / "master_manifest.json"
        check_manifest_validity(manifest_path)

    # 03_transcription.py (uses PROJECT ROOT to find proxies\*.mp4)
    if not args.skip_03:
        run_step("03_transcription.py", ["--base", str(working_dir)], args.dry_run)
    else:
        print("\n[skip] 03_transcription.py")

    # 10_fcpxml_ripper.py (working dir)
    run_step("10_fcpxml_ripper.py", ["--base", str(working_dir)], args.dry_run)

    # 15_enriched_manifest.py (working dir)
    run_step("15_enriched_manifest.py", ["--base", str(working_dir)], args.dry_run)

    # 20_detect_chatter.py (working dir)
    run_step(
        "20_detect_chatter.py",
        ["--base", str(working_dir), "--nudge", f"{args.nudge20:.6f}"],
        args.dry_run,
    )

    # 25_rebaseline_offsets.py (working dir)
    step25_args = [
        "--source", args.source25,
        "--nudge", f"{args.nudge25:.6f}",
        "--baseline", args.baseline25,
    ]
    if args.contiguity:
        step25_args.append("--contiguity")
    if args.audit_baselines:
        step25_args.append("--audit-baselines")
        if args.threshold_seconds is not None:
            step25_args += ["--threshold-seconds", f"{args.threshold_seconds:.6f}"]
    run_step("25_rebaseline_offsets.py", step25_args, args.dry_run)

    # Backup & swap Info.fcpxml
    backup_and_swap_fcpxml(working_dir, args.dry_run)

def phase_b(args: argparse.Namespace, working_dir: Path) -> None:
    # 01_dehydrate.py (refresh after swap)
    run_step("01_dehydrate.py", ["--base", str(working_dir)], args.dry_run)
    if not args.dry_run:
        manifest_path = working_dir / "ai_working_files" / "master_manifest.json"
        check_manifest_validity(manifest_path)

    # 10_fcpxml_ripper.py
    run_step("10_fcpxml_ripper.py", ["--base", str(working_dir)], args.dry_run)

    # 15_enriched_manifest.py
    run_step("15_enriched_manifest.py", ["--base", str(working_dir)], args.dry_run)

    # 40_classify_takes.py
    run_step("40_classify_takes.py", ["--base", str(working_dir)], args.dry_run)

    # 41_production_order.py
    run_step("41_production_order.py", ["--base", str(working_dir)], args.dry_run)

    # 45_pacing_analyzer.py
    run_step("45_pacing_analyzer.py", ["--base", str(working_dir)], args.dry_run)

    # 47_build_angle_changes.py (optional)
    if args.include_47:
        run_step("47_build_angle_changes.py", ["--base", str(working_dir)], args.dry_run)
    else:
        print("\n[skip] 47_build_angle_changes.py")

    # 50_final_merge.py
    run_step("50_final_merge.py", ["--base", str(working_dir)], args.dry_run)

# ---------- CLI ----------

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="GhostEdit batch wrapper: Phase A (01,03,10,15,20,25) → swap → Phase B (01,10,15,40,41,45,[47],50)",
        allow_abbrev=False,
    )
    ap.add_argument("--skip-03", action="store_true", help="Skip Step 03_transcription.py in Phase A")
    ap.add_argument("--include-47", action="store_true", help="Include Step 47_build_angle_changes.py in Phase B")
    ap.add_argument("--dry-run", action="store_true", help="Print commands without executing")

    # Step 20 controls
    ap.add_argument("--nudge20", type=float, default=-0.01, help="Step 20: signed seconds nudge (default: -0.01)")

    # Step 25 controls
    ap.add_argument("--source25", choices=["proposed", "first", "two", "midpoint"], default="proposed",
                    help="Step 25: which Step 20 field to use (default: proposed)")
    ap.add_argument("--nudge25", type=float, default=0.0, help="Step 25: signed seconds nudge (default: 0.00)")
    ap.add_argument("--baseline25", choices=["auto", "current", "tcStart", "override"], default="tcStart",
                    help="Step 25: baseline for desired start (default: tcStart)")
    ap.add_argument("--contiguity", action="store_true", default=True,
                    help="Step 25: pack spine contiguously after ripple (default: ON)")
    ap.add_argument("--no-contiguity", action="store_true",
                    help="Disable contiguity pass (overrides --contiguity)")

    # Audits
    ap.add_argument("--audit-baselines", action="store_true",
                    help="Step 25: log (current_start - tcStart) and flag beyond threshold")
    ap.add_argument("--threshold-seconds", type=float, default=None,
                    help="Step 25: audit threshold seconds (overrides project policy if set)")

    # Debug
    ap.add_argument("--print-paths", action="store_true", help="Print resolved paths (project root, working files, key inputs)")
    return ap

def main() -> None:
    ap = build_arg_parser()
    args = ap.parse_args()

    if args.no_contiguity:
        args.contiguity = False

    try:
        project_root, working_dir = resolve_project_and_working_dirs()
        script_dir = get_script_dir()

        if args.print_paths:
            info = working_dir / "Info.fcpxml"
            adjusted = working_dir / "26_adjusted.fcpxml"
            countdown = working_dir / "20_countdown_offsets.json"
            print(f"[i] Script dir:         {script_dir}")
            print(f"[i] Project root:       {project_root}")
            print(f"[i] Working dir:        {working_dir}")
            print(f"[i] Info.fcpxml:        {info}")
            print(f"[i] Adjusted:           {adjusted}")
            print(f"[i] Countdown:          {countdown}")
            return # Exit after printing paths

        print("\n=== Phase A: 01 → 03 → 10 → 15 → 20 → 25 ===")
        phase_a(args, project_root, working_dir)

        print("\n=== Phase B: 01 → 10 → 15 → 40 → 41 → 45 → [47] → 50 ===")
        phase_b(args, working_dir)

        print("\n[done] Batch pipeline complete.")
    except (subprocess.CalledProcessError, RuntimeError, FileNotFoundError) as e:
        print(f"\n[!] PIPELINE HALTED. An error occurred:", file=sys.stderr)
        print(f"[!] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[!] An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
