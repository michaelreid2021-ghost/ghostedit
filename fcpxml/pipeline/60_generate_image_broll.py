#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 60_generate_image_broll.py
# Version: 1.2.0
# Last Modified: 2025-09-16
#
# Purpose:
#   Reads B-roll plans, generates a placeholder image for each prompt using
#   the Vertex AI SDK, and then converts that image into a video clip with FFmpeg.
#   Includes image caching and uses the prompt to create descriptive filenames.

import argparse
import hashlib
import json
import os
import re  # <-- NEW: Import regular expressions
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

# --- Vertex AI Dependencies ---
try:
    import vertexai
    from vertexai.preview.vision_models import ImageGenerationModel
except ImportError:
    print("\n[!] FATAL ERROR: `google-cloud-aiplatform` library not found.")
    print("    Please install it by running: pip install google-cloud-aiplatform")
    sys.exit(1)

from project_paths import add_base_arg, from_args

# -------------------------------
# Helpers
# -------------------------------
def strip_ext_lower_basename(name: str) -> str:
    return Path(str(name)).stem.lower()

def parse_size(size_str: str) -> Tuple[int, int]:
    """Parses a 'WxH' string into a (width, height) tuple."""
    try:
        w, h = map(int, size_str.lower().split('x'))
        return w, h
    except ValueError:
        raise ValueError("Resolution must be in 'WIDTHxHEIGHT' format (e.g., 1920x1080)")

def sanitize_filename(text: str, max_length: int = 100) -> str:
    """Sanitizes a string to be a safe filename component."""
    # Remove characters that are invalid in Windows/Linux filenames
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text)
    # Replace whitespace sequences with a single underscore
    sanitized = re.sub(r'\s+', '_', sanitized)
    # Remove any resulting multiple underscores
    sanitized = re.sub(r'__+', '_', sanitized)
    # Truncate to the max length
    if len(sanitized) > max_length:
        # Cut at the last full word (underscore) to avoid splitting words
        sanitized = sanitized[:max_length].rsplit('_', 1)[0]
    return sanitized.strip('_')

# -------------------------------
# AI Image Generation
# -------------------------------
def generate_ai_image(
    prompt: str,
    cache_dir: Path,
    model: ImageGenerationModel,
    args: argparse.Namespace
) -> Optional[Path]:
    """
    Generates an image using the Vertex AI SDK.
    Caches the result to avoid redundant API calls.
    """
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    cached_image_path = cache_dir / f"{prompt_hash}.jpg"

    if cached_image_path.exists():
        print("   -> Found in cache.")
        return cached_image_path

    print("   -> Generating via Vertex AI SDK...")
    try:
        images = model.generate_images(
            prompt=prompt,
            number_of_images=1,
            aspect_ratio=args.aspect_ratio
        )
        if not images:
            print("\n[!] Image generation failed: The API returned no images, likely due to safety filters on the prompt.")
            return None
        images[0].save(str(cached_image_path))
        print(f"   -> Saved to cache: {cached_image_path.name}")
        return cached_image_path
    except Exception as e:
        print(f"\n[!] An unexpected error occurred during image generation: {e}")
        print("    HINT: Ensure you are authenticated via 'gcloud auth application-default login'.")
        return None

# -------------------------------
# Placeholder Generation (FFmpeg)
# -------------------------------
def generate_placeholder_clip(
    output_path: Path,
    item: dict,
    image_cache_dir: Path,
    model: ImageGenerationModel,
    args: argparse.Namespace
):
    """Generates a video clip from a source image using FFmpeg."""
    duration = float(item.get("duration_seconds", 5.0))
    prompt = item.get("video_prompt")

    if duration <= 0:
        print(f" [!] Skipping clip with invalid duration: {duration}s")
        return
    if not prompt:
        print(f" [!] Skipping clip with no video_prompt.")
        return

    image_path = generate_ai_image(prompt, image_cache_dir, model, args)
    if not image_path:
        print(f" [!] Failed to get image for prompt, skipping video generation.")
        return

    try:
        command = [
            'ffmpeg', '-loop', '1', '-i', str(image_path),
            '-c:v', 'libx264', '-t', str(duration), '-pix_fmt', 'yuv420p',
            '-s', args.resolution, '-y', str(output_path)
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("\n[!] FATAL ERROR: `ffmpeg` command not found. Please install FFmpeg and ensure it's in your system's PATH.")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\n[!] FFmpeg failed for: {output_path.name}\n    FFmpeg stderr:\n{e.stderr}")

# -------------------------------
# Main
# -------------------------------
def main(cfg):
    print("[+] Starting B-Roll Image Placeholder Generation...")
    p = from_args(cfg)

    try:
        vertexai.init(project="qtow-148623", location="us-central1")
        model = ImageGenerationModel.from_pretrained("imagen-3.0-fast-generate-001")
        print("[i] Vertex AI initialized successfully.")
    except Exception as e:
        print(f"\n[!] FATAL: Could not initialize Vertex AI: {e}")
        print("    Please ensure you have authenticated with Google Cloud SDK:")
        print("    gcloud auth application-default login")
        sys.exit(1)

    input_dir = Path(cfg.input_dir or str(p.b_roll_dir))
    output_dir = Path(cfg.output_dir or str(p.working_root / "60_generated_b_roll"))
    image_cache_dir = Path(cfg.image_cache_dir or str(p.working_root / "60_image_cache"))
    output_dir.mkdir(parents=True, exist_ok=True)
    image_cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"[i] Reading B-roll plans from: {input_dir}")
    print(f"[i] Saving placeholder videos to: {output_dir}")
    print(f"[i] Caching generated images in: {image_cache_dir}")

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
            prompt_text = item.get("video_prompt", f"no_prompt_{i}")

            # --- MODIFIED: Create descriptive filename from prompt ---
            safe_prompt_name = sanitize_filename(prompt_text)
            out_name = f"{base_name}_{safe_prompt_name}_{timestamp:.2f}s.mp4"
            out_path = output_dir / out_name
            # --- END MODIFICATION ---

            if out_path.exists() and not cfg.overwrite:
                print(f" -> Skipping (exists): {out_name}")
                continue

            print(f" -> Generating: {out_name} ({float(item.get('duration_seconds', 0)):.2f}s)")
            generate_placeholder_clip(out_path, item, image_cache_dir, model, cfg)
            total_generated += 1

    print(f"\n[+] Success. Generated {total_generated} placeholder video(s).")

# -------------------------------
# Entrypoint
# -------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate AI image-based B-roll placeholder videos using Vertex AI.")
    add_base_arg(parser)
    parser.add_argument("--input_dir", type=str, default=None, help="Directory containing *_broll_plan.json files.")
    parser.add_argument("--output_dir", type=str, default=None, help="Directory to save the generated .mp4 files.")
    parser.add_argument("--image-cache-dir", type=str, default=None, help="Directory to store and retrieve generated images.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing video files.")
    parser.add_argument("--resolution", type=str, default="1920x1080", help="Output resolution of the video (e.g., 1920x1080).")
    parser.add_argument("--aspect-ratio", type=str, default="16:9", choices=["16:9", "4:3", "1:1", "9:16", "3:4"], help="Aspect ratio for the generated image.")
    args = parser.parse_args()
    main(args)