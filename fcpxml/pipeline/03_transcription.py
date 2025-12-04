#!/usr/bin/env python3
# 03_whisper_transcriber.py
# Updated to support both .mp4 and .mov proxy files.
# Updated to reassemble fragmented segments into full sentences.

import os
import json
import subprocess
import argparse
from pathlib import Path
import whisper
import torch
import re

from project_paths import add_base_arg, from_args

WHISPER_MODEL = "large-v3"

def extract_audio_segment(ffmpeg_path: str, video_path: str, output_wav_path: str):
    """Extracts audio from the entire video file."""
    command = [
        ffmpeg_path,
        '-y', # Overwrite output file if it exists
        '-i', video_path,
        '-vn', # No video
        '-acodec', 'pcm_s16le', # Standard WAV format
        '-ar', '16000', # Whisper's preferred sample rate
        '-ac', '1', # Mono audio
        output_wav_path
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def reassemble_transcript(raw_segments):
    """
    Merges small Whisper segments into larger blocks based on sentence endings.
    Terminators: . ? !
    """
    merged = []
    current_text_buffer = []
    current_start = None
    
    # Regex to check if string ends with sentence punctuation (ignoring trailing spaces/quotes)
    # We look for . ? ! followed optionally by quote or space
    eos_pattern = re.compile(r'[.?!][\"\']?\s*$')

    for seg in raw_segments:
        text = seg.get('text', '')
        
        if current_start is None:
            current_start = seg.get('start', 0.0)

        current_text_buffer.append(text)
        
        # Join temporarily to check the ending of the current accumulated phrase
        # Whisper usually provides leading spaces on segments, so we just join strictly
        combined_text = "".join(current_text_buffer)

        if eos_pattern.search(combined_text):
            # Found a sentence end
            merged.append({
                "start": current_start,
                "end": seg.get('end', 0.0),
                "text": combined_text.strip()
            })
            # Reset buffer
            current_text_buffer = []
            current_start = None

    # Handle any leftovers in the buffer (e.g. sentence didn't end with punctuation)
    if current_text_buffer:
        merged.append({
            "start": current_start,
            "end": raw_segments[-1].get('end', 0.0),
            "text": "".join(current_text_buffer).strip()
        })

    return merged

def main():
    parser = argparse.ArgumentParser(description="Transcribe proxy videos using Whisper and write scene manifest.")
    add_base_arg(parser)
    parser.add_argument("--proxies", type=Path, default=None, help="Directory containing proxy .mp4 or .mov files.")
    parser.add_argument("--ffmpeg", type=str, default="ffmpeg", help="Path to ffmpeg executable (default: ffmpeg on PATH).")
    parser.add_argument("--whisper_model", type=str, default=WHISPER_MODEL, help="Whisper model size (e.g., base, small, medium).")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to master_manifest.json")
    parser.add_argument("--out", type=Path, default=None, help="Path to scene_manifest_transcribed.json")
    args = parser.parse_args()

    p = from_args(args)
    proxies_dir = args.proxies or p.proxies
    manifest_path = args.manifest or p.master_manifest
    out_path = args.out or p.scene_manifest
    temp_audio_dir = p.temp_audio
    ffmpeg_path = args.ffmpeg
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[+] Using device: {device}")
    print("[+] Transcription configuration:")
    print(f"  base: {p.base}")
    print(f"  proxies: {proxies_dir}")
    print(f"  manifest: {manifest_path}")
    print(f"  out: {out_path}")
    print(f"  ffmpeg: {ffmpeg_path}")
    print(f"  whisper_model: {args.whisper_model}")

    if not Path(ffmpeg_path).exists() and ffmpeg_path.lower() == "ffmpeg":
        # allow PATH resolution; we won't hard fail here
        pass
    elif not Path(ffmpeg_path).exists():
        print(f"[!] ffmpeg not found at: {ffmpeg_path}")
        return

    if not manifest_path.exists():
        print(f"[!] master manifest not found: {manifest_path}")
        return

    print("[+] Loading Whisper...")
    model = whisper.load_model(args.whisper_model, device=device)

    scenes = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"[+] {len(scenes)} scenes found.")

    temp_audio_dir.mkdir(parents=True, exist_ok=True)

    transcribed_scenes = []
    for i, scene in enumerate(scenes):
        # --- MODIFICATION START ---
        # Look for either .mp4 or .mov, prioritizing .mp4 if both exist.
        
        proxy_basename = Path(scene['proxy_filename']).stem
        mp4_path = proxies_dir / f"{proxy_basename}.mp4"
        mov_path = proxies_dir / f"{proxy_basename}.mov"
        braw_path = proxies_dir / f"{proxy_basename}.braw"

        video_path = None
        found_format = None

        if mp4_path.exists():
            video_path = mp4_path
            found_format = ".mp4"
        elif mov_path.exists():
            video_path = mov_path
            found_format = ".mov"
        elif braw_path.exists(): # Fixed bug: was checking mov_path again
            video_path = braw_path
            found_format = ".braw"

        print(f"({i+1}/{len(scenes)}) Processing {proxy_basename}...")

        if not video_path:
            print(f"     [!] Proxy video not found for '{proxy_basename}' (checked for .mp4 and .mov). Skipping.")
            # Add a placeholder scene with an error and continue
            error_scene_data = scene.copy()
            error_scene_data['timed_transcript'] = []
            error_scene_data['transcript'] = f"ERROR: Proxy file not found for {proxy_basename}"
            transcribed_scenes.append(error_scene_data)
            continue
        
        print(f"     -> Found proxy file: {video_path.name}")
        # --- MODIFICATION END ---

        audio_path = temp_audio_dir / f"scene_{scene['scene_number']}_audio.wav"

        try:
            extract_audio_segment(ffmpeg_path, str(video_path), str(audio_path))
            fp16 = (device == "cuda")
            result = model.transcribe(str(audio_path), fp16=fp16, word_timestamps=True)

            raw_segments = result.get('segments', [])
            
            # --- NEW: Reassemble segments into sentences ---
            reassembled_segments = reassemble_transcript(raw_segments)
            
            new_scene_data = scene.copy()
            new_scene_data['timed_transcript'] = reassembled_segments
            
            # Join with newlines as requested: "Sentence.\nSentence?\nSentence!"
            new_scene_data['transcript'] = "\n".join([s['text'] for s in reassembled_segments])

            transcribed_scenes.append(new_scene_data)

            print(f"     -> OK. Condensed {len(raw_segments)} raw segments into {len(reassembled_segments)} sentences.")
        except Exception as e:
            print(f"     [!] Transcription error: {type(e).__name__}: {e}")
            new_scene_data = scene.copy()
            new_scene_data['timed_transcript'] = []
            new_scene_data['transcript'] = "ERROR: Transcription failed."
            transcribed_scenes.append(new_scene_data)
        finally:
            if audio_path.exists():
                try:
                    audio_path.unlink()
                except Exception:
                    pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(transcribed_scenes, indent=4), encoding="utf-8")
    print(f"\n[+] Done. Saved transcribed manifest to: {out_path}")


if __name__ == "__main__":
    main()