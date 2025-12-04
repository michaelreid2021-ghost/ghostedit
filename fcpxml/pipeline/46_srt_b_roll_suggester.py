#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 46_srt_b_roll_suggester.py
# Version: 3.5.1 (UTF-8 Console Fix)
# Last Modified: 2025-11-21
#
# Purpose:
#   Generate timecoded B-roll plan from SRT with windowed, segment-aware tagging.
#   Loads system prompt from 46_config.yaml and schema from 46_response_schema.json.

import json
import sys
import argparse
import os
import re
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging

# --- FORCE UTF-8 OUTPUT (Windows Fix) ---
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from project_paths import add_base_arg, from_args, ProjectPaths, get_active_project_name
from token_logger import log_token_usage

# --- Gemini / Vertex imports ---
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except Exception:
    _GENAI_AVAILABLE = False

# --- YAML Import ---
try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False
    print("YAML package not found. Please install PyYAML: pip install PyYAML", file=sys.stderr)


script_dir = Path(__file__).resolve().parent
log_file_path = script_dir / "46_srt_b_roll_suggester.log"
debug_response_path = script_dir / "46_gemini_response_debug.json"
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

# -------------------------------
# Config Toggles
# -------------------------------
SKIP_SEGMENT_TYPES = {"Hook", "Disclaimer", "Introduction"}
ANALYSIS_WINDOW_SEC = 60.0
MIN_LOOKAHEAD_SEC = 30.0
MIN_GAP_CAPTIONS = 5
MIN_BROLL_DURATION = 2.0
MIN_BROLL_AFTER = 5.0
PROMPT_PREVIEW_CHARS = 800

# --------------------------------
# SRT Parsing & Helpers
# --------------------------------
@dataclass
class Caption:
    index: int
    start: float
    end: float
    text: str

def srt_time_to_seconds(t: str) -> float:
    m = re.match(r"^\s*(\d{2}):(\d{2}):(\d{2}),(\d{1,3})\s*$", t)
    if not m: raise ValueError(f"Invalid SRT time '{t}'")
    hh, mm, ss, ms = (int(m.group(i)) for i in range(1, 5))
    return hh * 3600 + mm * 60 + ss + ms / 1000.0

def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"</?[^>]+>", "", s)
    return s

def parse_srt(path: Path) -> List[Caption]:
    if not path.exists(): raise FileNotFoundError(f"SRT not found: {path}")
    content = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", content.strip(), flags=re.MULTILINE)
    out: List[Caption] = []
    idx_auto = 1
    for block in blocks:
        lines = [ln.rstrip("\r") for ln in block.splitlines() if ln.strip()]
        if not lines: continue
        idx = idx_auto
        try:
            idx = int(lines[0].strip())
            lines = lines[1:]
        except Exception:
            idx_auto += 1
        tm_line = next((ln for ln in lines if "-->" in ln), None)
        if not tm_line: continue
        m = re.match(r"^\s*([0-9:,\s]+)\s*-->\s*([0-9:,\s]+)", tm_line)
        if not m: continue
        start_s = srt_time_to_seconds(m.group(1).strip())
        end_s = srt_time_to_seconds(m.group(2).strip())
        reached_tm = False
        text_lines: List[str] = []
        for ln in lines:
            if not reached_tm and "-->" in ln:
                reached_tm = True
                continue
            if reached_tm:
                txt = strip_html(ln).strip()
                if txt:
                    text_lines.append(txt)
        text = " ".join(text_lines)
        out.append(Caption(index=idx, start=start_s, end=end_s, text=text))
    out.sort(key=lambda c: (c.start, c.end, c.index))
    return out

def normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s']", "", s)
    return s.strip()

def stream_total_end(caps: List[Caption]) -> float:
    return max((c.end for c in caps), default=0.0)

