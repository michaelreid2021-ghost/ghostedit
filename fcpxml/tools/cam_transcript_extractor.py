#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: 45_clean_transcript_inspector.py
Version: 3.2.0
Last Modified: 2025-08-28
Purpose: Extracts timed transcript lines that fall within the 'final_edit_takes'
         time windows defined in a producer's markup file.

Usage:
  python 45_clean_transcript_inspector.py \
      --base /path/to/project/root_or_ai_working_files \
      --markup_file /path/to/44_producers_markup.json \
      --transcripts_file /path/to/scene_manifest_transcribed.json
"""

import json
import argparse
from pathlib import Path

from project_paths import add_base_arg, from_args

# -------------------------------
# Helpers
# -------------------------------
def resolve_project_root(p_base: Path) -> Path:
    """Resolve project root from a provided base."""
    return p_base.parent if p_base.name.lower() == "ai_working_files" else p_base

def is_time_in_windows(time_to_check: float, windows: list[tuple[float, float]]) -> bool:
    """Checks if a given time falls within any of the (start, end) time windows."""
    for start, end in windows:
        if start <= time_to_check <= end:
            return True
    return False

# -------------------------------
# Main
# -------------------------------
def main(cfg: argparse.Namespace) -> None:
    print("[+] Starting clean transcript line extraction for inspection…")

    p = from_args(cfg)
    output_dir = Path(cfg.output_dir or p.plans_dir)
    markup_file = Path(cfg.markup_file)
    transcripts_file = Path(cfg.transcripts_file or p.scene_manifest)

    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load the markup file and extract the 'final_edit_takes' time windows
    try:
        with open(markup_file, "r", encoding="utf-8") as f:
            markup_data = json.load(f)

        # --- THIS BLOCK IS NOW CORRECTED ---
        if not isinstance(markup_data, list) or not markup_data:
             raise ValueError("Markup file is not a list or is empty.")
        
        # Get the first scene object from the list
        first_scene = markup_data[0]
        
        # Navigate using the CORRECT keys from your file: "producers_markup" -> "final_edit_takes"
        final_takes = first_scene["producers_markup"]["final_edit_takes"]
        # --- END CORRECTION ---

        clean_windows = [
            (float(take.get("start_seconds", 0.0)), float(take.get("end_seconds", 0.0)))
            for take in final_takes
        ]
        print(f"[i] Loaded {len(clean_windows)} clean time windows from '{markup_file.name}'.")
    except (FileNotFoundError, KeyError, TypeError, IndexError, ValueError) as e:
        print(f"[!] FATAL: Could not load or parse clean take windows from '{markup_file}'. Error: {e}")
        return

    # 2. Load the detailed timed transcript
    try:
        with open(transcripts_file, "r", encoding="utf-8") as f:
            scenes_data = json.load(f)
        if not scenes_data or not isinstance(scenes_data, list):
             raise ValueError("Transcript file is not a valid list of scenes.")
        timed_transcript = scenes_data[0].get("timed_transcript", [])
        if not timed_transcript:
             raise ValueError("No 'timed_transcript' found in the first scene.")
        print(f"[i] Loaded {len(timed_transcript)} transcript lines from '{transcripts_file.name}'.")
    except (FileNotFoundError, IndexError, ValueError, json.JSONDecodeError) as e:
        print(f"[!] FATAL: Could not load timed transcript from '{transcripts_file}'. Error: {e}")
        return

    # 3. Filter the transcript lines against the clean windows
    output_lines: list[str] = []
    for segment in timed_transcript:
        if not isinstance(segment, dict):
            continue

        start_time = float(segment.get("start", -1.0))
        text = (segment.get("text") or "").strip()

        if text and is_time_in_windows(start_time, clean_windows):
            output_lines.append(f"[{start_time:.2f}s] {text}")

    # 4. Write the filtered lines to the output file
    if not output_lines:
        print("[i] No transcript lines were found within the provided clean take windows.")
        return

    out_path = output_dir / "clean_transcript_lines.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines) + "\n")

    print("\n[+] Done.")
    print(f"    Lines extracted: {len(output_lines)}")
    print(f"    Output file created: '{out_path}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extracts timed transcript lines that fall within clean take windows.", allow_abbrev=False)
    add_base_arg(parser)
    parser.add_argument("--markup_file", required=True, type=str, help="Path to the JSON file containing producer's markup (e.g., 44_producers_markup.json).")
    parser.add_argument("--transcripts_file", type=str, default=None, help="Path to the original scene_manifest_transcribed.json file.")
    parser.add_argument("--output_dir", type=str, default=None, help="Directory for the output .txt file.")
    args = parser.parse_args()
    main(args)