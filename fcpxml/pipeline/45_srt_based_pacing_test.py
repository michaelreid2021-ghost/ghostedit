#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 45_subtitle_based_pacing_test.py
# Version: 4.0.5
# Last Modified: 2025-11-23

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from google.api_core import exceptions as google_exceptions
from project_paths import add_base_arg, from_args, ProjectPaths, get_active_project_name
from token_logger import log_token_usage
import logging
import yaml
import time

# --- FORCE UTF-8 OUTPUT (Windows Fix) ---
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from gemini_client import GeminiClient

# Setup logging
script_dir = Path(__file__).resolve().parent
log_file_path = script_dir / "45_srt_based_pacing_test.log"
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    logger.addHandler(stream_handler)

# -------------------------------
# SRT parsing
# -------------------------------
@dataclass
class Caption:
    index: int
    start: float
    end: float
    text: str

def srt_time_to_seconds(t: str) -> float:
    m = re.match(r"^\s*(\d{2}):(\d{2}):(\d{2}),(\d{1,3})\s*$", t)
    if not m:
        raise ValueError(f"Invalid SRT time '{t}'")
    hh, mm, ss, ms = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return hh * 3600 + mm * 60 + ss + ms / 1000.0

def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"</?[^>]+>", "", s)
    return s

def parse_srt(path: Path) -> List[Caption]:
    if not path.exists():
        raise FileNotFoundError(f"SRT not found: {path}")
    content = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", content.strip(), flags=re.MULTILINE)
    out: List[Caption] = []
    idx_auto = 1
    for block in blocks:
        lines = [ln.rstrip("\r") for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        idx = None
        try:
            idx = int(lines[0].strip())
            lines = lines[1:]
        except Exception:
            idx = idx_auto
        idx_auto += 1
        tm_line = None
        for ln in lines:
            if "-->" in ln:
                tm_line = ln
                break
        if not tm_line:
            continue
        m = re.match(r"^\s*([0-9:,\s]+)\s*-->\s*([0-9:,\s]+)", tm_line)
        if not m:
            continue
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

# -------------------------------
# Caption helpers
# -------------------------------
def normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s']", "", s)
    return s.strip()

def stream_total_end(caps: List[Caption]) -> float:
    return max((c.end for c in caps), default=0.0)

def find_phrase_time(caps: List[Caption], phrase: str) -> Optional[float]:
    if not phrase:
        return None
    target = normalize_text(phrase)
    if not target:
        return None
    flat_parts = []
    mapping = []
    cursor = 0
    for c in caps:
        t = normalize_text(c.text)
        mapping.append((cursor, len(t), c.start, c.end))
        flat_parts.append(t)
        cursor += len(t) + 1
    flat = " ".join(flat_parts)
    idx = flat.find(target)
    if idx == -1:
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
def _consolidate_transcript_lines(input_lines: List[str]) -> List[str]:
    """Joins fragmented caption lines into complete sentences."""
    if not input_lines:
        return []

    consolidated_lines = [input_lines[0]]
    PUNCTUATION = ('.', '?', '!')

    for line in input_lines[1:]:
        # Get the text part of the PREVIOUS consolidated line
        last_line = consolidated_lines[-1]
        try:
            # Find the last ']' to reliably isolate the text content
            text_start_index = last_line.rfind(']') + 1
            previous_text = last_line[text_start_index:].strip()
        except ValueError: # rfind returns -1 if not found
            previous_text = last_line.strip()

        # If the previous line is an incomplete sentence...
        if previous_text and not previous_text.endswith(PUNCTUATION):
            # ...get the text from the CURRENT line...
            try:
                text_start_index = line.rfind(']') + 1
                current_text = line[text_start_index:].strip()
            except ValueError:
                current_text = line.strip()
            
            # ...and append it.
            consolidated_lines[-1] += " " + current_text
        else:
            # Otherwise, this line starts a new sentence.
            consolidated_lines.append(line)
    
    return consolidated_lines


def gather_window_lines(captions: List[Caption], start_time: float, duration: float, current_angle: str) -> str:
    lines: List[str] = []
    end_time = start_time + duration
    marker_added = False
    for c in captions:
        if start_time <= c.start < end_time:
            text = c.text.strip()
            if not marker_added:
                line = f"[{c.start:.2f}s][current position {current_angle}] {text}"
                marker_added = True
            else:
                line = f"[{c.start:.2f}s] {text}"
            lines.append(line)
   # Process the lines before returning
    processed_lines = _consolidate_transcript_lines(lines)
    return "\n".join(processed_lines)

def gather_window_lines_with_context(
    captions: List[Caption],
    context_start: float,
    current_time: float,
    duration: float,
    previous_label_time: Optional[float],
    current_angle: str,
    rejections: Optional[List[Dict]] = None,  # <--- NEW ARGUMENT
) -> str:
    lines: List[str] = []
    end_time = current_time + duration
    start_idx = _first_caption_index_at_or_after(captions, max(0.0, context_start))
    if start_idx is None:
        return ""
    
    prev_idx = _first_caption_index_at_or_after(captions, previous_label_time) if previous_label_time is not None else None
    curr_idx = _first_caption_index_at_or_after(captions, current_time)
    
    # Sort local rejections for this window
    local_rejections = sorted(rejections, key=lambda x: x['time']) if rejections else []

    for i in range(start_idx, len(captions)):
        c = captions[i]
        if c.start >= end_time:
            break
        if c.start < context_start:
            continue
        
        # --- NEW: Inject Rejection Tags ---
        # Insert tag if a rejection happened roughly before this caption
        while local_rejections and local_rejections[0]['time'] <= (c.start + 0.5):
            r = local_rejections.pop(0)
            if r['time'] >= context_start:
                lines.append(f"[{r['time']:.2f}s] [🚫 PREVIOUSLY REJECTED CUT] (Reason: {r['reason']})")
        # ----------------------------------

        text = c.text.strip()
        tag = ""
        if curr_idx is not None and i == curr_idx:
            tag = f"[Current Angle {current_angle}] "
        elif prev_idx is not None and i == prev_idx:
            tag = "[Previous Angle Change] "
        line = f"[{c.start:.2f}s] {tag}{text}"
        lines.append(line)

    processed_lines = _consolidate_transcript_lines(lines)
    return "\n".join(processed_lines)
# -------------------------------
# Core pacing loop
# -------------------------------
def run_pacing_over_captions(
    captions: List[Caption],
    gemini: GeminiClient,
    project_name: str,
    segment_type: str,
    segment_start: float,
    segment_end: float,
    config: dict,
    log_fn=print,
) -> List[Dict[str, Any]]:
    # --- 1. SETUP ---
    if not captions or segment_start >= segment_end:
        return []
    
    # Check skipping rules
    if segment_type in config['script']['skip_segment_types']:
        log_fn(f"     -> Segment policy: skipping '{segment_type}' (listed in skip_segment_types).")
        return []

    if not config['script']['allow_cuts'].get(segment_type, True):
        log_fn(f"     -> Segment policy: no cuts for '{segment_type}'.")
        return []

    # [UPDATED LOGIC] Dynamic Style Selection
    style = config['script'].get('pacing_style', 'default')
    
    # Map style names to their config keys
    if style == 'academic':
        pacing_rules_key = 'academic_pacing_rules'
        final_notes_key = 'academic_final_notes'
    elif style == 'high_energy':
        pacing_rules_key = 'high_energy_pacing_rules'
        final_notes_key = 'high_energy_final_notes'
    else:
        pacing_rules_key = 'pacing_rules'
        final_notes_key = 'final_notes'

    # Graceful fallback if the specific key is missing in YAML
    if pacing_rules_key not in config:
        log_fn(f"     [!] WARNING: Style '{style}' requested but '{pacing_rules_key}' not found in YAML. Using default.")
        pacing_rules_key = 'pacing_rules'
        final_notes_key = 'final_notes'

    pacing_guidance = config[pacing_rules_key].get(segment_type, config[pacing_rules_key]['Default'])
    
    base_system_prompt = config['prompts']['base_system']
    system_prompt = f"{base_system_prompt}\n\nPACING GUIDANCE FOR the '{segment_type}' SEGMENT: {pacing_guidance}"
    
    # Only inject specific question handling if the segment warrants it or if high energy is on
    if segment_type in {"Problem", "Reframe", "Agitation", "Proof"} or style == 'high_energy':
         # Use .get() to avoid crashing if the notes key is missing
        notes = config.get(final_notes_key, config['final_notes'])
        system_prompt += f"\n\nADDITIONAL RULES FOR HANDLING QUESTIONS: {json.dumps(notes, indent=None)}"

    current_time = max(segment_start, 0.0)
    current_angle = "Angle A"
    changes: List[Dict[str, Any]] = []
    chat_history: List = []
    skipped_last_turn = False
    max_retries = config['script']['max_retries']

    # --- NEW: Rejection Memory ---
    # Key: The 'start_time' of the analysis window where the decision was made
    # Value: List of dicts { 'time': float, 'reason': str }
    rejection_memory: Dict[float, List[Dict]] = {} 

    # --- MAIN PROCESSING LOOP ---
    while current_time < segment_end:
        if segment_end - current_time < config['script']['min_cut_after']:
            break

        # --- 2. CONTEXT & PROMPT BUILDING ---
        if not changes:  # This is the first cut in the segment
            transcript_chunk = gather_window_lines(
                captions=captions,
                start_time=current_time,
                duration=config['script']['analysis_window_sec'],
                current_angle=current_angle,
            )
        else:  # This is a subsequent cut
            last_cut = changes[-1]
            prev_context_start = float(last_cut.get("previous_time", current_time))
            time_since_last_cut = current_time - prev_context_start
            look_ahead_duration = max(config['script']['min_lookahead_sec'], config['script']['analysis_window_sec'] - time_since_last_cut)
            
            # Retrieve relevant rejections for this specific window
            relevant_rejections = rejection_memory.get(prev_context_start, [])

            transcript_chunk = gather_window_lines_with_context(
                captions=captions,
                context_start=prev_context_start,
                current_time=current_time,
                duration=look_ahead_duration,
                previous_label_time=prev_context_start,
                current_angle=current_angle,
                rejections=relevant_rejections # <--- PASS REJECTIONS
            )

        if not transcript_chunk.strip():
            logger.info(f"     -> Empty window at {current_time:.2f}s; advancing.")
            current_time += 5.0
            continue

        next_angle = "Angle B" if current_angle == "Angle A" else "Angle A"
        special_instruction = "You chose to skip the previous cut. It is now more important to find a suitable place for an angle change. " if skipped_last_turn else ""
        
        if not changes:
            user_prompt = config['prompts']['user_first'].replace("{{current_time}}", f"{current_time:.2f}").replace("{{current_angle}}", current_angle).replace("{{next_angle}}", next_angle).replace("{{special_instruction}}", special_instruction).replace("{{transcript_chunk}}", transcript_chunk)
        else:
            last_cut = changes[-1]
            user_prompt = config['prompts']['user_subsequent'].replace("{{last_cut_time}}", f"{last_cut['absolute_cut_time']:.2f}").replace("{{current_time}}", f"{current_time:.2f}").replace("{{next_angle}}", next_angle).replace("{{special_instruction}}", special_instruction).replace("{{transcript_chunk}}", transcript_chunk)

        # --- 3. AI INTERACTION WITH RETRY LOGIC ---
        try:
            logger.info("----- ANALYZING '%s' @ %.2fs -----", segment_type, current_time)
            if special_instruction:
                logger.info("   [!] Special Instruction: %s", special_instruction.strip())
            
            # DIRECTLY LOG THE TRANSCRIPT CHUNK
            logger.info("   [>] TRANSCRIPT WINDOW:\n%s", transcript_chunk) 
            
            logger.info("     [AI CALL] ...")
        except Exception as log_error:
            logger.error("     -> Logging error during prompt display: %s", log_error)

        chat_history.append({"role": "user", "parts": [{"text": user_prompt}]})
        response = None
        retry_count = 0
        while retry_count <= max_retries:
            try:
                response = gemini.send_chat_message(
                    history=chat_history,
                    system_instruction=system_prompt,
                    response_schema=config['response_schema'],
                    temperature=config['script']['model']['temperature'],
                    top_p=config['script']['model']['top_p'],
                    seed=config['script']['model']['seed'],
                    max_output_tokens=config['script']['model']['max_output_tokens'],
                    thinking_budget=config['script']['model']['thinking_budget']
                )
                logger.info(" done.")
                break
            except Exception as e:
                retry_count += 1
                if retry_count > max_retries:
                    logger.error("     -> Max retries (%d) exceeded for API call: %s", max_retries, e)
                    raise RuntimeError(f"Max retries exceeded for API call: {e}.")
                logger.warning("     -> API error (attempt %d/%d): %s. Retrying in 2 seconds...", retry_count, max_retries, e)
                time.sleep(2)
        
        if not response or not response.text:
            raise RuntimeError("Received null/invalid response from API after retries.")

        # --- 4. RESPONSE PROCESSING & RIGOROUS VALIDATION ---
        try:
            resp = json.loads(response.text)
            required_fields = ["propose_cut", "reason", "ignore_previous_angle_change"]
            if not isinstance(resp, dict) or not all(field in resp for field in required_fields):
                raise ValueError("Invalid JSON response structure: missing required fields.")
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("     -> FATAL: Invalid JSON response: %s", e)
            raise RuntimeError(f"Invalid JSON response: {e}")

        try:
            resp_str = json.dumps(resp, indent=2)
            logger.info("     RawResp: %s", resp_str.replace("\n", "\n              "))
        except Exception as log_error:
            logger.error("     -> Logging error for response: %s", log_error)

        if response.usage_metadata:
            log_token_usage(project_name=project_name, script_name=Path(__file__).name, model_name=config['script']['model']['model_name'], prompt_token_count=response.usage_metadata.prompt_token_count, candidates_token_count=response.usage_metadata.candidates_token_count, total_token_count=response.usage_metadata.total_token_count)

        model_response_text = json.dumps(resp)
        chat_history.append({"role": "model", "parts": [{"text": model_response_text}]})

        # --- HANDLE ROLLBACK WITH MEMORY ---
        if resp.get("ignore_previous_angle_change") and changes:
            reason = resp.get("reason", "N/A")
            logger.info("     -> AI requested to ignore previous cut. Rewinding. Reason: %s", reason)
            
            # 1. Identify the cut we are undoing
            bad_cut = changes.pop()
            
            # 2. Identify where we are going back TO
            rewind_time = float(bad_cut["previous_time"])
            bad_cut_absolute_time = float(bad_cut["absolute_cut_time"])
            
            # 3. Record the failure so the AI sees it next time
            if rewind_time not in rejection_memory:
                rejection_memory[rewind_time] = []
            
            # We record the time the AI *tried* to cut at (bad_cut['absolute_cut_time'])
            # and the reason it gave for rolling back.
            rejection_memory[rewind_time].append({
                "time": bad_cut_absolute_time,
                "reason": reason
            })

            # 4. Reset state
            current_time = rewind_time
            current_angle = bad_cut["angle_choice"]
            
            # 5. Clear the chat history so we don't confuse the context, 
            # BUT the 'transcript_chunk' in the next loop will now contain the [REJECTED] tags.
            if len(chat_history) >= 2:
                chat_history.pop(); chat_history.pop()
            
            continue

        if not resp.get("propose_cut"):
            logger.info("     -> AI chose to skip. Reason: %s", resp.get("reason", "N/A"))
            skipped_last_turn = True
            current_time += 20.0
            continue
        
        # --- HEURISTIC CORRECTION & VALIDATION BLOCK ---
        try:
            cut_duration = float(resp.get("cut_after_seconds"))
        except (ValueError, TypeError):
            logger.error("     -> FATAL: AI proposed a non-numeric cut duration ('%s').", resp.get("cut_after_seconds", "N/A"))
            raise RuntimeError("Invalid 'cut_after_seconds' value from AI.")

        # If the AI returns a value larger than the analysis window, it's likely an absolute timestamp. Correct it.
        if cut_duration > config['script']['analysis_window_sec'] and cut_duration > current_time:
            corrected_duration = cut_duration - current_time
            logger.warning(
                "     -> AI likely returned an absolute timestamp (%.2f). Correcting to a relative duration of %.2fs.",
                cut_duration, corrected_duration
            )
            cut_duration = corrected_duration

        # --- POST-CORRECTION VALIDATION ---
        min_cut_allowed = config['script']['min_cut_after']
        segment_tolerance = config['script'].get('segment_end_tolerance_sec', 5.0) # Default to 5s if not in config
        new_absolute_time = current_time + cut_duration

        # VALIDATION 1: Is the cut too far outside the segment?
        if new_absolute_time > (segment_end + segment_tolerance):
            logger.warning(
                "     -> Cut at %.2fs is outside the segment (end %.2fs). Treating as segment completion.",
                new_absolute_time, segment_end
            )
            break 
        
        # VALIDATION 2: Is the cut too short?
        if cut_duration < min_cut_allowed:
            # If the AI proposes a cut that is too short, it's often because it's at a
            # logical endpoint (like "Thanks for listening.") but the remaining time
            # is less than the minimum. Instead of erroring, we'll just stop here.
            logger.warning(
                "     -> Proposed duration (%.2fs) is below the minimum of %.2fs. "
                "Assuming this is the end of the segment and stopping.",
                cut_duration, min_cut_allowed
            )
            break # Finalize as success
        
        # VALIDATION 3: Is the duration plausible? (It must be positive)
        if cut_duration <= 0:
            logger.error("     -> FATAL: Proposed or corrected duration (%.2f) must be a positive number.", cut_duration)
            raise RuntimeError("Proposed or corrected duration is invalid.")

        # --- 5. STATE UPDATE (ACCEPT AND RECORD CUT) ---
        skipped_last_turn = False
        logger.info("     -> AI decision: Cut to %s at absolute time %.2fs.", next_angle, new_absolute_time)
        record = {
            "propose_cut": True,
            "cut_after_seconds": cut_duration,
            "absolute_cut_time": new_absolute_time,
            "previous_time": current_time,
            "angle_choice": current_angle,
            "reason": resp.get("reason", ""),
            "ignore_previous_angle_change": False,
        }
        changes.append(record)
        current_time = new_absolute_time
        current_angle = next_angle

    return changes

# -------------------------------
# SRT discovery
# -------------------------------
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
    for d in list(peer_dirs):
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

# -------------------------------
# Segments (41_production_order.json)
# -------------------------------
def load_production_order(p: ProjectPaths) -> Optional[Dict[str, Any]]:
    path = p.working_root / "41_production_order.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def derive_segments_from_41(captions: List[Caption], prod41: Dict[str, Any], config: dict) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    plan = (prod41 or {}).get("project_plan") or []
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
        
        # [MODIFIED] REMOVED 'SKIP' LOGIC HERE
        # We must gather ALL segments (even Disclaimers) so that we can calculate
        # the start/end times for the valid segments correctly.
        
        # 1. DEFACTO TRUTH: Check 'start_seconds' first
        t = None
        json_start = seg.get("start_seconds")
        if json_start is not None:
            try:
                t = float(json_start)
            except (ValueError, TypeError):
                t = None

        # 2. FALLBACK: Only if start_seconds is missing/invalid do we search text
        if t is None and isinstance(ep, str) and ep.strip():
            t = find_phrase_time(captions, ep)

        anchors.append((st, ep, t))

    first_cap_start = captions[0].start if captions else 0.0
    usable: List[Tuple[str, Optional[str], float]] = []
    
    for i, (st, ep, t) in enumerate(anchors):
        if t is not None:
            usable.append((st, ep, float(t)))
        else:
            if i == 0 and captions:
                usable.append((st, ep, first_cap_start))

    if not usable and captions:
        out.append({"segment_type": "Default", "entry_point_phrase": "", "start": captions[0].start, "end": stream_total_end(captions)})
        return out

    usable.sort(key=lambda x: x[2])
    total_end = stream_total_end(captions)
    
    for i, (st, ep, t) in enumerate(usable):
        seg_start = t
        # End is either next segment's start OR total end
        seg_end = usable[i + 1][2] if i + 1 < len(usable) else total_end
        
        if seg_end - seg_start < 1.0:
            continue
        out.append({"segment_type": st, "entry_point_phrase": ep or "", "start": seg_start, "end": seg_end})
    return out

# -------------------------------
# Main
# -------------------------------
def main():
    parser = argparse.ArgumentParser(description="Guided AI pacing over timeline SRT (pipeline-aware).", allow_abbrev=False)
    add_base_arg(parser)
    parser.add_argument("--srt", type=str, default=None, help="Optional explicit SRT path; otherwise auto-discovered.")
    args = parser.parse_args()
    p = from_args(args)
    working_root = Path(p.working_root)
    base = Path(p.base)
    # Load YAML config
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "45_config.yaml"
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    script_config = config['script']
    script_name = script_config['name']
    success_flag = p.workspace / f"{script_name}.success"
    fail_flag = p.workspace / f"{script_name}.fail"
    if success_flag.exists():
        logger.info(f"[+] Script {script_name} has already run successfully. Skipping.")
        return
    if fail_flag.exists():
        logger.info(f"[i] Retrying script {script_name} after previous failure.")
        fail_flag.unlink()
    try:
        plans_dir = Path(p.plans_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)
        # Load response schema
        schema_path = script_dir / "45_response_schema.json"
        if not schema_path.exists():
            logger.error(f"Schema file not found: {schema_path}")
            raise FileNotFoundError(f"Schema file not found: {schema_path}")
        with open(schema_path, 'r', encoding='utf-8') as f:
            config['response_schema'] = json.load(f)
        # Discover SRT
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
                logger.info("[!] Could not locate a timeline SRT in the active project.")
                logger.info("     Searched the following candidate paths:")
                for cand in sorted(list(set(tried))):
                    logger.info(f"         - {cand}")
                logger.info("\n     Drop your file as 'subtitle 1.srt' in:", working_root)
                raise FileNotFoundError("Could not locate a timeline SRT")
        captions = parse_srt(srt_path)
        prod41 = load_production_order(p)
        segments = derive_segments_from_41(captions, prod41, config)
        gemini_client = GeminiClient(
            project_id=script_config['model']['project_id'],
            location=script_config['model']['location'],
            model_name=script_config['model']['model_name'],
            safety_settings=script_config['model']['safety_settings']
        )
        logger.info(f"[+] Active project base: {p.base}")
        logger.info(f"[+] Working root:       {working_root}")
        logger.info(f"[+] Plans dir:          {plans_dir}")
        logger.info(f"[+] Using subtitles at: {srt_path}")
        logger.info(f"[+] Loaded {len(captions)} caption items.")
        logger.info(f"[+] Planned {len(segments)} segment(s).")
        all_changes: List[Dict[str, Any]] = []
        per_segment_changes: List[Dict[str, Any]] = []
        for i, seg in enumerate(segments, start=1):
            stype = seg["segment_type"]
            s_start = float(seg["start"])
            s_end = float(seg["end"])
            ephrase = seg.get("entry_point_phrase", "")
            logger.info(f"\n -> Segment {i}: '{stype}' | Anchor: {ephrase or '(none)'} | Window: {s_start:.2f}s -> {s_end:.2f}s")
            changes = run_pacing_over_captions(
                captions=captions,
                gemini=gemini_client,
                project_name=get_active_project_name(),
                segment_type=stype,
                segment_start=s_start,
                segment_end=s_end,
                config=config,
                log_fn=print,
            )
            per_segment_changes.append({
                "segment_type": stype,
                "entry_point_phrase": ephrase,
                "start_time": s_start,
                "end_time": s_end,
                "angle_changes": changes,
            })
            all_changes.extend(changes)
        out_json = {
            "source_srt": str(srt_path),
            "segments": per_segment_changes,
            "angle_changes": all_changes,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
        base_stem = re.sub(r"\s+", "_", Path(srt_path).stem.strip().lower())
        out_path = plans_dir / script_config['output_file'].replace("{{srt_stem}}", base_stem)
        out_path.write_text(json.dumps(out_json, indent=2), encoding="utf-8")
        logger.info(f"\n[+] Saved {len(all_changes)} total angle changes -> {out_path}")
        success_flag.touch()
    except Exception as e:
        logger.info(f"[!] An error occurred: {e}")
        fail_flag.touch()
        raise

if __name__ == "__main__":
    main()