def find_phrase_time(captions: List[Caption], phrase: str) -> Optional[float]:
    if not phrase: return None
    target = normalize_text(phrase)
    if not target: return None
    flat_parts = []
    mapping = []
    cursor = 0
    for c in captions:
        t = normalize_text(c.text)
        mapping.append((cursor, len(t), c.start, c.end))
        flat_parts.append(t)
        cursor += len(t) + 1
    flat = " ".join(flat_parts)
    idx = flat.find(target)
    if idx == -1:
        logger.debug(f"Unmatched phrase: '{phrase}'")
        return None
    for c0, ln, s, e in mapping:
        c1 = c0 + ln
        if ln > 0 and c0 <= idx < c1:
            within = idx - c0
            frac = max(0.0, min(1.0, within / ln))
            return float(s + (e - s) * frac)
    return None

def _first_caption_index_at_or_after(caps: List[Caption], t: float) -> Optional[int]:
    for i, c in enumerate(caps):
        if c.start >= t:
            return i
    if caps and t > caps[-1].start:
        return len(caps) - 1
    return None

def discover_srt(working_root: Path, base: Path, info_fcpxml: Path) -> Tuple[Optional[Path], List[Path]]:
    tried: List[Path] = []
    preferred_names = [
        "subtitle 1.srt", "Subtitle 1.srt", "subtitle_1.srt", "subtitles.srt",
        "captions.srt", "clean_subtitles.srt", "subtitle1.srt"
    ]
    for name in preferred_names:
        p = working_root / name
        tried.append(p)
        if p.exists():
            return p, tried
    fcpxml_peers = [
        info_fcpxml,
        working_root / "ai_final_cut.fcpxml",
        working_root / "26_adjusted.fcpxml",
        base / "ai_final_cut.fcpxml",
        base / "26_adjusted.fcpxml",
    ]
    peer_dirs = {x.parent for x in fcpxml_peers if isinstance(x, Path) and x.exists()}
    for d in peer_dirs:
        for name in preferred_names:
            p = d / name
            tried.append(p)
            if p.exists():
                return p, tried
    cwd = Path.cwd()
    for name in preferred_names:
        p = cwd / name
        tried.append(p)
        if p.exists():
            return p, tried
    roots = [working_root, base, cwd]
    seen: set[Path] = set()
    for r in roots:
        if not r.exists() or r in seen:
            continue
        seen.add(r)
        for p in r.glob("*.srt"):
            tried.append(p)
            if p.name.lower().startswith("subtitle") or p.name.lower() in {"subtitles.srt", "captions.srt"}:
                return p, tried
        for sub in [d for d in r.iterdir() if d.is_dir()]:
            for p in sub.glob("*.srt"):
                tried.append(p)
                if p.name.lower().startswith("subtitle") or p.name.lower() in {"subtitles.srt", "captions.srt"}:
                    return p, tried
    for r in roots:
        if not r.exists():
            continue
        for p in r.rglob("*.srt"):
            tried.append(p)
            if p.name.lower().startswith("subtitle") or p.name.lower() in {"subtitles.srt", "captions.srt"}:
                return p, tried
    return None, tried

