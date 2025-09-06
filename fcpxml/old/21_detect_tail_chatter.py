#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: 20_detect_tailcut.py
Version: 1.0.0
Last Modified: 2025-08-16
Purpose: Step 20 (tail) — Detect end-of-take chatter or extended end silence and propose per-Take END offsets.
         Output schema mirrors 20_detect_chatter.py for easy downstream reuse (same general fields; end-specific keys).
Reference: 20_detect_chatter.py  :contentReference[oaicite:0]{index=0}

Usage:
  python 20_detect_tailcut.py \
      --transcripts /path/scene_manifest_transcribed.json \
      --out /path/20_tailcut_offsets.json \
      [--min-silence 1.2] [--nudge -0.02] [--lexicon /path/tail_lexicon.json]

Notes:
- Relies only on transcripts (no raw audio). Uses two detectors:
  (A) Tail-chatter regex: flags short, non-content phrases (e.g., “cut”, “stop recording”, “good?”).
  (B) End-silence: flags unusually long pause before last speech segment.
- Proposed end is a midpoint between last content and tail indicator (then a small negative nudge),
  or the last content word end (with small negative nudge) if no indicator is found.
"""

import json
import re
import os
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from project_paths import add_base_arg, from_args

TRANSCRIBED_MANIFEST_FILE = "scene_manifest_transcribed.json"
OUTPUT_TAILCUT_FILE = "21_tailcut_offsets.json"

# ----------------------------
# Sanitization and Lexicon
# ----------------------------

def _sanitize(s: str) -> str:
    return re.sub(r"[^\w\s']", "", (s or "").lower()).strip()

# Default tail-phrase snippets. Keep concise to reduce false positives.
DEFAULT_TAIL_LEXICON = [
    # hard stops / production
    "cut", "and cut", "we can cut", "stop recording", "stop it", "kill it", "reset", "that's a wrap",
    # check/confirm
    "good", "good?", "we good", "we good?", "got it", "got it?", "did we get it", "did we get it?",
    "how was that", "how was that?",
    # meta-filming
    "rolling", "camera", "mic check", "levels", "check audio",
    # go again
    "again", "let's go again", "go again", "one more", "last one",
    # filler often used trailing
    "ok", "okay", "cool", "nice"
]

def _load_lexicon(path: Optional[Path]) -> List[str]:
    if not path:
        return DEFAULT_TAIL_LEXICON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x).lower().strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return DEFAULT_TAIL_LEXICON

# ----------------------------
# Core Helpers
# ----------------------------

def _segments_of(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    return scene.get("timed_transcript", []) or []

def _word_times(seg: Dict[str, Any]) -> List[Tuple[float, float, str]]:
    out = []
    for wd in seg.get("words") or []:
        try:
            ws = float(wd.get("start", 0.0))
            we = float(wd.get("end", ws))
            tx = str(wd.get("word", "")).strip()
            out.append((ws, we, tx))
        except Exception:
            continue
    return out

def _seg_bounds(seg: Dict[str, Any]) -> Tuple[float, float]:
    try:
        ss = float(seg.get("start", 0.0))
    except Exception:
        ss = 0.0
    try:
        se = float(seg.get("end", ss))
    except Exception:
        se = ss
    return ss, se

def _is_tail_chatter(text: str, lexicon: List[str]) -> bool:
    t = _sanitize(text)
    if not t:
        return False
    # Short, non-content utterances are more likely tail chatter
    wc = len(t.split())
    shortish = wc <= 4
    # Exact or substring match for simple lexicon entries
    if shortish:
        if t in lexicon:
            return True
        for k in lexicon:
            if k in t:
                return True
    # Single interjection at end (e.g., "okay", "cool") often tail-ish
    return shortish and t in {"ok", "okay", "cool", "nice", "good", "good?"}

def _last_content_boundary(segments: List[Dict[str, Any]], min_silence: float, lexicon: List[str]) -> Tuple[Optional[float], Optional[float], str]:
    """
    Returns (last_content_end, tail_indicator_start, method)
      - If tail-chatter is found near the end, returns boundary before it (method="tail_regex_midpoint").
      - Else if a long end pause precedes the last segment, boundary is before that last seg (method="tail_silence_gap").
      - Else boundary is the end of the final word (method="last_word").
    """
    if not segments:
        return None, None, "no_segments"

    # Build compact list with (seg_start, seg_end, text, first_word_start)
    comp: List[Tuple[float, float, str, float]] = []
    for seg in segments:
        ss, se = _seg_bounds(seg)
        text = (seg.get("text") or "").strip()
        words = _word_times(seg)
        first_ws = words[0][0] if words else ss
        comp.append((ss, se, text, first_ws))

    # 1) Tail-chatter regex: scan from end backward until a content-looking segment
    tail_idx = None
    for idx in range(len(comp) - 1, -1, -1):
        ss, se, text, first_ws = comp[idx]
        if _is_tail_chatter(text, lexicon):
            tail_idx = idx
            # keep walking back to skip a cluster of tail-chatter
            continue
        # as soon as we hit a contentful line after skipping tail, stop
        if tail_idx is not None:
            # Boundary: content end right before the first tail-chatter segment
            prev_ss, prev_se, _, _ = comp[max(idx, 0)]
            tail_start = comp[tail_idx][0]  # start of the first tail segment
            return prev_se, tail_start, "tail_regex_midpoint"

    # 2) End-silence: look for unusually long gap before the final segment
    if len(comp) >= 2:
        last_ss, last_se, _, _ = comp[-1]
        prev_ss, prev_se, _, _ = comp[-2]
        gap = max(0.0, last_ss - prev_se)
        if gap >= min_silence:
            return prev_se, last_ss, "tail_silence_gap"

    # 3) No indicator: use final word end
    last_end = comp[-1][1]
    return last_end, None, "last_word"

def _proposed_end(last_content_end: float, tail_indicator_start: Optional[float], nudge: float) -> float:
    if tail_indicator_start is not None and tail_indicator_start > last_content_end:
        midpoint = last_content_end + (tail_indicator_start - last_content_end) / 2.0
        return round(midpoint + nudge, 3)
    # If no indicator, be conservative: small negative nudge only
    return round(last_content_end + min(nudge, 0.0), 3)

# ----------------------------
# Main
# ----------------------------

def main():
    parser = argparse.ArgumentParser(description="Detect tail chatter or long end-silence and propose per-Take end offsets.")
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
        help=f"Path to write {OUTPUT_TAILCUT_FILE}"
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=1.2,
        help="Seconds; gap threshold to treat as end-silence prior to last segment (default: 1.2s)."
    )
    parser.add_argument(
        "--nudge",
        type=float,
        default=-0.02,
        help="Signed seconds applied to proposed_end_seconds (default: -0.02; negative trims earlier)."
    )
    parser.add_argument(
        "--lexicon",
        type=Path,
        default=None,
        help="Optional JSON list of tail phrases to augment/override defaults."
    )
    args = parser.parse_args()

    p = from_args(args)
    transcripts_path = args.transcripts or p.scene_manifest
    out_path = args.out or (p.base / OUTPUT_TAILCUT_FILE)
    min_silence = float(args.min_silence)
    nudge = float(args.nudge)
    lexicon = _load_lexicon(args.lexicon)

    print("--- Starting Tailcut Detector (Step 20 - END) ---")
    print(f"  base:           {p.base}")
    print(f"  transcripts:    {transcripts_path}")
    print(f"  out:            {out_path}")
    print(f"  min_silence:    {min_silence:.2f}s")
    print(f"  nudge (s):      {nudge:+.3f}")
    print(f"  lexicon size:   {len(lexicon)}")

    if not transcripts_path.exists():
        print(f"[!] FATAL ERROR: Input file '{transcripts_path}' not found. Run Step 03 first.")
        return

    try:
        transcripts_data = json.loads(transcripts_path.read_text(encoding='utf-8'))
    except Exception as e:
        print(f"[!] FATAL ERROR: Failed to parse transcripts: {e}")
        return

    tail_offsets: List[Dict[str, Any]] = []

    for scene in transcripts_data:
        braw_filename = scene.get("braw_filename")
        # Only analyze primary camera to avoid double-processing multicam sources
        if not braw_filename or "CAM 1" not in braw_filename:
            continue

        print(f"-> Analyzing: {braw_filename}")
        segments = _segments_of(scene)

        last_content_end, tail_indicator_start, method = _last_content_boundary(segments, min_silence, lexicon)
        if last_content_end is None:
            print("    -> No transcript segments. Skipping.")
            continue

        proposed = _proposed_end(last_content_end, tail_indicator_start, nudge)

        # Extract the 2-digit take number from filename
        m = re.search(r"\b(\d{2})\b", braw_filename or "")
        if not m:
            print("    -> Could not parse take number. Skipping.")
            continue
        take_num = m.group(1)

        entry = {
            "braw_filename": braw_filename,
            "take_number": take_num,
            "last_content_seconds": round(float(last_content_end), 3),
            "tail_indicator_seconds": round(float(tail_indicator_start), 3) if tail_indicator_start is not None else None,
            "proposed_end_seconds": float(proposed),
            "method": method,
            "nudge_seconds": round(nudge, 3),
        }
        tail_offsets.append(entry)

        if tail_indicator_start is not None:
            print(
                "    -> last_content: {:.3f}s, indicator: {:.3f}s, midpoint->proposed(+nudge): {:.3f}s".format(
                    last_content_end, tail_indicator_start, proposed
                )
            )
        else:
            print(
                "    -> last_content: {:.3f}s, no indicator; proposed(+nudge): {:.3f}s".format(
                    last_content_end, proposed
                )
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(tail_offsets, indent=2), encoding='utf-8')

    print(f"\n[+] Success! Tailcut offsets saved to '{out_path}'")


if __name__ == "__main__":
    main()

# ----------------------------
# Changelog / Developer Notes
# ----------------------------
# 1.0.0 (2025-08-16)
# - Initial implementation to mirror Step 20 start-detector semantics for end-of-take:
#   * Tail-chatter regex detector.
#   * End-silence gap detector.
#   * Conservative fallback to final word end with small negative nudge.
#   * Output includes: last_content_seconds, tail_indicator_seconds (optional), proposed_end_seconds.
# - Filename and output defaults chosen to play nicely with existing tooling; adjust as needed.
