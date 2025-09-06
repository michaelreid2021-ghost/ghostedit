#!/usr/bin/env python3
# Version: 2.4.0
# Last Modified: 2025-08-13
# Purpose: Step 20 — Detect head countdown chatter from transcripts and propose per-Take start offsets.
#          Improvements:
#            - First content time is the first word start strictly AFTER the "two" end, even if in the same segment.
#            - Adds a signed --nudge (default -0.01s) applied to the proposed start to compensate transcript timing bias.
#            - Keeps outputs backward-compatible with existing pipeline.

import json
import re
import os
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional
from project_paths import add_base_arg, from_args

TRANSCRIBED_MANIFEST_FILE = "scene_manifest_transcribed.json"
OUTPUT_OFFSETS_FILE = "20_countdown_offsets.json"


def _sanitize(s: str) -> str:
    return re.sub(r"[^\w\s']", "", s.lower()).strip()


def _find_first_two_end_time(segments: List[Dict[str, Any]]) -> Optional[float]:
    """
    Returns the end time of the first occurrence of the word 'two' within a segment that
    also contains 'three' (to reduce false positives).
    """
    for seg in segments:
        text = _sanitize(seg.get("text", ""))
        if "three" in text and "two" in text:
            # Prefer precise word timing for 'two'
            for wd in seg.get("words", []):
                if _sanitize(wd.get("word", "")) == "two":
                    try:
                        return float(wd.get("end", 0.0))
                    except Exception:
                        pass
            # Fallback to segment end if word timings are unavailable
            try:
                return float(seg.get("end", 0.0))
            except Exception:
                return None
    return None


def _find_first_word_after(segments: List[Dict[str, Any]], t_after: float) -> Optional[float]:
    """
    Returns the start time of the very first word that starts STRICTLY after t_after.
    Scans all segments and their word lists in order; falls back to next segment start
    if no word timing is present.
    """
    # First pass: precise word-level search
    for seg in segments:
        words = seg.get("words") or []
        for wd in words:
            try:
                ws = float(wd.get("start", 0.0))
            except Exception:
                continue
            if ws > t_after:
                return ws

    # Second pass fallback: next segment boundary at/after t_after
    for seg in segments:
        try:
            ss = float(seg.get("start", 0.0))
        except Exception:
            continue
        if ss >= t_after:
            # If words exist, prefer the first word start in this segment
            words = seg.get("words") or []
            if words:
                try:
                    return float(words[0].get("start", ss))
                except Exception:
                    return ss
            return ss

    return None


def main():
    parser = argparse.ArgumentParser(description="Detect head countdown chatter and propose per-Take start offsets.")
    add_base_arg(parser)
    parser.add_argument(
        "--transcripts",
        type=Path,
        default=None,
        help=f"Path to {TRANSCRIBED_MANIFEST_FILE}"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Path to write {OUTPUT_OFFSETS_FILE}"
    )
    parser.add_argument(
        "--nudge",
        type=float,
        default=-0.01,
        help="Signed seconds to add to proposed_start_seconds (default: -0.01)."
    )
    args = parser.parse_args()

    p = from_args(args)
    transcripts_path = args.transcripts or p.scene_manifest
    out_path = args.out or p.countdown_offsets
    nudge = float(args.nudge)

    print("--- Starting Chatter Detector (Step 20) ---")
    print(f"  base:        {p.base}")
    print(f"  transcripts: {transcripts_path}")
    print(f"  out:         {out_path}")
    print(f"  nudge (s):   {nudge:+.3f}")

    if not transcripts_path.exists():
        print(f"[!] FATAL ERROR: Input file '{transcripts_path}' not found. Run Step 03 first.")
        return

    try:
        transcripts_data = json.loads(transcripts_path.read_text(encoding='utf-8'))
    except Exception as e:
        print(f"[!] FATAL ERROR: Failed to parse transcripts: {e}")
        return

    countdown_offsets: List[Dict[str, Any]] = []

    for scene in transcripts_data:
        braw_filename = scene.get("braw_filename")
        # Only analyze primary camera to avoid double-processing multicam sources
        if not braw_filename or "CAM 1" not in braw_filename:
            continue

        print(f"-> Analyzing: {braw_filename}")
        timed_transcript = scene.get("timed_transcript", [])

        two_end_time = _find_first_two_end_time(timed_transcript)
        if two_end_time is None:
            print("    -> No countdown found. Skipping.")
            continue

        first_content_time = _find_first_word_after(timed_transcript, two_end_time)
        if first_content_time is None:
            print("    -> No content found after countdown. Skipping.")
            continue

        # Proposed start = midpoint between two_end and first_content, then apply global nudge
        midpoint_start_time = two_end_time + ((first_content_time - two_end_time) / 2.0)
        proposed = midpoint_start_time + nudge

        # Extract the 2-digit take number from filename
        m = re.search(r"\b(\d{2})\b", braw_filename)
        if not m:
            print("    -> Could not parse take number. Skipping.")
            continue
        take_num = m.group(1)

        offset_data = {
            "braw_filename": braw_filename,
            "take_number": take_num,
            "two_end_seconds": round(float(two_end_time), 3),
            "first_content_seconds": round(float(first_content_time), 3),
            "proposed_start_seconds": round(float(proposed), 3),
            "method": "midpoint_after_countdown_wordlevel",
            "nudge_seconds": round(nudge, 3),
        }
        countdown_offsets.append(offset_data)
        print(
            "    -> two_end: {:.3f}s, first_word_after: {:.3f}s, midpoint: {:.3f}s, proposed(+nudge): {:.3f}s".format(
                two_end_time, first_content_time, midpoint_start_time, proposed
            )
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(countdown_offsets, indent=2), encoding='utf-8')

    print(f"\n[+] Success! Countdown offsets saved to '{out_path}'")


if __name__ == "__main__":
    main()