# --------------------------------
# Segments
# --------------------------------
def load_production_order(p: ProjectPaths) -> Optional[Dict[str, Any]]:
    path = p.working_root / "41_production_order.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def derive_segments_from_41(captions: List[Caption], prod41: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    plan = (prod41 or {}).get("project_plan") or []
    
    # Default fallback if no plan exists
    if not isinstance(plan, list) or not plan:
        if captions:
            out.append({"segment_type": "Default", "entry_point_phrase": "", "start": captions[0].start, "end": stream_total_end(captions)})
        return out

    segs: List[Dict[str, Any]] = []
    for item in plan:
        s = item.get("segments") or []
        if isinstance(s, list):
            segs.extend(s)

    anchors: List[Tuple[str, Optional[str], Optional[float]]] = []
    
    for seg in segs:
        st = str(seg.get("segment_type", "Default"))
        ep = seg.get("entry_point_phrase")
        
        # 1. DEFACTO TRUTH: Check 'start_seconds' first
        # We trust the upstream pipeline unless the neutrinos interfere.
        t = None
        json_start = seg.get("start_seconds")
        
        if json_start is not None:
            try:
                t = float(json_start)
            except (ValueError, TypeError):
                logger.warning(f"Invalid start_seconds '{json_start}' for segment '{st}'; falling back to text match.")
                t = None

        # 2. FALLBACK: Only if start_seconds is missing/invalid do we search text
        if t is None and isinstance(ep, str) and ep.strip():
            t = find_phrase_time(captions, ep)

        anchors.append((st, ep, t))

    # Determine boundaries
    first_cap_start = captions[0].start if captions else 3600.0
    usable: List[Tuple[str, Optional[str], float]] = []
    
    for i, (st, ep, t) in enumerate(anchors):
        if t is not None:
            usable.append((st, ep, float(t)))
        else:
            # If we are the FIRST segment and have no time, safe to default to start.
            # Otherwise, we skip it because we don't know where to put it.
            if i == 0:
                 logger.debug(f"Segment '{st}' has no time; defaulting to start ({first_cap_start:.2f}s)")
                 usable.append((st, ep, first_cap_start))
            else:
                 logger.warning(f"Skipping segment '{st}' - no start_seconds and phrase not found.")

    if not usable and captions:
        out.append({"segment_type": "Default", "entry_point_phrase": "", "start": first_cap_start, "end": stream_total_end(captions)})
        return out

    # Sort strictly by time
    usable.sort(key=lambda x: x[2])
    
    total_end = stream_total_end(captions)
    
    # Build the segments (Current Start -> Next Start)
    for i, (st, ep, t) in enumerate(usable):
        seg_start = t
        
        # End is either the next segment's start OR the end of the video
        if i + 1 < len(usable):
            seg_end = usable[i + 1][2]
        else:
            seg_end = total_end
        
        # Sanity check: If next segment starts BEFORE this one (shouldn't happen due to sort, but good for safety)
        if seg_end <= seg_start:
            seg_end = total_end

        # Filter out 0-length or negative segments
        if seg_end - seg_start < 0.1:
            continue
            
        out.append({"segment_type": st, "entry_point_phrase": ep or "", "start": seg_start, "end": seg_end})
        
    return out

def find_pacing_plan_file(p: ProjectPaths, srt_path: Path) -> Optional[Path]:
    base_stem = re.sub(r"\s+", "_", srt_path.stem.strip().lower())
    plan_path = p.plans_dir / f"{base_stem}_changes.json"
    return plan_path if plan_path.exists() else None

def load_angle_changes(pacing_file: Optional[Path]) -> List[Dict[str, Any]]:
    if not pacing_file: return []
    data = safe_json_load(pacing_file)
    if not (data and isinstance(data.get("angle_changes"), list)): return []
    return sorted([
        {"t": float(c["absolute_cut_time"]), "angle": c.get("angle_choice", "")}
        for c in data["angle_changes"] if "absolute_cut_time" in c
    ], key=lambda x: x["t"])

# --------------------------------
# Window Gathering
# --------------------------------
def find_current_segment(segments: List[Dict[str, Any]], t: float) -> Optional[Dict[str, Any]]:
    for seg in segments:
        if seg["start"] <= t < seg["end"]:
            return seg
    return None

def parse_timestamp_str(ts_str: str) -> float:
    ts_str = ts_str.strip('[]')
    if ts_str.endswith('s'):
        ts_str = ts_str[:-1]
    try:
        return float(ts_str)
    except ValueError:
        logger.warning(f"Invalid timestamp '{ts_str}'; returning 0.0")
        return 0.0

