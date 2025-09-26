#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 59_generate_broll_placeholders.py
# Version: 2.2.0 (Automatic Text Wrapping)
# Last Modified: 2025-09-16
#
# Purpose:
#   Reads B-roll plans and generates professional placeholder video clips using FFmpeg.
#   This version renders the prompt and reasoning as separate, formatted text blocks
#   and now includes automatic text wrapping to prevent overflow.

import argparse
import json
import os
import subprocess
import tempfile
import textwrap # <-- NEW: Import textwrap module
from pathlib import Path
from typing import Tuple

from project_paths import add_base_arg, from_args

# -------------------------------
# Helpers
# -------------------------------
def strip_ext_lower_basename(name: str) -> str:
    return Path(str(name)).stem.lower()

def ff_escape_for_filter(path_str: str) -> str:
    """Escapes a Windows path for use inside an FFmpeg filter string."""
    return path_str.replace('\\', '/').replace(':', '\\:')

def parse_size(size_str: str) -> Tuple[int, int]:
    """Parses a 'WxH' string into a (width, height) tuple."""
    try:
        w, h = map(int, size_str.lower().split('x'))
        return w, h
    except ValueError:
        raise ValueError("Resolution must be in 'WIDTHxHEIGHT' format (e.g., 1920x1080)")

# -------------------------------
# FFmpeg Filter Construction
# -------------------------------
def make_drawtext_filters(font_path: str, sug_file: Path, rsn_file: Path, args: argparse.Namespace) -> str:
    font_arg = ff_escape_for_filter(font_path)
    sug_path_arg = ff_escape_for_filter(sug_file.as_posix())
    rsn_path_arg = ff_escape_for_filter(rsn_file.as_posix())
    
    width, height = parse_size(args.resolution)
    margin_x = int(width * args.margin_x)
    x_expr = "(w-text_w)/2" if args.center_text else f"{margin_x}"
    
    sug_y = f"{int(height * args.sug_top_percent)}"
    rsn_y = f"{int(height * args.rsn_top_percent)}"

    sug = (f"drawtext=fontfile='{font_arg}':textfile='{sug_path_arg}':x={x_expr}:y={sug_y}:fontsize={args.fontsize_suggestion}:"
           f"fontcolor={args.font_color}:line_spacing={args.line_spacing}:box=1:boxcolor={args.box_color}:boxborderw={args.box_borderw}")
    rsn = (f"drawtext=fontfile='{font_arg}':textfile='{rsn_path_arg}':x={x_expr}:y={rsn_y}:fontsize={args.fontsize_reasoning}:"
           f"fontcolor={args.font_color}:line_spacing={args.line_spacing}:box=1:boxcolor={args.box_color}:boxborderw={args.box_borderw}")
    
    return f"{sug},{rsn}"

# -------------------------------
# Placeholder Generation (FFmpeg)
# -------------------------------
def generate_placeholder_clip(output_path: Path, item: dict, args: argparse.Namespace):
    """Generates a video clip with formatted text using FFmpeg."""
    duration = float(item.get("duration_seconds", 5.0))
    if duration <= 0:
        print(f" [!] Skipping clip with invalid duration: {duration}s")
        return

    prompt = item.get("video_prompt", "No prompt found.")
    reasoning = item.get("reasoning", "No reasoning provided.")
    item_type = item.get("type", "b-roll")
    
    # --- NEW: Automatic Text Wrapping Logic ---
    width, _ = parse_size(args.resolution)
    # Calculate available width in pixels, considering margins
    available_width_px = width * (1.0 - (args.margin_x * 2))

    # Estimate character width as ~55% of font size (a good heuristic for proportional fonts like Arial)
    sug_char_width_px = args.fontsize_suggestion * 0.55
    rsn_char_width_px = args.fontsize_reasoning * 0.55

    # Calculate max characters per line
    sug_chars_per_line = int(available_width_px / sug_char_width_px)
    rsn_chars_per_line = int(available_width_px / rsn_char_width_px)
    
    # Wrap the text using the calculated widths
    wrapped_prompt = textwrap.fill(prompt, width=sug_chars_per_line)
    wrapped_reasoning = textwrap.fill(reasoning, width=rsn_chars_per_line)
    # --- END NEW LOGIC ---

    header = f"TYPE: {item_type.upper()} | DURATION: {duration:.2f}s"
    suggestion_text = f"{header}\n\nPROMPT:\n{wrapped_prompt}"
    reasoning_text = f"REASONING:\n{wrapped_reasoning}"

    try:
        # Use temporary files to handle multi-line text and special characters safely
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.txt', delete=False, encoding='utf-8') as sug_file, \
             tempfile.NamedTemporaryFile(mode='w+', suffix='.txt', delete=False, encoding='utf-8') as rsn_file:
            
            sug_file.write(suggestion_text)
            rsn_file.write(reasoning_text)
            sug_file_path = Path(sug_file.name)
            rsn_file_path = Path(rsn_file.name)

        # Generate the complex filter string
        filter_str = make_drawtext_filters(args.font_path, sug_file_path, rsn_file_path, args)
        
        command = [
            'ffmpeg', '-f', 'lavfi',
            '-i', f'color=c={args.bg_color}:s={args.resolution}:r=30:d={duration}',
            '-vf', filter_str,
            '-y', str(output_path)
        ]
        
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    except FileNotFoundError:
        print("\n[!] FATAL ERROR: `ffmpeg` command not found. Please install FFmpeg.")
        exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\n[!] FFmpeg failed for: {output_path.name}\n    FFmpeg stderr:\n{e.stderr}")
    finally:
        # Ensure temporary files are cleaned up
        if 'sug_file_path' in locals() and sug_file_path.exists():
            sug_file_path.unlink()
        if 'rsn_file_path' in locals() and rsn_file_path.exists():
            rsn_file_path.unlink()

