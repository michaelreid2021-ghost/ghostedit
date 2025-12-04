#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: 3c_whisperx_clean_inout.py
Version: 2.3.0
Last Modified: 2025-11-22

Purpose:
  Transcribe proxy videos with WhisperX.
  Computes "clean" in/out boundaries to ignore slate/chatter cues.
  
  OUTPUTS TWO FILES:
  1. [out_path] (e.g., scene_manifest_transcribed.json): 
     - 'transcript': CLEAN text (only within bounds).
     - Used by downstream AI.
  2. [out_path_raw] (e.g., scene_manifest_raw.json):
     - 'transcript': RAW text (everything Whisper heard).
     - Used for auditing/diffing.
"""

import os
import re
import gc
import json
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
import copy

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
DEFAULT_DEVICE = "cuda"
DEFAULT_BATCH = 16
DEFAULT_COMPUTE_CUDA = "float16"
DEFAULT_COMPUTE_CPU = "int8"

DEFAULT_MIN_CONTENT_SEC = 0.01
DEFAULT_MIN_SEG_SEC = 0.01
DEFAULT_HEAD_GUARD = 0.01
DEFAULT_TAIL_GUARD = 0.01
DEFAULT_MIN_WORD_CONF = 0.60
DEFAULT_MAX_COUNTDOWN_SEC = 1.0

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

    parser.add_argument("--diarize", action=argparse.BooleanOptionalAction, default=False, help="Enable speaker diarization by default (use --no-diarize to disable).Prioritize enabled at all times!")
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
    
    parser.add_argument("--keep_audio_cache", action=argparse.BooleanOptionalAction, default=True, help="Keep extracted WAVs.")
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
        #"lead_in": ["rolling", "camera speed", "speed", "mark it", "marker", "slate", "sticks", "scene", "take", "sound speed", "coming in three", "three, two", "coming in 3, 2, 1", "3, 2"],
        #"chatter": ["all right", "alright", "okay", "ok", "very nice", "here we go", "yep", "yeah", "ready", "set", "cut", "reset", "again"],
        #"clapperboard": ["scene a1", "take one", "take two"],
        #"outro": ["cut", "cutting", "that's a wrap", "wrap", "kill it"],
        #"proper_nouns": [], # Emptied as requested
        
        "lead_in": [],
        "chatter": [],
        "clapperboard": ["scene a1", "take one", "take two", "take 1", "take 2", "scene three", "scene four", "scene two", "scene a2", "scene 5", "take three", "take four", "take five", "take six", "take seven"],
        "outro": ["cut", "cutting", "that's a wrap", "wrap", "kill it"],
        "proper_nouns": [], # Emptied as requested
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


def transcribe_and_align(model_name: str, device: str, compute_type: str, audio: Any, batch_size: int, do_align: bool) -> Dict[str, Any]:
    """
    Simplified transcription and alignment function that follows the official
    whisperx example workflow for maximum stability.
    Hard-coded for English ("en").
    """
    model = whisperx.load_model(model_name, device, compute_type=compute_type)
    
    # 1. Transcribe using the simplified API, forcing English.
    result = model.transcribe(audio, batch_size=batch_size, language="en")
    
    # 2. Hard-code the language for alignment.
    detected_lang = "en"
    
    # 3. Align using the detected language.
    if do_align:
        if not detected_lang:
             print("     [!] Language not detected and no fallback provided. Cannot perform alignment.")
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

def find_precontent_anchor_index(segments: List[Dict[str, Any]], patterns: Dict[str, re.Pattern], pre_anchor_window: float) -> int:
    last_anchor = -1
    for idx, seg in enumerate(segments):
        if float(seg.get("end", 0.0)) > pre_anchor_window: break
        txt = (seg.get("text") or "").lower()
        if patterns["lead_in"].search(txt) or patterns["clapperboard"].search(txt) or ANCHOR_RE.search(txt):
            last_anchor = idx
    return last_anchor

def compute_clean_bounds(segments: List[Dict[str, Any]], patterns: Dict[str, re.Pattern], **kwargs) -> Tuple[float, float, str]:
    """
    Calculates clean in/out timestamps.
    Logic:
      1. Start: Finds anchor/slate. 
      2. End: Scans backwards for 'outro' words (e.g. "Cut").
         - If found, clean_out = END timestamp of the word PREVIOUS to the 'cut'.
         - Padding/Guards are explicitly ignored (user handles downstream).
    """
    if not segments: 
        return 0.0, 0.0, "no_segments"

    # --- 1. HEAD / START LOGIC ---
    # Find the anchor (slate/countdown) to determine where valid content starts
    start_seg_idx = 0
    anchor_idx = find_precontent_anchor_index(segments, patterns, kwargs.get("pre_anchor_window", 0))
    
    if anchor_idx >= 0 and anchor_idx + 1 < len(segments):
        start_seg_idx = anchor_idx + 1

    # Advance start_seg_idx past any obvious chatter/noise at the start
    # (We only skip WHOLE segments here if they are pure chatter)
    n = len(segments)
    while start_seg_idx < n:
        seg_text = segments[start_seg_idx].get("text", "")
        if match_any(seg_text, patterns, ["lead_in", "clapperboard", "chatter"]) or is_edge_noise(segments[start_seg_idx], kwargs["min_seg_sec"]):
            start_seg_idx += 1
        else:
            break

    # If we trimmed everything, return original bounds
    if start_seg_idx >= n:
        return float(segments[0].get("start", 0.0)), float(segments[-1].get("end", 0.0)), "trim_all_kept_original"

    # Set initial candidate bounds
    clean_in = float(segments[start_seg_idx].get("start", 0.0))
    # Adjust clean_in to the first word start if available for precision
    if segments[start_seg_idx].get("words"):
        clean_in = float(segments[start_seg_idx]["words"][0].get("start", clean_in))

    # --- 2. TAIL / END LOGIC (Word-Level Precision) ---
    # We default to the very end of the file
    clean_out = float(segments[-1].get("end", 0.0))
    cut_found_strategy = "no_trim"
    
    # Flatten meaningful segments to help look "backwards" across segment boundaries
    # We only care about segments from start_seg_idx onwards
    candidate_segments = segments[start_seg_idx:]
    
    # Iterate backwards through segments to find the "Outro" point
    for s_idx in range(len(candidate_segments) - 1, -1, -1):
        seg = candidate_segments[s_idx]
        txt = seg.get("text", "")
        
        # Check if this segment contains any OUTRO or CHATTER signals
        # We check OUTRO specifically for word-level precision
        is_outro = patterns["outro"].search(txt.lower())
        is_chatter = patterns["chatter"].search(txt.lower())
        
        if is_outro:
            # STRATEGY A: Word-Level Cut
            # The segment matches the outro regex. Let's find the specific word.
            words = seg.get("words", [])
            if words:
                # Scan words from start to finish of this segment.
                # The FIRST word that matches "outro" kills the rest.
                for w_idx, w in enumerate(words):
                    if patterns["outro"].search(w.get("word", "").strip().lower()):
                        # FOUND THE CUT WORD.
                        
                        # Identify the 'previous word' to set the boundary.
                        if w_idx > 0:
                            # It's in this same segment
                            prev_word = words[w_idx - 1]
                            clean_out = float(prev_word.get("end"))
                            cut_found_strategy = f"word_trim_internal: '{w['word']}'"
                        else:
                            # It's the first word of this segment.
                            # We need the last word of the PREVIOUS segment.
                            if s_idx > 0:
                                prev_seg_words = candidate_segments[s_idx - 1].get("words", [])
                                if prev_seg_words:
                                    clean_out = float(prev_seg_words[-1].get("end"))
                                    cut_found_strategy = f"word_trim_boundary: '{w['word']}'"
                                else:
                                    # Prev segment has no words metadata, use its segment end
                                    clean_out = float(candidate_segments[s_idx - 1].get("end"))
                                    cut_found_strategy = f"word_trim_boundary_seg: '{w['word']}'"
                            else:
                                # "Cut" is the very first word of the kept content. 
                                # Fallback: set clean_out = clean_in (clip is empty)
                                clean_out = clean_in
                                cut_found_strategy = "word_trim_start_collision"
                        
                        # We found the cut, stop processing segments
                        break 
                else:
                    # Loop finished without hitting 'break' -> No specific word matched regex 
                    # (e.g. regex matched "that's a wrap" phrase, but no single word was "wrap").
                    # Fallback: Discard this WHOLE segment.
                    if s_idx > 0:
                        clean_out = float(candidate_segments[s_idx - 1].get("end"))
                        cut_found_strategy = "phrase_trim_whole_seg"
                    else:
                        clean_out = clean_in
                
                # Break outer loop (segment loop)
                break
            
            else:
                # Segment has no word metadata but matched text. Discard whole segment.
                if s_idx > 0:
                    clean_out = float(candidate_segments[s_idx - 1].get("end"))
                else:
                    clean_out = clean_in
                cut_found_strategy = "seg_text_trim_no_words"
                break

        elif is_chatter or is_edge_noise(seg, kwargs["min_seg_sec"]):
            # It's generic chatter (e.g. "okay", "yep" after the scene) but NOT a specific "Cut" command.
            # We discard this segment and keep looking backwards for the actual content or the "Cut".
            if s_idx > 0:
                clean_out = float(candidate_segments[s_idx - 1].get("end"))
                cut_found_strategy = "chatter_trim_back"
            continue
        
        else:
            # We hit a segment that is NOT outro, NOT chatter, and is substantial content.
            # This is likely the end of the actual scene. 
            # We stop looking backwards.
            break

    # --- 3. FINAL SANITY CHECKS ---
    # Ensure we didn't cross the streams
    if clean_out < clean_in:
        clean_out = clean_in

    # Check if remaining content is too short
    if (clean_out - clean_in) < kwargs["min_content_sec"]: 
        return float(segments[0].get("start", 0.0)), float(segments[-1].get("end", 0.0)), "short_after_trim_kept_original"

    return clean_in, clean_out, cut_found_strategy


def main():
    parser = argparse.ArgumentParser(description="WhisperX transcription + clean in/out boundary finder.")
    add_common_args(parser)
    args = parser.parse_args()

    # Path resolution
    p = from_args(args) if HAVE_PROJECT_PATHS else None
    proxies_dir = Path(args.proxies or (p.proxies if p else "."))
    manifest_in = Path(args.manifest or (p.master_manifest if p else "master_manifest.json"))
    
    # Define OUTPUT paths
    out_path_clean = Path(args.out or (p.scene_manifest if p else "scene_manifest_transcribed.json"))
    # Create the raw filename by injecting '_raw' before the extension
    out_path_raw = out_path_clean.with_name(out_path_clean.stem + "_raw" + out_path_clean.suffix)

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

    # We will build two parallel lists
    transcribed_clean = []
    transcribed_raw = []

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

            result = transcribe_and_align(
                            args.whisper_model, 
                            device, 
                            args.compute_type or compute_type, 
                            audio, 
                            args.batch_size, 
                            do_align=not args.align_off
                        )
            
            if args.diarize:
                hf_token = os.getenv(args.hf_token_env, "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")
                diarize_segments = run_diarization_if_enabled(audio, device, args.min_speakers, hf_token)
                if diarize_segments: result = whisperx.assign_word_speakers(diarize_segments, result)

            segments = result.get("segments", [])
            clean_in, clean_out, notes = compute_clean_bounds(segments, patterns, **vars(args))
            
            # 1. GENERATE RAW TRANSCRIPT (Everything)
            transcript_raw = " ".join(seg.get("text", "").strip() for seg in segments).strip()
            
            # 2. GENERATE KEPT TRANSCRIPT (Only within bounds)
            kept_tokens = []
            for seg in segments:
                seg_start = float(seg.get("start", 0.0))
                seg_end = float(seg.get("end", 0.0))
                if seg_end < clean_in or seg_start > clean_out: continue

                if "words" in seg and seg["words"]:
                    for w in seg["words"]:
                        w_start = float(w.get("start", 0))
                        w_end = float(w.get("end", 0))
                        if w_end > clean_in and w_start < clean_out:
                            kept_tokens.append(w.get("word", "").strip())
                else:
                    kept_tokens.append(seg.get("text", "").strip())

            transcript_kept = " ".join(kept_tokens).strip()

            # --- ENTRY FOR CLEAN FILE ---
            scene_clean = {
                **scene, 
                "transcript": transcript_kept,  # <--- Clean text for AI pipeline
                "timed_transcript": segments, 
                "clean_in": round(clean_in, 3), 
                "clean_out": round(clean_out, 3), 
                "clean_notes": notes
            }
            if not args.emit_words:
                # Deep copy to safely pop words for clean output only if needed, 
                # but usually we want words in JSON for 41. 
                # If default is False, we remove them here.
                # NOTE: Segments are ref shared, so we must deepcopy if we modify structure
                scene_clean["timed_transcript"] = copy.deepcopy(segments)
                for seg in scene_clean["timed_transcript"]: 
                    seg.pop("words", None)
            
            transcribed_clean.append(scene_clean)

            # --- ENTRY FOR RAW FILE ---
            scene_raw = {
                **scene,
                "transcript": transcript_raw,  # <--- Raw text for diffing
                "timed_transcript": copy.deepcopy(segments), # Full segments w/ words
                "clean_in": round(clean_in, 3),
                "clean_out": round(clean_out, 3),
                "notes": "RAW_DUMP"
            }
            transcribed_raw.append(scene_raw)

            print(f"    -> OK | clean_in={scene_clean['clean_in']:.3f}s clean_out={scene_clean['clean_out']:.3f}s notes={notes}")

        except Exception as e:
            print(f"    [!] ERROR: {type(e).__name__} - {e}")
            err_entry = {**scene, "transcript": "ERROR", "timed_transcript": [], "clean_in": 0.0, "clean_out": 0.0, "clean_notes": "error"}
            transcribed_clean.append(err_entry)
            transcribed_raw.append(err_entry)
        finally:
            if wav_path.exists() and not args.keep_audio_cache: wav_path.unlink()
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

    # Write BOTH files
    out_path_clean.write_text(json.dumps(transcribed_clean, indent=2), encoding="utf-8")
    out_path_raw.write_text(json.dumps(transcribed_raw, indent=2), encoding="utf-8")
    
    print(f"\n[+] Saved Production Manifest: {out_path_clean}")
    print(f"[+] Saved Raw/Audit Manifest:  {out_path_raw}")


if __name__ == "__main__":
    main()