def gather_window_with_segments(
    captions: List[Caption],
    segments: List[Dict[str, Any]],
    current_time: float,
    duration: float,
    previous_broll_end: Optional[float] = None,
    angle_changes: List[Dict[str, Any]] = None,
) -> str:
    lines: List[str] = []
    end_time = current_time + duration
    context_start = max(0.0, current_time - 10.0)

    start_idx = _first_caption_index_at_or_after(captions, context_start)
    if start_idx is None:
        logger.debug(f"Empty window at {current_time:.2f}s: No captions after {context_start:.2f}s")
        return ""

    prev_idx = _first_caption_index_at_or_after(captions, previous_broll_end) if previous_broll_end else None
    curr_idx = _first_caption_index_at_or_after(captions, current_time)

    seg_lines = []
    current_seg = None
    for i in range(start_idx, len(captions)):
        c = captions[i]
        if c.start >= end_time:
            break

        seg_at_time = find_current_segment(segments, c.start)
        if seg_at_time != current_seg:
            if current_seg:
                if c.start >= current_seg["end"]:
                    seg_lines.append(f"[{current_seg['segment_type']} end]")
                else:
                    seg_lines.append(f"[{current_seg['segment_type']} continues]")
            if seg_at_time:
                seg_lines.append(f"[{seg_at_time['segment_type']} start]")
            current_seg = seg_at_time

        text = c.text.strip()
        tag = ""
        if prev_idx == i:
            tag = "[Previous B-Roll End] "
        elif curr_idx == i:
            tag = "[Current Position] "
        if angle_changes:
            for ac in angle_changes:
                if abs(ac["t"] - c.start) < 1.0:
                    tag += f"[Angle {ac['angle']}] "

        line = f"[{c.start:.2f}s] {tag}{text}"
        lines.append(line)

    if current_seg and end_time < current_seg["end"]:
        lines.append(f"[{current_seg['segment_type']} continues]")

    full_lines = []
    caption_ptr = 0
    for sline in seg_lines:
        while caption_ptr < len(lines) and parse_timestamp_str(lines[caption_ptr].split(']')[0]) < current_time:
            full_lines.append(lines[caption_ptr])
            caption_ptr += 1
        full_lines.append(sline)
    full_lines.extend(lines[caption_ptr:])
    return "\n".join(full_lines)

# --- Other Utilities ---
def safe_json_load(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception: return None

def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)

def has_nonempty_plan(dir_path: Path) -> bool:
    if not dir_path.exists():
        return False
    for plan_path in dir_path.glob('*_broll_plan.json'):
        try:
            data = json.loads(plan_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        plan_items = data.get('b_roll_plan') or data.get('b-roll_plan')
        if isinstance(plan_items, list) and plan_items:
            return True
    return False

# --------------------------------
# Gemini Client
# --------------------------------
class GeminiClient:
    def __init__(self, project_id: str, location: str, model_name: str, response_schema_data: Dict[str, Any], seed: int = 42):
        self.available = _GENAI_AVAILABLE and bool(project_id) and bool(model_name)
        self.model_name = model_name
        self.seed = seed
        if self.available:
            self.client = genai.Client(vertexai=True, project=project_id, location=location)
            self.safety_settings = [
                types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
            ]
        # MODIFIED: Load schema from constructor argument instead of hard-coding
        self.response_schema = response_schema_data

    def send_chat_message(self, history: List, system_instruction: str, temperature: float = 0.5) -> Optional[types.GenerateContentResponse]:
        if not self.available:
            return None
        try:
            config = types.GenerateContentConfig(
                temperature=temperature,
                top_p=0.95,
                seed=self.seed,
                max_output_tokens=63000,
                safety_settings=self.safety_settings,
                thinking_config=types.ThinkingConfig(thinking_budget=32000),
                system_instruction=[types.Part.from_text(text=system_instruction)],
                response_mime_type="application/json",
                response_schema=self.response_schema,
            )
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=history,
                config=config,
            )
            return response
        except Exception as e:
            logger.error(f"Gemini API Error: {e}")
            with debug_response_path.open('a', encoding='utf-8') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Error: {str(e)}\n")
            return None

