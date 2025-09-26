#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: 3c_whisperx_clean_inout.py
Version: 2.1.0
Last Modified: 2025-09-04

Purpose:
  Transcribe proxy videos with WhisperX using a powerful model and emitting
  word-level timestamps. It also computes "clean" in/out boundaries to ignore
  slate/chatter cues, preparing the data for sentence splitting.

Key Changes in 2.1.0:
  - MAJOR FIX: Aligned with official whisperx examples for stability.
  - Removed all 'prompt' logic, which caused version conflicts.
  - The script now relies on whisperx's internal language detection before alignment.
"""

import os
import re
import gc
import json
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional

import torch
import whisperx

# Optional project paths integration
try:
    from project_paths import add_base_arg, from_args  # type: ignore
    HAVE_PROJECT_PATHS = True
except Exception:
    HAVE_PROJECT_PATHS = False


# ----------------------------
# Defaults & Config
# ----------------------------
DEFAULT_MODEL = "large-v3"
DEFAULT_DEVICE = "auto"
DEFAULT_BATCH = 16
DEFAULT_COMPUTE_CUDA = "float16"
DEFAULT_COMPUTE_CPU = "int8"

DEFAULT_MIN_CONTENT_SEC = 5.0
DEFAULT_MIN_SEG_SEC = 0.70
DEFAULT_HEAD_GUARD = 0.18
DEFAULT_TAIL_GUARD = 0.14
DEFAULT_MIN_WORD_CONF = 0.60
DEFAULT_MAX_COUNTDOWN_SEC = 4.0

DEFAULT_EMIT_WORDS = True
DEFAULT_PRE_ANCHOR_WINDOW = 120.0


# ----------------------------
# CLI / Project Paths
# ----------------------------
def add_common_args(parser: argparse.ArgumentParser) -> None:
    if HAVE_PROJECT_PATHS:
        add_base_arg(parser)

    parser.add_argument("--proxies", type=Path, default=None, help="Directory containing proxy .mp4 files.")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to input master_manifest.json.")
    parser.add_argument("--out", type=Path, default=None, help="Output scene manifest path.")
    parser.add_argument("--ffmpeg", type=str, default=r"C:\ffmpeg\bin\ffmpeg.exe", help="Path to ffmpeg.")

    parser.add_argument("--whisper_model", type=str, default=DEFAULT_MODEL, help="WhisperX model name.")
    parser.add_argument("--device", type=str, choices=["auto", "cuda", "cpu"], default=DEFAULT_DEVICE, help="Device selection.")
    parser.add_argument("--batch_size", type=int, default=DEFAULT_BATCH, help="Transcription batch size.")
    parser.add_argument("--compute_type", type=str, default=None, help="Override compute_type (float16/int8).")
    parser.add_argument("--align_off", action="store_true", help="Disable alignment step (not recommended).")

    parser.add_argument("--diarize", action="store_true", help="Enable speaker diarization (requires HF token).")
    parser.add_argument("--min_speakers", type=int, default=2, help="Min speakers for diarization.")
    parser.add_argument("--hf_token_env", type=str, default="HF_TOKEN", help="Env var name for HuggingFace token.")

    parser.add_argument("--lexicon", type=Path, default=None, help="Optional path to lexicon.json.")
    parser.add_argument("--min_content_sec", type=float, default=DEFAULT_MIN_CONTENT_SEC)
    parser.add_argument("--min_seg_sec", type=float, default=DEFAULT_MIN_SEG_SEC)
    parser.add_argument("--head_guard", type=float, default=DEFAULT_HEAD_GUARD)
    parser.add_argument("--tail_guard", type=float, default=DEFAULT_TAIL_GUARD)
    parser.add_argument("--min_word_conf", type=float, default=DEFAULT_MIN_WORD_CONF)
    parser.add_argument("--max_countdown_sec", type=float, default=DEFAULT_MAX_COUNTDOWN_SEC)
    parser.add_argument("--pre_anchor_window", type=float, default=DEFAULT_PRE_ANCHOR_WINDOW)
    
    parser.add_argument("--keep_audio_cache", action="store_true", help="Keep extracted WAVs.")
    parser.add_argument("--emit_words", action=argparse.BooleanOptionalAction, default=DEFAULT_EMIT_WORDS, help="Emit word-level alignment into output manifest.")


# --- The rest of the script (FFmpeg, Lexicon, WhisperX, Boundary Logic) remains the same ---

def resolve_ffmpeg(ffmpeg_path: str) -> Optional[str]:
    p = Path(ffmpeg_path)
    if p.exists():
        return str(p)
    if ffmpeg_path.lower() == "ffmpeg":
        return "ffmpeg"
    return None


def extract_audio(ffmpeg_bin: str, video_path: Path, wav_out: Path) -> None:
    cmd = [
        ffmpeg_bin, "-y",
        "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(wav_out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def default_lexicon() -> Dict[str, List[str]]:
    return {
        "lead_in": ["rolling", "camera speed", "speed", "mark it", "marker", "slate", "sticks", "scene", "take", "sound speed", "coming in three", "three, two", "coming in 3, 2, 1", "3, 2"],
        "chatter": ["all right", "alright", "okay", "ok", "very nice", "here we go", "yep", "yeah", "ready", "set", "cut", "reset", "again"],
        "clapperboard": ["scene a1", "take one", "take two"],
        "outro": ["cut", "that's a wrap", "we're good"],
        "proper_nouns": ["Michael Frizell", "Fruzel"],
    }


def discover_lexicon_path(cli_path: Optional[Path], base_dir: Optional[Path]) -> Optional[Path]:
    if cli_path and cli_path.exists(): return cli_path
    if base_dir:
        candidate = Path(base_dir) / "lexicon.json"
        if candidate.exists(): return candidate
    return None


def load_lexicon(path: Optional[Path]) -> Dict[str, List[str]]:
    if path and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            base = default_lexicon()
            for k, v in base.items(): data.setdefault(k, v)
            return data
        except Exception: pass
    return default_lexicon()


def build_regex(lex: Dict[str, List[str]]) -> Dict[str, re.Pattern]:
    def normalize(phr: str) -> str: return re.escape(phr.lower())
    groups = {}
    for key in ["lead_in", "chatter", "clapperboard", "outro"]:
        toks = [normalize(t) for t in lex.get(key, []) if t.strip()]
        groups[key] = re.compile(r"\b(" + "|".join(toks) + r")\b", re.IGNORECASE) if toks else re.compile(r"$^\b$")
    return groups

def pick_device(device_flag: str) -> Tuple[str, str]:
    if device_flag in ("cuda", "auto") and torch.cuda.is_available():
        return "cuda", DEFAULT_COMPUTE_CUDA
    return "cpu", DEFAULT_COMPUTE_CPU


def transcribe_and_align(model_name: str, device: str, compute_type: str, audio: Any, batch_size: int, language_fallback: str, do_align: bool) -> Dict[str, Any]:
    """
    Simplified transcription and alignment function that follows the official
    whisperx example workflow for maximum stability.
    """
    model = whisperx.load_model(model_name, device, compute_type=compute_type)
    
    # 1. Transcribe using the simplified API. This will also detect the language.
    result = model.transcribe(audio, batch_size=batch_size)
    
    # 2. Get the detected language, with a fallback if detection fails.
    detected_lang = result.get("language", language_fallback)
    
    # 3. Align using the detected language.
    if do_align:
        if not detected_lang:
             print("    [!] Language not detected and no fallback provided. Cannot perform alignment.")
        else:
            align_model, metadata = whisperx.load_align_model(language_code=detected_lang, device=device)
            result = whisperx.align(result["segments"], align_model, metadata, audio, device)
            # Cleanup to free VRAM
            del align_model
            del metadata

    del model
    return result

def run_diarization_if_enabled(audio: Any, device: str, min_speakers: int, hf_token: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    if not hf_token:
        print("[i] Diarization requested but no HF token found; skipping.")
        return None
    diarize_model = whisperx.diarize.DiarizationPipeline(use_auth_token=hf_token, device=device)
    return diarize_model(audio, min_speakers=min_speakers)


ANCHOR_RE = re.compile(r"(coming\s+in\s+(three|3)[\s,]*(two|2)[\s,]*(one|1))|\bscene\b|\btake\b", re.IGNORECASE)

def is_edge_noise(seg: Dict[str, Any], min_seg_sec: float) -> bool:
    dur = float(seg.get("end", 0.0)) - float(seg.get("start", 0.0))
    return dur < min_seg_sec

def match_any(text: str, patterns: Dict[str, re.Pattern], keys: List[str]) -> bool:
    t = (text or "").lower()
    for k in keys:
        if patterns[k].search(t): return True
    return False

def find_first_confident_word(words: List[Dict[str, Any]], min_conf: float) -> Optional[float]:
    for w in words:
        if w and float(w.get("score", 1.0)) >= min_conf: return w.get("start")
    return None

def find_last_confident_word(words: List[Dict[str, Any]], min_conf: float) -> Optional[float]:
    for w in reversed(words):
        if w and float(w.get("score", 1.0)) >= min_conf: return w.get("end")
    return None

def find_precontent_anchor_index(segments: List[Dict[str, Any]], patterns: Dict[str, re.Pattern], pre_anchor_window: float) -> int:
    last_anchor = -1
    for idx, seg in enumerate(segments):
        if float(seg.get("end", 0.0)) > pre_anchor_window: break
        txt = (seg.get("text") or "").lower()
        if patterns["lead_in"].search(txt) or patterns["clapperboard"].search(txt) or ANCHOR_RE.search(txt):
            last_anchor = idx
    return last_anchor

def compute_clean_bounds(segments: List[Dict[str, Any]], patterns: Dict[str, re.Pattern], **kwargs) -> Tuple[float, float, str]:
    if not segments: return 0.0, 0.0, "no_segments"
    
    start_idx = 0
    anchor_idx = find_precontent_anchor_index(segments, patterns, kwargs["pre_anchor_window"])
    if anchor_idx >= 0 and anchor_idx + 1 < len(segments):
        start_idx = anchor_idx + 1

    i, n = start_idx, len(segments)
    while i < n and (match_any(segments[i].get("text", ""), patterns, ["lead_in", "clapperboard", "chatter"]) or is_edge_noise(segments[i], kwargs["min_seg_sec"])):
        i += 1
    
    j = n - 1
    while j >= 0 and (match_any(segments[j].get("text", ""), patterns, ["outro", "chatter"]) or is_edge_noise(segments[j], kwargs["min_seg_sec"])):
        j -= 1

    orig_in, orig_out = float(segments[0].get("start", 0.0)), float(segments[-1].get("end", 0.0))
    if i > j: return orig_in, orig_out, "trim_all_kept_original"

    cand_in, cand_out = float(segments[i].get("start", orig_in)), float(segments[j].get("end", orig_out))
    words = [w for k in range(i, j + 1) for w in segments[k].get("words", []) if w]
    
    new_in, new_out = cand_in, cand_out
    if words:
        w_in = find_first_confident_word(words, kwargs["min_word_conf"])
        if w_in is not None: new_in = max(cand_in, w_in - kwargs["head_guard"]) # Subtract guard
        w_out = find_last_confident_word(words, kwargs["min_word_conf"])
        if w_out is not None: new_out = min(cand_out, w_out + kwargs["tail_guard"]) # Add guard
        
    new_in, new_out = max(orig_in, min(new_in, new_out)), max(new_in, min(new_out, orig_out))
    if (new_out - new_in) < kwargs["min_content_sec"]: return orig_in, orig_out, "short_after_trim_kept_original"
    
    return new_in, new_out, "trimmed" if i > 0 or j < n - 1 else "no_trim_needed"


def main():
    parser = argparse.ArgumentParser(description="WhisperX transcription + clean in/out boundary finder.")
    add_common_args(parser)
    args = parser.parse_args()

    # Path resolution
    p = from_args(args) if HAVE_PROJECT_PATHS else None
    proxies_dir = Path(args.proxies or (p.proxies if p else "."))
    manifest_in = Path(args.manifest or (p.master_manifest if p else "master_manifest.json"))
    out_path = Path(args.out or (p.scene_manifest if p else "scene_manifest_transcribed.json"))
    temp_audio_dir = Path(p.temp_audio if p else "temp_audio")
    
    ffmpeg_bin = resolve_ffmpeg(args.ffmpeg)
    if not ffmpeg_bin: raise FileNotFoundError(f"ffmpeg not found: {args.ffmpeg}")
    if not manifest_in.exists(): raise FileNotFoundError(f"Master manifest not found: {manifest_in}")
    if not proxies_dir.exists(): raise FileNotFoundError(f"Proxies directory not found: {proxies_dir}")

    lex_path = discover_lexicon_path(args.lexicon, Path(p.base) if p else None)
    lex = load_lexicon(lex_path)
    patterns = build_regex(lex)
    device, compute_type = pick_device(args.device)
    
    scenes = json.loads(manifest_in.read_text(encoding="utf-8"))
    print(f"[+] Found {len(scenes)} scenes. Starting transcription...")
    temp_audio_dir.mkdir(parents=True, exist_ok=True)

    transcribed = []
    for idx, scene in enumerate(scenes, 1):
        proxy_filename = scene.get("proxy_filename")
        if not proxy_filename: continue

        video_path = proxies_dir / proxy_filename
        if not video_path.exists():
            print(f"({idx}/{len(scenes)}) [!] Proxy not found: {video_path}; skipping.")
            continue

        wav_path = temp_audio_dir / f"scene_{scene.get('scene_number', idx)}_audio.wav"
        print(f"({idx}/{len(scenes)}) Processing: {proxy_filename}")

        try:
            extract_audio(ffmpeg_bin, video_path, wav_path)
            audio = whisperx.load_audio(str(wav_path))

            # Call the simplified and robust transcription function
            result = transcribe_and_align(
                args.whisper_model, 
                device, 
                args.compute_type or compute_type, 
                audio, 
                args.batch_size, 
                language_fallback="en", 
                do_align=not args.align_off
            )

            if args.diarize:
                hf_token = os.getenv(args.hf_token_env, "")
                diarize_segments = run_diarization_if_enabled(audio, device, args.min_speakers, hf_token)
                if diarize_segments: result = whisperx.assign_word_speakers(diarize_segments, result)

            segments = result.get("segments", [])
            clean_in, clean_out, notes = compute_clean_bounds(segments, patterns, **vars(args))
            
            # Note: result["text"] may not exist if alignment happens, so we join segments.
            full_text = " ".join(seg.get("text", "").strip() for seg in segments).strip()

            new_scene = {**scene, "transcript": full_text, "timed_transcript": segments, "clean_in": round(clean_in, 3), "clean_out": round(clean_out, 3), "clean_notes": notes}
            if not args.emit_words:
                for seg in new_scene["timed_transcript"]: seg.pop("words", None)
            
            transcribed.append(new_scene)
            print(f"    -> OK | clean_in={new_scene['clean_in']:.3f}s clean_out={new_scene['clean_out']:.3f}s notes={notes}")

        except Exception as e:
            print(f"    [!] ERROR: {type(e).__name__} - {e}")
            transcribed.append({**scene, "transcript": "ERROR", "timed_transcript": [], "clean_in": 0.0, "clean_out": 0.0, "clean_notes": "error"})
        finally:
            if wav_path.exists() and not args.keep_audio_cache: wav_path.unlink()
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

    out_path.write_text(json.dumps(transcribed, indent=2), encoding="utf-8")
    print(f"\n[+] Saved: {out_path}")


if __name__ == "__main__":
    main()