#!/usr/bin/env python3
"""
patch_002_win_ascii_and_align.py

- Converts fancy Unicode punctuation in 002_setup_project.py to plain ASCII
  to avoid Windows cp1252 console errors.
- Replaces align_frames() with a tolerant version. If not found, appends it
  at the end of the file so calls resolve to the robust implementation.

Usage:
  python patch_002_win_ascii_and_align.py --path "E:\\ghostedit\\fcpxml\\pipeline\\002_setup_project.py"
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

ASCII_MAP = {
    "\u2192": "->",   # arrow
    "\u2013": "-",    # en dash
    "\u2014": "-",    # em dash
    "\u2015": "-",    # horizontal bar
    "\u2018": "'",    # left single quote
    "\u2019": "'",    # right single quote / apostrophe
    "\u201C": '"',    # left double quote
    "\u201D": '"',    # right double quote
    "\u2026": "...",  # ellipsis
    "\u00D7": "x",    # multiplication
    "\u2022": "*",    # bullet
    "\u00A0": " ",    # non-breaking space
}

ALIGN_FUNC_NEW = r'''
def align_frames(values, mode="auto", tolerance_frames=2):
    """
    Robust combiner for per-camera start frames.

    values: list of ints
    mode:
      - "auto" (default): if spread <= tolerance_frames, return median; else use mean.
      - "first": use the first value
      - "min": use min(values)
      - "max": use max(values)
      - "mean": round(mean(values))
      - "strict": raise if values differ

    tolerance_frames: allowed spread before falling back to non-strict choice
    """
    vals = [int(v) for v in values]
    if not vals:
        raise ValueError("align_frames(): empty values")

    lo, hi = min(vals), max(vals)
    if lo == hi:
        return lo

    spread = hi - lo

    # Best-effort logging without assuming console encoding
    try:
        log.debug(f"[align_frames] inputs={vals} spread={spread} tol={tolerance_frames} mode={mode}")
    except Exception:
        pass

    if mode in (None, "", "auto"):
        if spread <= tolerance_frames:
            sv = sorted(vals)
            m = len(sv) // 2
            if len(sv) % 2:
                return int(sv[m])
            return int(round((sv[m - 1] + sv[m]) / 2))
        choice = int(round(sum(vals) / len(vals)))
        try:
            log.warning(f"[align_frames] large spread={spread} frames; choosing mean={choice} for continuity")
        except Exception:
            pass
        return choice

    if mode == "first":
        return vals[0]
    if mode == "min":
        return lo
    if mode == "max":
        return hi
    if mode == "mean":
        return int(round(sum(vals) / len(vals)))
    if mode == "strict":
        raise ValueError(f"Start mismatch (strict): {vals}")

    # Fallback to auto-like median
    sv = sorted(vals)
    m = len(sv) // 2
    if len(sv) % 2:
        return int(sv[m])
    return int(round((sv[m - 1] + sv[m]) / 2))
'''

def replace_unicode_with_ascii(text: str) -> str:
    for u, a in ASCII_MAP.items():
        text = text.replace(u, a)
    return text

def replace_or_append_align(text: str):
    """
    Replace align_frames() if found (handles decorators, multi-line signatures,
    and optional return annotation). Otherwise, append robust version.
    """
    # Use flags parameter (no inline (?ms) inside the pattern)
    pattern = re.compile(
        r'^\s*(?:@.*\n)*'                      # optional decorators
        r'def\s+align_frames\s*'               # function name
        r'\('
        r'[\s\S]*?'                             # signature (non-greedy, spans lines)
        r'\)\s*'
        r'(?:->\s*[^:]+)?\s*'                  # optional return annotation
        r':\s*'
        r'[\s\S]*?'                             # body (non-greedy)
        r'(?=^\s*(?:def|class)\b|^\s*if\s+__name__\s*==\s*["\']__main__["\']\s*:|\Z)',
        re.MULTILINE | re.DOTALL
    )

    if pattern.search(text):
        new_text = pattern.sub(ALIGN_FUNC_NEW.strip() + "\n\n", text, count=1)
        return new_text, "replaced"
    else:
        # Append at end so our definition wins
        sep = "" if text.endswith("\n") else "\n"
        new_text = text + sep + "\n" + ALIGN_FUNC_NEW.strip() + "\n"
        return new_text, "appended"

def main():
    ap = argparse.ArgumentParser(description="Patch 002_setup_project.py for Windows ASCII logging and robust align_frames.")
    ap.add_argument("--path", type=Path, required=True, help="Path to 002_setup_project.py")
    args = ap.parse_args()

    target: Path = args.path
    if not target.exists():
        print("ERROR: file not found:", target)
        sys.exit(1)

    # Backup
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = target.with_suffix(".py.bak_" + ts)
    shutil.copy2(target, backup)

    # Read with a forgiving decoder, then write back as UTF-8
    try:
        original = target.read_text(encoding="utf-8", errors="strict")
    except Exception:
        original = target.read_text(encoding="cp1252", errors="replace")

    # ASCII-ize punctuation that breaks Windows consoles
    patched = replace_unicode_with_ascii(original)

    # Replace or append align_frames()
    patched, action = replace_or_append_align(patched)

    target.write_text(patched, encoding="utf-8")

    print("OK: Patched 002_setup_project.py")
    print(f"- Backup: {backup}")
    print("- Replaced Unicode punctuation with ASCII")
    if action == "replaced":
        print("- Replaced existing align_frames() with tolerant version")
    else:
        print("- align_frames() not found; appended tolerant version at end")

if __name__ == "__main__":
    main()