# --------------------------------
# Core B-Roll Loop
# --------------------------------
def run_broll_over_captions(
    captions: List[Caption],
    segments: List[Dict[str, Any]],
    gemini: GeminiClient,
    project_name: str,
    angle_changes: List[Dict[str, Any]],
    system_prompt: str,
    log_fn=print,
) -> List[Dict[str, Any]]:
    if not captions:
        return []

    total_end = stream_total_end(captions)
    current_time = max(captions[0].start, 3600.0) if captions else 3600.0
    last_broll_end = None
    all_suggestions: List[Dict[str, Any]] = []
    
    # Track generated descriptions for context
    generated_prompts_history: List[str] = [] 
    
    chat_history: List = []
    skipped_last = False

    base_system_prompt = system_prompt

    while current_time < total_end:
        curr_seg = find_current_segment(segments, current_time)
        if curr_seg and curr_seg["segment_type"] in SKIP_SEGMENT_TYPES:
            current_time = curr_seg["end"]
            logger.debug(f"Skipped segment {curr_seg['segment_type']} at {current_time:.2f}s")
            continue

        if total_end - current_time < MIN_BROLL_AFTER:
            logger.debug(f"Reached end at {current_time:.2f}s; too short for B-roll")
            break

        time_since_last = current_time - (last_broll_end or 0.0)
        look_ahead = max(MIN_LOOKAHEAD_SEC, ANALYSIS_WINDOW_SEC - time_since_last)
        transcript_chunk = gather_window_with_segments(
            captions, segments, current_time, look_ahead, last_broll_end, angle_changes
        )
        
        if not transcript_chunk.strip():
            next_idx = _first_caption_index_at_or_after(captions, current_time)
            if next_idx is None:
                logger.debug(f"Empty window at {current_time:.2f}s: No more captions; ending")
                break
            next_time = captions[next_idx].start
            next_seg = find_current_segment(segments, next_time)
            if next_seg and next_seg["segment_type"] in SKIP_SEGMENT_TYPES:
                next_time = next_seg["end"]
            logger.debug(f"Empty window at {current_time:.2f}s: Jumping to {next_time:.2f}s")
            current_time = next_time
            continue

        special = "You skipped last—prioritize a good spot. " if skipped_last else ""
        
        # [UPDATED] Robust Context Injection with "Welder Rule" Framing
        history_context = ""
        if generated_prompts_history:
            # Format as a bulleted list
            formatted_hist = "\n".join([f"- {p}" for p in generated_prompts_history])
            history_context = (
                "\n\n"
                "### VISUAL MEMORY (ANTI-REPETITION LOG)\n"
                "**INSTRUCTION:** The following prompts have already been generated for this video.\n"
                "1. **Avoid Visual Redundancy:** If a specific metaphor (e.g., 'Welder') appears below, YOU MUST CHOOSE A DIFFERENT ONE (e.g., 'Carpenter') for the current segment.\n"
                "2. **Ensure Diversity:** Review the demographics and settings below. If you have heavily favored one group, pivot to another.\n\n"
                "[START LOG]\n"
                f"{formatted_hist}\n"
                "[END LOG]\n"
                "**Apply these constraints to your next suggestion.**\n"
            )

        if all_suggestions:
            user_prompt = (
                f"Last B-roll ended at {last_broll_end:.2f}s. Current: {current_time:.2f}s. "
                f"Propose next (solo or short sequence) with 5-caption gap.\n"
                f"{history_context}\n"
                f"{special}\n\n"
                f"Tagged Transcript:\n{transcript_chunk}"
            )
        else:
            user_prompt = (
                f"Start at {current_time:.2f}s. Scan for first B-roll opportunity.\n"
                f"{special}\n\n"
                f"Tagged Transcript:\n{transcript_chunk}"
            )

        logger.info("-" * 20 + f" PROMPT @ {current_time:.2f}s " + "-" * 20)
        # Log preview only to keep logs clean, but full prompt is sent
        logger.info("SYSTEM:\n" + base_system_prompt + "\n\nUSER (Preview):\n" + user_prompt[:PROMPT_PREVIEW_CHARS] + "...")
        logger.info("     [AI CALL] ...")

        chat_history.append(types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)]))
        response = gemini.send_chat_message(chat_history, base_system_prompt)
        logger.info(" done.")

        if not response or not response.text:
            logger.info("     -> Null response. Recovering.")
            with debug_response_path.open('a', encoding='utf-8') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Null response @ {current_time:.2f}s\n")
            chat_history.pop()
            skipped_last = True
            current_time += 15.0
            continue

        if response.usage_metadata:
            log_token_usage(
                project_name=project_name,
                script_name=Path(__file__).name,
                model_name=gemini.model_name,
                prompt_token_count=response.usage_metadata.prompt_token_count,
                candidates_token_count=response.usage_metadata.candidates_token_count,
                total_token_count=response.usage_metadata.total_token_count
            )

        try:
            resp = json.loads(response.text)
            with debug_response_path.open('a', encoding='utf-8') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Response @ {current_time:.2f}s:\n{json.dumps(resp, indent=2)}\n\n")
            logger.info("     RawResp: " + str(resp)[:400])
        except json.JSONDecodeError as e:
            logger.error(f"     -> Invalid JSON response: {e}")
            with debug_response_path.open('a', encoding='utf-8') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Invalid JSON @ {current_time:.2f}s:\n{response.text}\n\n")
            chat_history.pop()
            skipped_last = True
            current_time += 15.0
            continue

        model_resp_text = json.dumps(resp)
        chat_history.append(types.Content(role="model", parts=[types.Part.from_text(text=model_resp_text)]))

        if not resp or not isinstance(resp.get("b_roll_suggestions"), list):
            logger.info("     -> No suggestions. Skipping.")
            with debug_response_path.open('a', encoding='utf-8') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] No suggestions @ {current_time:.2f}s:\n{json.dumps(resp, indent=2)}\n\n")
            skipped_last = True
            current_time += 15.0
            continue

        suggestions = []
        for s in resp["b_roll_suggestions"]:
            try:
                seq_len = s.get("sequence_length", "1")
                if isinstance(seq_len, (int, float)):
                    seq_len = str(int(float(seq_len)))
                if seq_len not in ["1", "2", "3", "4", "5"]:
                    logger.warning(f"Invalid sequence_length {seq_len}; defaulting to '1'")
                    seq_len = "1"
                s["sequence_length"] = seq_len 
                suggestions.append(s)
            except Exception as e:
                logger.warning(f"Invalid suggestion: {e}; skipping")
                continue

        valid_sugs = [s for s in suggestions if s.get("duration_seconds", 0) >= MIN_BROLL_DURATION]
        if not valid_sugs:
            logger.info("    -> Invalid durations. Skipping.")
            current_time += 15.0
            continue

        # Append accepted prompts to history for the next loop
        for s in valid_sugs:
            if "video_prompt" in s:
                generated_prompts_history.append(s["video_prompt"])

        new_end = max(s["timestamp_start"] + s["duration_seconds"] for s in valid_sugs)
        gap_start_idx = _first_caption_index_at_or_after(captions, new_end)
        if gap_start_idx is not None:
            target_gap_idx = gap_start_idx + MIN_GAP_CAPTIONS
            if target_gap_idx < len(captions):
                enforced_time = captions[target_gap_idx].start
                current_time = max(current_time, enforced_time)
            else:
                current_time = total_end
        else:
            current_time = new_end

        last_broll_end = new_end
        all_suggestions.extend(valid_sugs)
        skipped_last = False
        logger.info(f"     -> Added {len(valid_sugs)} suggestions, ending at {new_end:.2f}s.")

    return all_suggestions

