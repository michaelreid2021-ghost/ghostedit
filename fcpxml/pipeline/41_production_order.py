#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 41_production_order.py (subtitle-aware, AI-driven, with logging + consolidation)
# Version: 4.1.0 (Fixed Matching Logic + Debug Logging)
# Last Modified: 2025-11-22

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
import difflib
import logging
import yaml

# --- FORCE UTF-8 OUTPUT (Windows Fix) ---
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from project_paths import add_base_arg, from_args, get_active_project_name
from token_logger import log_token_usage
from gemini_client import GeminiClient

# Setup logging
script_dir = Path(__file__).resolve().parent
log_file_path = script_dir / "41_production_order.log"
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    file_handler = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    logger.addHandler(stream_handler)

# =========================
# SRT parsing
# =========================
def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()

def _tc_to_seconds(tc: str) -> float:
    hh, mm, ss_ms = tc.split(":")
    ss, ms = ss_ms.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

def parse_srt(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Expected subtitle file not found: {path}")
    content = path.read_text(encoding="utf-8").strip()
    blocks = re.split(r"\n\s*\n", content)
    out: List[Dict[str, Any]] = []
    for b in blocks:
        lines = [ln.rstrip("\n") for ln in b.splitlines() if ln.strip() != ""]
        if len(lines) < 2 or not lines[0].strip().isdigit():
            continue
        tc_line = lines[1].strip()
        m = re.match(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", tc_line)
        if not m:
            continue
        start = _tc_to_seconds(m.group(1))
        end = _tc_to_seconds(m.group(2))
        text = _strip_tags(" ".join(lines[2:]).strip())
        out.append({"id": int(lines[0].strip()), "start": start, "end": end, "text": text})
    out.sort(key=lambda x: x["start"])
    return out

def normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s']", "", s)
    return s.strip()

# =========================
# Advanced Matching (Ported from Script 45)
# =========================
def find_phrase_time(captions: List[Dict[str, Any]], phrase: str) -> Optional[float]:
    """
    Flattens the SRT into a single string to find matches that span across 
    fragmented subtitle lines.
    """
    if not phrase:
        return None
    
    target = normalize_text(phrase)
    if not target:
        return None
    
    # 1. Construct the flat string and keep a map of indices
    flat_parts = []
    mapping = [] # List of (start_index, length, start_time, end_time)
    cursor = 0
    
    for c in captions:
        t = normalize_text(c["text"])
        # We add 1 for the space we insert during join
        length = len(t)
        mapping.append((cursor, length, c["start"], c["end"]))
        flat_parts.append(t)
        cursor += length + 1 # +1 for the space
        
    flat = " ".join(flat_parts)
    
    # 2. Find the index
    idx = flat.find(target)
    
    # 3. Fallback: If exact match fails, try a looser search (first 20 chars)
    #    This helps if the AI hallucinated the end of the sentence but got the start right.
    if idx == -1 and len(target) > 20:
        short_target = target[:20]
        logger.debug(f"     ...Exact match failed for '{target[:30]}...', trying prefix '{short_target}'")
        idx = flat.find(short_target)

    if idx == -1:
        return None

    # 4. Map index back to time
    for c0, ln, s, e in mapping:
        c1 = c0 + ln
        # Check if the start of the match falls within this caption
        # We allow it to match if it starts in the whitespace immediately following (handled by the next loop usually, but edge cases)
        if c0 <= idx < c1 + 1: 
            # Calculate interpolation within the caption
            within = idx - c0
            if ln > 0:
                frac = max(0.0, min(1.0, within / ln))
                return float(s + (e - s) * frac)
            else:
                return float(s)
                
    return None

def merge_consecutive_segments(sorted_sequence: list) -> list:
    """Merges adjacent segments if they have the same segment_type."""
    if not sorted_sequence:
        return []

    merged_sequence = [sorted_sequence[0]]
    for current_segment in sorted_sequence[1:]:
        last_segment = merged_sequence[-1]
        if current_segment.get("segment_type") == last_segment.get("segment_type"):
            continue
        else:
            merged_sequence.append(current_segment)
    
    return merged_sequence

# =========================
# Main
# =========================
def main():
    
    parser = argparse.ArgumentParser(description="Build a high-granularity production order from a final timeline SRT.")
    add_base_arg(parser)
    args = parser.parse_args()

    p = from_args(args)
    working_root = Path(p.working_root)
    script_dir = Path(__file__).resolve().parent
    # Load YAML config
    config_path = script_dir / "41_config.yaml"
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    script_config = config['script']
    model_config = script_config['model']
    system_prompt = config['prompts']['system']
    user_prompt_template = config['prompts']['user']
    script_name = script_config['name']
    srt_file = script_config['srt_file']
    output_file = script_config['output_file']

    success_flag = p.workspace / f"{script_name}.success"
    fail_flag = p.workspace / f"{script_name}.fail"

    if success_flag.exists():
        logger.info(f"[+] Script {script_name} has already run successfully. Skipping.")
        return
    if fail_flag.exists():
        logger.info(f"[i] Retrying script {script_name} after previous failure.")
        fail_flag.unlink()

    try:
        logger.info(f"[+] Starting Production Order Generator (v{script_config['version']})...")

        # Load response schema
        schema_path = script_dir / "41_response_schema.json"
        if not schema_path.exists():
            logger.error(f"Schema file not found: {schema_path}")
            raise FileNotFoundError(f"Schema file not found: {schema_path}")
        with open(schema_path, 'r', encoding='utf-8') as f:
            response_schema = json.load(f)

        # Load SRT
        srt_path = working_root / srt_file
        captions = parse_srt(srt_path)
        transcript_text = "\n".join([c["text"] for c in captions])
        logger.info(f"[+] Loaded {len(captions)} caption items from: {srt_path}")

        # Build user prompt
        user_prompt = user_prompt_template.replace("{{transcript_text}}", transcript_text)

        # AI Call
        logger.info("-> Sending full transcript to AI for high-level analysis...")
        client = GeminiClient(
            project_id=model_config['project_id'],
            location=model_config['location'],
            model_name=model_config['model_name'],
            safety_settings=model_config['safety_settings']
        )
        response = client.query(
            system_instruction=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
            temperature=model_config['temperature'],
            top_p=model_config['top_p'],
            seed=model_config['seed'],
            max_output_tokens=model_config['max_output_tokens'],
            thinking_budget=model_config['thinking_budget']
        )

        if not (response and response.text):
            logger.error("[!] AI analysis failed to return a valid response. Aborting.")
            fail_flag.touch()
            raise SystemExit(1)

        log_token_usage(
            get_active_project_name(), Path(__file__).name, model_config['model_name'],
            response.usage_metadata.prompt_token_count,
            response.usage_metadata.candidates_token_count,
            response.usage_metadata.total_token_count
        )

        # Process AI Response
        production_order = json.loads(response.text)
        video_sequence = production_order.get('video_sequence', [])
        logger.info(f" -> AI returned {len(video_sequence)} high-level segments. Anchoring to SRT timestamps...")

        total_duration = max(c["end"] for c in captions) - min(c["start"] for c in captions) if captions else 0.0
        first_start = min(c["start"] for c in captions) if captions else 3600.0

        for i, segment in enumerate(video_sequence):
            quote = segment.get("anchor_quote", "")
            start_time = None

            # --- IMPROVED MATCHING LOGIC ---
            # Instead of comparing line-by-line, we search the whole text stream
            found_time = find_phrase_time(captions, quote)

            if found_time is not None:
                start_time = found_time
                logger.debug(f"   [MATCH] Quote: '{quote[:30]}...' -> Found at {start_time:.2f}s")
            else:
                # Fallback Estimate
                est_time = first_start + (total_duration * (i / max(1, len(video_sequence))))
                logger.warning(f"   [FAIL] Unmatched quote: '{quote[:50]}...'. Using estimate {est_time:.2f}s")
                logger.debug(f"          -> Full Failed Quote: {quote}") 
                start_time = est_time

            segment["start_seconds"] = round(start_time, 2)
            segment["entry_point_phrase"] = segment["anchor_quote"]

        # Sort and merge segments
        video_sequence.sort(key=lambda x: x.get("start_seconds", 0))
        logger.info(" -> Re-sorted all segments into chronological order.")
        merged_video_sequence = merge_consecutive_segments(video_sequence)
        if len(merged_video_sequence) < len(video_sequence):
            merged_count = len(video_sequence) - len(merged_video_sequence)
            logger.info(f" -> Merged {merged_count} consecutive segments of the same type.")

        # Final Output
        final_output_structure = {
            "source_srt": str(srt_path.name),
            "generation_metadata": {
                "message_style": production_order.get("message_style"),
                "style_confidence": production_order.get("style_confidence"),
                "series_context": production_order.get("series_context")
            },
            "project_plan": [{
                "take_number": "01",
                "proxy_filename": srt_file,
                "braw_filename": srt_file,
                "segments": merged_video_sequence
            }]
        }

        out_path = working_root / output_file
        out_path.write_text(json.dumps(final_output_structure, indent=2), encoding='utf-8')
        logger.info(f"[+] Success! Wrote production order with {len(merged_video_sequence)} final segments -> {out_path}")
        success_flag.touch()

    except Exception as e:
        logger.error(f"[!] An unexpected error occurred: {e}", exc_info=True)
        fail_flag.touch()
        raise

if __name__ == "__main__":
    main()