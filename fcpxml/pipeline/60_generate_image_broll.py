#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 60_generate_image_broll.py
# Version: 3.1.0 (Restored High-End Features + Sequence Stitching)
# Purpose: Generates B-roll using Imagen-4 Ultra.
#          Stitches "Sequences" (Triplets/Quartets) into single seamless files.

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple, List, Dict

# --- Vertex AI Dependencies ---
try:
    import vertexai
    from vertexai.preview.vision_models import ImageGenerationModel
except ImportError:
    print("\n[!] FATAL ERROR: `google-cloud-aiplatform` library not found.")
    sys.exit(1)

from project_paths import add_base_arg, from_args

# -------------------------------
# Configuration & Logging
# -------------------------------
GAP_THRESHOLD = 0.1 # Threshold to detect abutting clips
script_dir = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s"))
    logger.addHandler(handler)

# -------------------------------
# Helpers
# -------------------------------
def parse_size(size_str: str) -> Tuple[int, int]:
    w, h = map(int, size_str.lower().split('x'))
    return w, h

def sanitize_filename(text: str, max_length: int = 60) -> str:
    sanitized = re.sub(r"[<>:\"/\\|?*\x00-\x1f']", "", text)
    sanitized = re.sub(r'\s+', '_', sanitized)
    return sanitized[:max_length].strip('_. ')

def group_sequences_by_id(items: List[Dict]) -> List[List[Dict]]:
    """
    Groups items based on the 'sequence_length' tag AND timestamps.
    Logic: 
    1. If we see 'sequence_length' > 1, we look ahead to grab that many items.
    2. Fallback: If timestamps abut (gap < 0.1s), group them.
    """
    if not items: return []
    items.sort(key=lambda x: x.get("timestamp_start", 0))
    
    groups = []
    i = 0
    while i < len(items):
        current_item = items[i]
        
        # Check explicit sequence tag
        seq_len_str = str(current_item.get("sequence_length", "1"))
        try:
            seq_len = int(seq_len_str)
        except ValueError:
            seq_len = 1
            
        current_group = [current_item]
        
        if seq_len > 1:
            # Grab the next (seq_len - 1) items if they exist
            frames_needed = seq_len - 1
            for k in range(frames_needed):
                if (i + 1 + k) < len(items):
                    current_group.append(items[i + 1 + k])
            i += seq_len # Skip the items we just grouped
        else:
            # Fallback: Check for timestamp abutment
            # If next item starts exactly when this one ends, maybe group?
            # For now, we trust the explicit schema first.
            i += 1
            
        groups.append(current_group)

    return groups

# -------------------------------
# Generation: Image (Imagen 4 Ultra)
# -------------------------------
def generate_ai_image(prompt, cache_dir, model, args) -> Optional[Path]:
    phash = hashlib.sha256(prompt.encode()).hexdigest()
    cpath = cache_dir / f"{phash}.png"
    
    if cpath.exists() and not args.overwrite_images:
        return cpath
    
    try:
        logger.info(f"   [ImgGen] Generating: {prompt[:50]}...")
        # IMAGEN-4 ULTRA PARAMETERS
        # Note: aspect_ratio must be one of ['1:1', '9:16', '16:9', '3:4', '4:3']
        imgs = model.generate_images(
            prompt=prompt,
            number_of_images=1,
            aspect_ratio=args.aspect_ratio,
            safety_filter_level="block_few",
            person_generation="allow_adult" 
        )
        if imgs:
            imgs[0].save(str(cpath))
            return cpath
    except Exception as e:
        logger.error(f"   [!] Image Gen Error: {e}")
    return None

# -------------------------------
# Generation: Video (Motion)
# -------------------------------
def build_motion_vf(width: int, height: int, duration: float, fps: float, motion_type: str) -> str:
    """
    Constructs FFmpeg filter for Pan/Zoom.
    """
    frames = int(duration * fps)
    
    # 1. Subtle "Breathing" Zoom (Standard)
    if motion_type == "zoom_in":
        z = "min(zoom+0.0010,1.2)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"
        
    # 2. Linear Pan Right (Good for Flow sequences like Blood)
    elif motion_type == "pan_right":
        z = "1.2" # Fixed zoom
        # Pan from Left (0) to Right
        x = f"(iw-iw/zoom) * (on/{frames})"
        y = "(ih-ih/zoom)/2"
        
    # 3. Slow Push (Cinematic)
    else:
        z = f"min(zoom+0.0015,1.5)"
        x = "(iw-iw/zoom)/2"
        y = "(ih-ih/zoom)/2"

    vf = (
        f"scale=-1:{height*2}," # Pre-scale for zoom quality
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},"
        f"format=yuv420p"
    )
    return vf

def render_clip(output_path: Path, img_path: Path, duration: float, args):
    width, height = parse_size(args.resolution)
    vf = build_motion_vf(width, height, duration, args.fps, args.motion_type)
    
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(img_path),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        str(output_path)
    ]
    subprocess.run(cmd, check=True)

