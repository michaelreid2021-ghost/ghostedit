#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: transcribe_manifest_whisper.py
Version: 1.2.0
Last Modified: 2025-08-18
Purpose: Transcribe proxy videos into a scene manifest using OpenAI Whisper.
         Defaults to `medium.en` and segment-level timing only (no word-level),
         with automatic CUDA/CPU selection and sensible decoding defaults for clean English dialog.
"""

import os
import json
import argparse
import subprocess
from pathlib import Path
from typing import Optional

import torch
import whisper

from project_paths import add_base_arg, from_args

DEFAULT_WHISPER_MODEL = "large-v3"


# ----------------------------
# Audio Extraction
# ----------------------------
def extract_audio(ffmpeg_path: str, video_path: Path, output_wav_path: Path) -> None:
    """Extract mono 16kHz PCM WAV from the full video."""
    cmd = [
        ffmpeg_path,
        "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(output_wav_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ----------------------------
# Helpers
# ----------------------------
def resolve_ffmpeg(ffmpeg_path: str) -> Optional[str]:
    p = Path(ffmpeg_path)
    if p.exists():
        return str(p)
    # allow PATH resolution when user passes "ffmpeg"
    if ffmpeg_path.lower() == "ffmpeg":
        return "ffmpeg"
    return None


def pick_device(user_choice: str) -> str:
    if user_choice == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if user_choice == "cuda" and not torch.cuda.is_available():
        print("[!] CUDA requested but not available; falling back to CPU.")
        return "cpu"
    return user_choice


def load_lexicon(path: Optional[Path]) -> str:
    if not path:
        return ""
    if not path.exists():
        print(f"[!] Lexicon file not found: {path}")
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"[!] Failed to read lexicon: {type(e).__name__}: {e}")
        return ""


def transcribe_with_fallback(
    model_name: str,
    device: str,
    audio_path: Path,
    initial_prompt: str,
    language: str,
    temperature: float,
    beam_size: int,
    best_of: int,
    condition_on_previous_text: bool,
    fp16: Optional[bool],
    no_word_timestamps: bool,
) -> dict:
    """
    Transcribe once on chosen device; if CUDA OOM occurs, retry on CPU automatically.
    """
    def _run(_device: str) -> dict:
        use_fp16 = (fp16 if fp16 is not None else (_device == "cuda"))
        model = whisper.load_model(model_name, device=_device)
        # Only segment-level timing; no per-word breakdown by default.
        return model.transcribe(
            str(audio_path),
            language=language,
            temperature=temperature,
            beam_size=beam_size if temperature == 0 else None,
            best_of=None if temperature == 0 else best_of,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt if initial_prompt else None,
            fp16=use_fp16,
            word_timestamps=(not no_word_timestamps),
        )

    try:
        return _run(device)
    except RuntimeError as e:
        msg = str(e).lower()
        if "cuda" in msg and "out of memory" in msg and device == "cuda":
            print("[!] CUDA OOM encountered; retrying on CPU...")
            torch.cuda.empty_cache()
            return _run("cpu")
        raise


# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser(description="Transcribe proxy videos with Whisper into a scene manifest (segment-level only).")
    add_base_arg(parser)

    # I/O + environment
    parser.add_argument("--proxies", type=Path, default=None, help="Directory containing proxy .mp4 files (defaults to project_paths.proxies).")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to master_manifest.json (defaults to project_paths.master_manifest).")
    parser.add_argument("--out", type=Path, default=None, help="Path to output scene_manifest_transcribed.json (defaults to project_paths.scene_manifest).")
    parser.add_argument("--ffmpeg", type=str, default="C:\\ffmpeg\\bin\\ffmpeg.exe", help="Path to ffmpeg (or 'ffmpeg' if on PATH).")

    # Whisper config
    parser.add_argument("--whisper_model", type=str, default=DEFAULT_WHISPER_MODEL, help="Whisper model name (e.g., medium.en, large, turbo).")
    parser.add_argument("--device", type=str, choices=["auto", "cuda", "cpu"], default="auto", help="Device selection (default: auto).")
    parser.add_argument("--language", type=str, default="en", help="Language hint (default: en).")
    parser.add_argument("--temperature", type=float, default=0.0, help="Decoding temperature; 0 for deterministic/beam search.")
    parser.add_argument("--beam_size", type=int, default=5, help="Beam size when temperature == 0.")
    parser.add_argument("--best_of", type=int, default=5, help="Best-of samples when temperature > 0.")
    parser.add_argument("--condition_on_previous_text", action="store_true", help="Condition on previous text (off by default).")
    parser.add_argument("--fp16", dest="fp16", action="store_true", help="Force fp16=True.")
    parser.add_argument("--no-fp16", dest="fp16", action="store_false", help="Force fp16=False.")
    parser.set_defaults(fp16=None)  # auto: cuda->True, cpu->False

    # Prompts / lexicon
    parser.add_argument("--initial_prompt", type=str, default="", help="Inline initial prompt for domain lexicon (slates, names, brands).")
    parser.add_argument("--lexicon", type=Path, default=None, help="Path to a text file with domain terms to seed as initial_prompt.")

    # Output shaping
    parser.add_argument("--no-word-timestamps", action="store_true", default=True, help="Disable per-word timestamps (default: True).")
    parser.add_argument("--keep-audio-cache", action="store_true", help="Do not delete temp WAVs after transcription.")

    args = parser.parse_args()
    p = from_args(args)

    proxies_dir = args.proxies or p.proxies
    manifest_path = args.manifest or p.master_manifest
    out_path = args.out or p.scene_manifest
    temp_audio_dir = p.temp_audio

    ffmpeg_bin = resolve_ffmpeg(args.ffmpeg)
    if not ffmpeg_bin:
        print(f"[!] ffmpeg not found at: {args.ffmpeg}")
        return

    if not manifest_path.exists():
        print(f"[!] master manifest not found: {manifest_path}")
        return

    if not proxies_dir.exists():
        print(f"[!] proxies directory not found: {proxies_dir}")
        return

    device = pick_device(args.device)
    lexicon_text = load_lexicon(args.lexicon)
    initial_prompt = (lexicon_text + ("\n" + args.initial_prompt if args.initial_prompt else "")).strip()

    print("[+] Transcription configuration")
    print(f"  base dir         : {p.base}")
    print(f"  proxies          : {proxies_dir}")
    print(f"  manifest in      : {manifest_path}")
    print(f"  out path         : {out_path}")
    print(f"  temp audio dir   : {temp_audio_dir}")
    print(f"  ffmpeg           : {ffmpeg_bin}")
    print(f"  model            : {args.whisper_model}")
    print(f"  device           : {device}")
    print(f"  language         : {args.language}")
    print(f"  temperature      : {args.temperature}")
    print(f"  beam_size        : {args.beam_size}")
    print(f"  best_of          : {args.best_of}")
    print(f"  cond_prev_text   : {args.condition_on_previous_text}")
    print(f"  fp16 (auto None) : {args.fp16}")
    print(f"  word timestamps  : {not args.no_word_timestamps}")
    print(f"  initial_prompt   : {'<provided>' if initial_prompt else '<none>'}")

    scenes = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"[+] Found {len(scenes)} scene entries.")

    temp_audio_dir.mkdir(parents=True, exist_ok=True)

    transcribed = []
    for i, scene in enumerate(scenes, 1):
        proxy_filename = scene.get("proxy_filename")
        scene_number = scene.get("scene_number", i)
        if not proxy_filename:
            print(f"({i}/{len(scenes)}) [!] Missing proxy_filename; skipping.")
            continue

        video_path = Path(proxies_dir) / proxy_filename
        if not video_path.exists():
            print(f"({i}/{len(scenes)}) [!] Proxy not found: {video_path}; skipping.")
            continue

        audio_path = Path(temp_audio_dir) / f"scene_{scene_number}_audio.wav"
        print(f"({i}/{len(scenes)}) Transcribing {proxy_filename}...")

        try:
            extract_audio(ffmpeg_bin, video_path, audio_path)

            result = transcribe_with_fallback(
                model_name=args.whisper_model,
                device=device,
                audio_path=audio_path,
                initial_prompt=initial_prompt,
                language=args.language,
                temperature=args.temperature,
                beam_size=args.beam_size,
                best_of=args.best_of,
                condition_on_previous_text=args.condition_on_previous_text,
                fp16=args.fp16,
                no_word_timestamps=args.no_word_timestamps,
            )

            # Keep only segment-level info; drop per-word content if present.
            slim_segments = []
            for seg in result.get("segments", []):
                slim_segments.append({
                    "id": seg.get("id"),
                    "seek": seg.get("seek"),
                    "start": seg.get("start"),
                    "end": seg.get("end"),
                    "text": seg.get("text", "").strip(),
                    "avg_logprob": seg.get("avg_logprob"),
                    "no_speech_prob": seg.get("no_speech_prob"),
                    "temperature": seg.get("temperature"),
                    "compression_ratio": seg.get("compression_ratio"),
                })

            new_scene = dict(scene)
            new_scene["timed_transcript"] = slim_segments
            new_scene["transcript"] = result.get("text", "").strip()
            transcribed.append(new_scene)

            print(f"    -> OK. Segments: {len(slim_segments)}")
        except Exception as e:
            print(f"    [!] Transcription error: {type(e).__name__}: {e}")
            new_scene = dict(scene)
            new_scene["timed_transcript"] = []
            new_scene["transcript"] = "ERROR: Transcription failed."
            transcribed.append(new_scene)
        finally:
            if audio_path.exists() and not args.keep_audio_cache:
                try:
                    audio_path.unlink()
                except Exception:
                    pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(transcribed, indent=4), encoding="utf-8")
    print(f"\n[+] Done. Saved transcribed manifest to: {out_path}")


if __name__ == "__main__":
    main()

# Changelog
# 1.2.0 (2025-08-18): Default to medium.en; segment-only output; auto CUDA/CPU; CUDA OOM fallback to CPU; optional lexicon seed.
# 1.1.0 (2025-08-17): Added device/precision flags; decoding knobs (beam/best_of/temperature).
# 1.0.0 (2025-08-15): Initial version based on project_paths manifest workflow.