# --------------------------------
# Main
# --------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate SRT-based B-roll suggestions with windows.")
    add_base_arg(parser)
    parser.add_argument("--project_id", type=str, default="qtow-148623")
    parser.add_argument("--location", type=str, default="us-central1")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output_dir", type=str, default=None)
    parser.add_argument("--suggest_temp", type=float, default=1.0)
    parser.add_argument("--srt", type=str, default=None)
    args = parser.parse_args()

    p = from_args(args)
    script_name = Path(__file__).stem
    success_flag = p.workspace / f"{script_name}.success"
    fail_flag = p.workspace / f"{script_name}.fail"

    output_dir = Path(args.output_dir) if args.output_dir else Path(p.b_roll_dir)

    if success_flag.exists():
        if has_nonempty_plan(output_dir):
            logger.info(f"[+] Script {script_name} has already run successfully. Skipping.")
            return
        logger.info(f"[!] Previous success but no plan. Re-running.")
        success_flag.unlink()

    if fail_flag.exists():
        logger.info(f"[i] Retrying {script_name}.")
        fail_flag.unlink()

    try:
        # --- NEW: Load external config and schema ---
        if not _YAML_AVAILABLE:
            logger.error("[!] PyYAML package is not installed. Cannot load config. `pip install PyYAML`")
            raise ImportError("PyYAML not found")

        config_path = script_dir / "46_config.yaml"
        schema_path = script_dir / "46_response_schema.json"

        if not config_path.exists():
            logger.error(f"[!] Config file not found: {config_path}")
            raise FileNotFoundError(f"Missing config file: {config_path.name}")
        if not schema_path.exists():
            logger.error(f"[!] Schema file not found: {schema_path}")
            raise FileNotFoundError(f"Missing schema file: {schema_path.name}")

        try:
            config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            system_prompt = config_data['prompts']['system']
        except Exception as e:
            logger.error(f"Error parsing 46_config.yaml: {e}")
            raise

        try:
            schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error parsing 46_response_schema.json: {e}")
            raise
        
        logger.info("[+] Loaded system prompt from 46_config.yaml")
        logger.info("[+] Loaded response schema from 46_response_schema.json")
        # --- END NEW ---

        working_root = Path(p.working_root)
        base = Path(p.base)
        plans_dir = Path(p.plans_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        if args.srt:
            srt_path = Path(args.srt)
            if not srt_path.is_absolute():
                if (working_root / srt_path).exists():
                    srt_path = (working_root / srt_path).resolve()
                elif (Path.cwd() / srt_path).exists():
                    srt_path = (Path.cwd() / srt_path).resolve()
        else:
            srt_path, tried = discover_srt(working_root=working_root, base=base, info_fcpxml=p.info_fcpxml)
            if not srt_path:
                logger.info("[!] SRT not found. Searched:")
                for cand in sorted(set(tried)):
                    logger.info(f"      - {cand}")
                raise FileNotFoundError("SRT not located")

        captions = parse_srt(srt_path)
        prod41 = load_production_order(p)
        segments = derive_segments_from_41(captions, prod41)
        pacing_file = find_pacing_plan_file(p, srt_path)
        angle_changes = load_angle_changes(pacing_file)
        
        # MODIFIED: Pass the loaded schema_data to the client
        gemini_client = GeminiClient(
            args.project_id, 
            args.location, 
            args.model, 
            schema_data,  # <-- Pass loaded schema
            args.seed
        )

        logger.info(f"[+] SRT: {srt_path}")
        logger.info(f"[+] Segments: {len(segments)}")
        logger.info(f"[+] Angle Plan: {pacing_file.name if pacing_file else 'None'}")

        # MODIFIED: Pass the loaded system_prompt to the function
        suggestions = run_broll_over_captions(
            captions, 
            segments, 
            gemini_client, 
            get_active_project_name(), 
            angle_changes,
            system_prompt  # <-- Pass loaded prompt
        )

        if not suggestions:
            logger.info("[!] No suggestions generated.")
            fail_flag.touch()
            sys.exit(1) 

        suggestions.sort(key=lambda x: x.get("timestamp_start", 0.0))

        out_json = {
            "source_srt": str(srt_path),
            "b_roll_plan": suggestions,
            "generated_at": time.time(),
        }
        base_stem = re.sub(r"\s+", "_", srt_path.stem.strip().lower())
        out_path = output_dir / f"{base_stem}_broll_plan.json"
        atomic_write_json(str(out_path), out_json)
        logger.info(f"[+] Saved {len(suggestions)} suggestions to {out_path}")
        success_flag.touch()

    except Exception as e:
        logger.error(f"[!] Error: {e}")
        fail_flag.touch()
        raise

if __name__ == "__main__":
    main()