# -------------------------------
# Stitching (Seamless Concat)
# -------------------------------
def stitch_sequence(output_path: Path, clips: List[Path]):
    """Stitches clips seamlessly (No crossfade, perfect abutment)."""
    if len(clips) == 1:
        import shutil
        shutil.copy(clips[0], output_path)
        return

    # Use concat demuxer for speed and seamless joining
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        for clip in clips:
            f.write(f"file '{clip.resolve().as_posix()}'\n")
        list_file = f.name
    
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c", "copy", str(output_path)
        ]
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"   [!] Stitching Failed: {e}")
    finally:
        Path(list_file).unlink()

# -------------------------------
# Main Logic
# -------------------------------
def main(cfg):
    p = from_args(cfg)
    
    # 1. Initialize Vertex AI (Imagen 4 Ultra)
    try:
        vertexai.init(project="qtow-148623", location="us-central1")
        # Explicitly asking for Ultra
        model = ImageGenerationModel.from_pretrained("imagen-4.0-ultra-generate-001")
        logger.info("[+] Model Loaded: imagen-4.0-ultra-generate-001")
    except Exception as e:
        logger.error(f"[!] Vertex AI Init Failed: {e}")
        return

    # 2. Setup Paths
    input_dir = Path(cfg.input_dir or str(p.b_roll_dir))
    output_dir = Path(cfg.output_dir or str(p.working_root / "60_generated_b_roll"))
    cache_dir = Path(cfg.image_cache_dir or str(p.working_root / "60_image_cache"))
    
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Temp dir for video fragments (hidden)
    temp_dir = output_dir / ".temp_fragments"
    temp_dir.mkdir(exist_ok=True)

    # 3. Find Plans
    plans = sorted(input_dir.glob("*_broll_plan.json"))
    if not plans:
        logger.warning(f"No plans found in {input_dir}")
        return

    for plan_path in plans:
        logger.info(f"Processing Plan: {plan_path.name}")
        try:
            plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.error("   [!] Invalid JSON.")
            continue
            
        # Support both key styles (just in case)
        items = plan_data.get("b_roll_suggestions", plan_data.get("b_roll_plan", []))
        
        # 4. Group Items (Triplets/Quartets)
        groups = group_sequences_by_id(items)
        logger.info(f"   -> Found {len(groups)} distinct visual events (Singles or Sequences).")

        for grp_idx, group in enumerate(groups):
            group_clips = []
            
            # 5. Render Fragments
            for i, item in enumerate(group):
                prompt = item.get("video_prompt")
                dur = float(item.get("duration_seconds", 4.0))
                
                if not prompt: continue

                # A. Generate Image
                img_path = generate_ai_image(prompt, cache_dir, model, cfg)
                if not img_path: 
                    logger.warning("   [!] Image generation failed, skipping clip.")
                    continue
                
                # B. Render Video Fragment
                # Naming: frag_{group_index}_{item_index}.mp4
                frag_name = f"frag_{grp_idx}_{i}.mp4"
                frag_path = temp_dir / frag_name
                
                if not frag_path.exists() or cfg.overwrite_video:
                    try:
                        render_clip(frag_path, img_path, dur, cfg)
                        group_clips.append(frag_path)
                    except Exception as e:
                        logger.error(f"   [!] Render fragment failed: {e}")
                else:
                    group_clips.append(frag_path)

            # 6. Stitch Sequence
            if group_clips:
                start_ts = float(group[0].get("timestamp_start", 0))
                base_name = plan_path.stem.replace("_broll_plan", "")
                
                # Naming: "subtitle_1_3787.17_blood_cells.mp4"
                raw_prompt = group[0].get("video_prompt", "seq")
                safe_prompt = sanitize_filename(raw_prompt)[:20]
                
                final_name = f"{base_name}_{start_ts:.2f}s_{safe_prompt}.mp4"
                final_path = output_dir / final_name
                
                # Skip if exists
                if final_path.exists() and not cfg.overwrite_video:
                    logger.info(f"   -> Skipping existing: {final_name}")
                    continue

                if len(group_clips) > 1:
                    logger.info(f"   -> Stitching Sequence ({len(group_clips)} clips) -> {final_name}")
                else:
                    logger.info(f"   -> Saving Single -> {final_name}")
                    
                stitch_sequence(final_path, group_clips)

    logger.info("[+] Batch Complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_base_arg(parser)
    parser.add_argument("--input_dir", type=str)
    parser.add_argument("--output_dir", type=str)
    parser.add_argument("--image-cache-dir", type=str)
    
    # Knobs
    parser.add_argument("--overwrite-images", action="store_true", help="Re-generate images from Vertex AI")
    parser.add_argument("--overwrite-video", action="store_true", help="Re-render video clips (uses cached images)")
    
    parser.add_argument("--resolution", default="1920x1080")
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--aspect-ratio", type=str, default="16:9")
    parser.add_argument("--motion-type", type=str, default="zoom_in", choices=["zoom_in", "pan_right", "static"])
    
    args = parser.parse_args()
    main(args)