# -------------------------------
# Main
# -------------------------------
def main(cfg):
    print("[+] Starting B-Roll Placeholder Generation...")

    p = from_args(cfg)
    input_dir = Path(cfg.input_dir or str(p.b_roll_dir))
    output_dir = Path(cfg.output_dir or str(p.working_root / "59_generated_b_roll"))
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[i] Reading B-roll plans from: {input_dir}")
    print(f"[i] Saving placeholder videos to: {output_dir}")
    if not Path(cfg.font_path).exists():
        print(f"[!] WARNING: Font file not found at '{cfg.font_path}'. FFmpeg may fail if it cannot find a default font.")

    plans = sorted(input_dir.glob("*_broll_plan.json"))
    if not plans:
        print(f"[!] No B-roll plans found in '{input_dir}'.")
        return

    total_generated = 0
    for plan_path in plans:
        print(f"\n[+] Processing plan: {plan_path.name}")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        base_name = strip_ext_lower_basename(plan_path.stem).replace("_broll_plan", "")
        items = plan.get("b_roll_plan", [])
        if not items:
            print("   -> No b_roll_plan entries; skipping.")
            continue

        for i, item in enumerate(items):
            timestamp = float(item.get("timestamp_start", i))
            out_name = f"{base_name}_placeholder_{timestamp:.2f}s.mp4"
            out_path = output_dir / out_name

            if out_path.exists() and not cfg.overwrite:
                print(f" -> Skipping (exists): {out_name}")
                continue

            print(f" -> Generating: {out_name} ({float(item.get('duration_seconds', 0)):.2f}s)")
            generate_placeholder_clip(out_path, item, cfg)
            total_generated += 1

    print(f"\n[+] Success. Generated {total_generated} placeholder video(s).")

# -------------------------------
# Entrypoint
# -------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate B-roll placeholder videos using FFmpeg.")
    add_base_arg(parser)
    
    # Script behavior
    parser.add_argument("--input_dir", type=str, default=None)
    parser.add_argument("--output_dir", type=str, default=None)
    parser.add_argument("--overwrite", action="store_true")
    
    # Placeholder video parameters
    parser.add_argument("--resolution", type=str, default="1920x1080")
    parser.add_argument("--bg-color", type=str, default="black")
    
    # Text and Font parameters
    parser.add_argument("--font-path", type=str, default="C:/Windows/Fonts/Arial.ttf", help="Path to the .ttf font file.")
    parser.add_argument("--font-color", type=str, default="white")
    parser.add_argument("--fontsize-suggestion", type=int, default=48)
    parser.add_argument("--fontsize-reasoning", type=int, default=32)
    parser.add_argument("--line-spacing", type=int, default=8)
    parser.add_argument("--center-text", action="store_true", help="Center text horizontally; otherwise, left-align with margin.")

    # Positioning parameters
    parser.add_argument("--margin-x", type=float, default=0.05, help="Left margin as a percentage of video width (e.g., 0.05 for 5%%).")
    parser.add_argument("--sug-top-percent", type=float, default=0.1, help="Suggestion text block top position (percentage of height).")
    parser.add_argument("--rsn-top-percent", type=float, default=0.6, help="Reasoning text block top position (percentage of height).")

    # Box parameters
    parser.add_argument("--box-color", type=str, default="black@0.5", help="Background box color for text.")
    parser.add_argument("--box-borderw", type=int, default=15, help="Border width for the text background box.")

    args = parser.parse_args()
    main(args)