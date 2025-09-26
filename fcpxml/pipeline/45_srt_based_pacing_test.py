#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 45_subtitle_based_pacing_test.py
# Version: 4.0.0
# Last Modified: 2025-09-11
#
# Purpose:
#   Guided AI pacing over a timeline-level SRT with a stateful, conversational approach.
#   - Builds on the high-fidelity prompting style of the transcript-based analyzer.
#   - Maintains a chat history, providing the AI with memory of its previous decisions.
#   - Enriches prompts with annotated context like "[Previous Angle Change]" and "[Current Angle X]".
#   - Uses a dynamic system prompt that adapts to the current video segment.
#
# Inputs (auto):
#   - <working_root>/subtitle 1.srt
#   - <working_root>/41_production_order.json
#
# Outputs:
#   - <working_root>/45_ai_pacing_plans/<srt_stem>_changes.json

import argparse
import json
import os
import re
import sys
import hashlib
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from project_paths import add_base_arg, from_args, ProjectPaths, get_active_project_name
from token_logger import log_token_usage

# --- Gemini / Vertex imports (optional) ---
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except Exception:
    _GENAI_AVAILABLE = False

warnings.filterwarnings("ignore", message="Field name .* shadows an attribute in parent.*", category=UserWarning)

# -------------------------------
# Config toggles
# -------------------------------
SKIP_SEGMENT_TYPES = {"Hook"}
ANALYSIS_WINDOW_SEC = 45.0
MIN_LOOKAHEAD_SEC = 25.0 # Ensure at least this much future transcript is visible after a cut
MIN_CUT_AFTER = 3.0
PROMPT_PREVIEW_CHARS = 500

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
    s = re.sub(r"[^\w\s\']", "", s)
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
    return "\n".join(lines)

def gather_window_lines_with_context(
    captions: List[Caption],
    context_start: float,
    current_time: float,
    duration: float,
    previous_label_time: Optional[float],
    current_angle: str,
) -> str:
    lines: List[str] = []
    end_time = current_time + duration

    start_idx = _first_caption_index_at_or_after(captions, max(0.0, context_start))
    if start_idx is None:
        return ""

    prev_idx = _first_caption_index_at_or_after(captions, previous_label_time) if previous_label_time is not None else None
    curr_idx = _first_caption_index_at_or_after(captions, current_time)

    for i in range(start_idx, len(captions)):
        c = captions[i]
        if c.start >= end_time:
            break
        if c.start < context_start:
            continue

        text = c.text.strip()
        tag = ""
        if curr_idx is not None and i == curr_idx:
            tag = f"[Current Angle {current_angle}] "
        elif prev_idx is not None and i == prev_idx:
            tag = "[Previous Angle Change] "

        line = f"[{c.start:.2f}s] {tag}{text}"
        lines.append(line)
    return "\n".join(lines)

# -------------------------------
# Gemini client wrapper
# -------------------------------

class GeminiClient:
    def __init__(self, project_id: str, location: str, model_name: str):
        self.available = _GENAI_AVAILABLE and bool(project_id) and bool(model_name)
        self.model_name = model_name
        if self.available:
            self.client = genai.Client(vertexai=True, project=project_id, location=location)
            self.safety_settings = [
                types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
            ]
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "propose_cut": {"type": "BOOLEAN"},
                "cut_after_seconds": {"type": "NUMBER"},
                "reason": {"type": "STRING"},
                "ignore_previous_angle_change": {"type": "BOOLEAN"},
            },
            "required": ["propose_cut", "reason", "ignore_previous_angle_change"],
        }

    def send_chat_message(self, history: List, system_instruction: str) -> Optional[types.GenerateContentResponse]:
        if not self.available:
            return None
        try:
            config = types.GenerateContentConfig(
                temperature=0.2,
                top_p=0.95,
                seed=42,
                max_output_tokens=4096,
                safety_settings=self.safety_settings,
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
            print(f" [!] Gemini API Error: {e}")
            return None

# -------------------------------
# Pacing rules
# -------------------------------
ACADEMIC_PACING_RULES = {
    "Hook": "For the Hook, establish a strong, authoritative presence. Hold a single, stable shot for the entire duration to let the opening statement land with gravitas.",
    "Introduction": "For the Introduction, use a deliberate and measured pace. Allow shots to last between 12 and 20 seconds to give the audience time to settle in and absorb the speaker's credentials and topic.",
    "Disclaimer": "For the Disclaimer, maintain clarity and trust. Use a single, uninterrupted shot for the duration of the statement. Avoid any camera angle changes.",
    "Problem": "For the Problem segment, allow for a thorough examination of the issue. Hold each shot for 15 to 25 seconds. Cuts should occur at natural pauses after a complex point has been fully explained, letting the weight of the problem sink in.",
    "Reframe": "For the Reframe, the pace should be thoughtful. Make cuts only after a complete, supportive thought has been delivered. Aim for shot durations between 10 and 18 seconds to give concepts proper space.",
    "Value": "For the Value segment, prioritize clarity and comprehension. Hold shots for 12 to 20 seconds to allow for detailed explanations. Avoid fast cuts; instead, use angle changes to signify a transition to a new benefit or key concept.",
    "Proof": "For the Proof segment, present evidence with confidence. Time your cuts to follow the presentation of each piece of evidence, holding each shot for approximately 10 to 15 seconds.",
    "Lesson": "For the Lesson, adopt a clear teaching cadence. Make cuts at the transition between distinct steps or concepts, with shot durations between 10 and 18 seconds to ensure each point is understood before moving to the next.",
    "Story": "For the Story, let the narrative unfold naturally. Make cuts at the end of key sentences or at natural pauses in the story, holding shots for 12 to 20 seconds to allow for audience connection.",
    "Roadmap": "For the Roadmap, outline the upcoming points with clarity. Plan for shot durations of 8 to 12 seconds for each point, giving each item on the agenda its due weight.",
    "Transition": "For the Transition, guide the viewer clearly to the next section. Use moderately paced shots, lasting between 6 and 10 seconds, to create a smooth and logical bridge.",
    "CTA": "For this Call to Action, the pacing should be direct and compelling, not rushed. For the final, conclusive sentence, you MUST use 'Angle A' to deliver the message with authority.",
    "Outro": "For the Outro, be direct and conclusive. For the final sentence or last line, you MUST use 'Angle A' to provide a stable, authoritative closing.",
    "Default": "For the Default pacing, use a thoughtful conversational style. Aim for an average shot duration of 10 to 15 seconds to maintain a natural, unhurried rhythm.",
}

ACADEMIC_FINAL_NOTES = {
    "Purpose": "To define a specific editing pattern for handling questions that reinforces the speaker's authority and gives the audience time to think.",
    "Rules": [
        "1. Anticipate Questions: Scan the upcoming transcript to identify question phrases before you reach them.",
        "2. Cut to Angle B on Question Start: At the beginning of a question, cut to Angle B. This angle change should feel like a deliberate moment for posing a thoughtful query to the audience.",
        "3. Hold on Angle B: Allow a brief, natural pause (1-2 seconds) after the question is asked before cutting back. This gives the question a moment to land.",
        "4. Return to Angle A for the Answer: As the speaker begins to answer, cut back to Angle A to re-establish the primary, authoritative shot for the explanation.",
        "5. Flexibility is Key: The goal is a natural, intellectual cadence, not rigid adherence. If a cut feels jarring or interrupts the speaker's flow, it's better to hold the shot."
    ],
    "Context": "You are performing one step in a larger workflow. Adhering to these specific, event-based rules is more important than maintaining a simple rhythmic pace. Planning ahead is crucial."
}

PACING_RULES = {
    "Hook": "For this Hook, hold a single shot for the entire duration. Avoid making any cuts unless it is absolutely necessary to maintain viewer engagement.",
    "Introduction": "For this Introduction, use a deliberate pace. Let the shots breathe by holding them for around ten to twenty seconds each.",
    "Disclaimer": "For this Disclaimer, use a single, uninterrupted shot. You should not switch camera angles during this segment.",
    "Problem": "For this Problem segment, build a sense of urgency. Avoid making choppy cuts, and instead, space your shots out, holding each for fifteen to thirty-five seconds to let the issue resonate.",
    "Reframe": "For this Reframe, use a moderate pace. Make your cuts after a complete, supportive thought has been delivered, with shot durations between seven and twelve seconds.",
    "Value": "For this Value segment, use a dynamic pace. Mix medium-length shots of eight to eleven seconds for explanations with occasional, faster cuts of four to seven seconds to emphasize key points.",
    "Proof": "For this Proof segment, maintain a moderate pace. Time your cuts to align with evidence beats, holding each shot for approximately eight to twelve seconds.",
    "Lesson": "For this Lesson, establish a teaching cadence. Make your cuts at the boundaries of each step or concept, with shot durations between eight and fourteen seconds.",
    "Story": "For this Story, allow the narrative to land with the audience. Make cuts at the end of sentences or at natural scene beats, holding shots for ten to sixteen seconds.",
    "Roadmap": "For this Roadmap, keep the pacing short and clear. Plan for shot durations of six to ten seconds for each beat or point you are outlining.",
    "Transition": "For this Transition, the pacing should be quick and purposeful. Use shorter shots, lasting between four and eight seconds, to move the viewer smoothly to the next section.",
    "CTA": "For this Call to Action, the pacing should be impactful. For the final sentence, you MUST use 'Angle A' to deliver the message directly.",
    "Outro": "For this Outro, be direct and conclusive. For the final sentence or last line, you MUST use 'Angle A'.",
    "Default": "For the Default pacing, use a standard conversational style. Aim for an average shot duration of seven to nine seconds to maintain a natural rhythm.",
}

FINAL_NOTES = {
    "Purpose": "To define a specific editing pattern for handling questions, using camera angles to add emphasis and guide viewer attention.",
    "Rules": [
        "1. Anticipate Questions: Scan the upcoming transcript to identify question phrases before you reach them.",
        "2. Cut to Angle B on Question Start: At the beginning of the question, your primary action is to cut to Angle B. This treats the question as a point of emphasis, consistent with its role as the 'punctuation' angle.",
        "3. Return to Angle A on Question End: At the natural end of the question (e.g., at the pause before the answer), your primary action is to cut back to Angle A to re-establish the main narrative shot.",
        "4. Be careful about looking for the 'perfect' cut, Nuance and A little flexibility goes a long way"
    ],
    "Context": "You are performing one step in a larger workflow. Adhering to these specific, event-based rules is more important than maintaining a simple rhythmic pace. Planning ahead is crucial."
}

ALLOW_CUTS = {"Disclaimer": False, "Hook": False, "Introduction": False}

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
    log_fn=print,
) -> List[Dict[str, Any]]:
    if not captions or segment_start >= segment_end:
        return []

    if not ALLOW_CUTS.get(segment_type, True):
        log_fn(f"     -> Segment policy: no cuts for '{segment_type}'.")
        return []

    pacing_guidance = PACING_RULES.get(segment_type, PACING_RULES["Default"])
    current_time = max(segment_start, 0.0)
    current_angle = "Angle A"
    changes: List[Dict[str, Any]] = []
    chat_history: List = []
    skipped_last_turn = False

    base_system_prompt = (
        "You are an expert video editor creating a detailed pacing plan for a specific segment of a video."
        " Your task is to analyze transcript segments and suggest camera angle changes to create a dynamic pace."
        " You can also correct a previous error or choose to skip a cut if the pacing isn't right."
        " You are meticulous and keep track of where you are in a segment, ensuring not to repeat cuts."
    )

    while current_time < segment_end:
        if segment_end - current_time < MIN_CUT_AFTER:
            break

        # --- Build System Prompt ---
        system_prompt = f"{base_system_prompt}\n\nPACING GUIDANCE FOR THIS '{segment_type}' SEGMENT: {pacing_guidance}"
        if segment_type in {"Problem", "Reframe", "Agitation", "Proof"}:
            system_prompt += f"\n\nADDITIONAL RULES FOR HANDLING QUESTIONS: {json.dumps(FINAL_NOTES, indent=None)}"

        # --- Build Transcript View ---
        if not changes:
            transcript_chunk = gather_window_lines(
                captions=captions,
                start_time=current_time,
                duration=ANALYSIS_WINDOW_SEC,
                current_angle=current_angle,
            )
        else:
            last_cut = changes[-1]
            prev_context_start = float(last_cut.get("previous_time", current_time))
            time_since_last_cut = current_time - prev_context_start
            look_ahead_duration = max(MIN_LOOKAHEAD_SEC, ANALYSIS_WINDOW_SEC - time_since_last_cut)

            transcript_chunk = gather_window_lines_with_context(
                captions=captions,
                context_start=prev_context_start,
                current_time=current_time,
                duration=look_ahead_duration,
                previous_label_time=prev_context_start,
                current_angle=current_angle,
            )

        if not transcript_chunk.strip():
            print(f"     -> Empty window at {current_time:.2f}s; advancing.")
            current_time += 5.0
            continue

        # --- Build User Prompt ---
        next_angle = "Angle B" if current_angle == "Angle A" else "Angle A"
        special_instruction = ""
        if skipped_last_turn:
            special_instruction += "You chose to skip the previous cut. It is now more important to find a suitable place for an angle change. "
        if (segment_end - current_time) <= ANALYSIS_WINDOW_SEC:
            special_instruction += "This is the final opportunity to make a cut in this segment. Prioritize making a cut now to avoid getting stuck on one angle."

        if not changes:
            user_prompt = (
                f"This is the start of the edit at {current_time:.2f}s on {current_angle}. "
                f"Propose a cut to {next_angle}. If no good cut is found, set 'propose_cut' to false.\n\n"
                f"{special_instruction}\n\n"
                f"Upcoming Transcript:\n{transcript_chunk}"
            )
        else:
            last_cut = changes[-1]
            user_prompt = (
                f"Your last cut was at {last_cut['absolute_cut_time']:.2f}s. Current time is {current_time:.2f}s. "
                f"Propose the next angle change to {next_angle}. "
                f"If no good cut is found, set 'propose_cut' to false. "
                f"You may also ignore the previous cut if it was a pacing error.\n\n"
                f"{special_instruction}\n\n"
                f"Transcript Context:\n{transcript_chunk}"
            )

        # --- Console Debug & AI Call ---
        print("-" * 20 + f" PROMPT FOR '{segment_type}' @ {current_time:.2f}s " + "-" * 20)
        print("SYSTEM PROMPT:\n" + system_prompt + "\n\nUSER PROMPT (Preview):\n" + user_prompt[:PROMPT_PREVIEW_CHARS] + "...")
        print("     [AI CALL] ...", end="", flush=True)

        chat_history.append(types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)]))
        response = gemini.send_chat_message(chat_history, system_prompt)
        print(" done.")

        if not response or not response.text:
            print("     -> Received null/invalid response from API. Advancing time to recover.")
            chat_history.pop()
            skipped_last_turn = True
            current_time += 15.0
            continue

        if response.usage_metadata:
            log_token_usage(
                project_name=get_active_project_name(),
                script_name=Path(__file__).name,
                model_name=gemini.model_name,
                prompt_token_count=response.usage_metadata.prompt_token_count,
                candidates_token_count=response.usage_metadata.candidates_token_count,
                total_token_count=response.usage_metadata.total_token_count
            )

        resp = json.loads(response.text)
        print("     RawResp:", (json.dumps(resp, indent=2) if isinstance(resp, dict) else str(resp))[:400].replace("\n", "\n             "))

        # --- Handle Response ---
        if not resp:
            print("     -> Received null/invalid response from API. Advancing time to recover.")
            chat_history.pop()
            skipped_last_turn = True
            current_time += 15.0
            continue

        model_response_text = json.dumps(resp)
        chat_history.append(types.Content(role="model", parts=[types.Part.from_text(text=model_response_text)]))

        if resp.get("ignore_previous_angle_change") and changes:
            print(f"     -> AI requested to ignore previous cut. Rewinding. Reason: {resp.get('reason', 'N/A')}")
            last_cut = changes.pop()
            current_time = float(last_cut["previous_time"])
            current_angle = last_cut["angle_choice"]
            if len(chat_history) >= 2:
                chat_history.pop(); chat_history.pop()
            continue

        if not resp.get("propose_cut"):
            print(f"     -> AI chose to skip. Reason: {resp.get('reason', 'N/A')}")
            skipped_last_turn = True
            current_time += 20.0
            continue

        try:
            cut_duration = float(resp.get("cut_after_seconds"))
        except (ValueError, TypeError):
            cut_duration = -1.0

        if cut_duration < MIN_CUT_AFTER:
            print(f"     -> AI proposed invalid cut duration ({cut_duration}). Skipping ahead.")
            skipped_last_turn = True
            current_time += 15.0
            continue

        # --- Accept and Record Cut ---
        skipped_last_turn = False
        new_absolute_time = current_time + cut_duration
        
        if new_absolute_time > segment_end:
            print(f"     -> Clamping cut from {new_absolute_time:.2f}s to segment end {segment_end:.2f}s.")
            new_absolute_time = segment_end - 0.1

        record = {
            "propose_cut": True,
            "cut_after_seconds": new_absolute_time - current_time,
            "absolute_cut_time": new_absolute_time,
            "previous_time": current_time,
            "angle_choice": current_angle,
            "reason": resp.get("reason", ""),
            "ignore_previous_angle_change": False,
        }
        changes.append(record)

        current_time = new_absolute_time
        current_angle = next_angle
        print(f"     -> AI decision: Cut to {current_angle} at absolute time {current_time:.2f}s.")

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
        if not r.exists() or r in seen: continue
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
        if not r.exists(): continue
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

def derive_segments_from_41(captions: List[Caption], prod41: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        if st in SKIP_SEGMENT_TYPES:
            continue
        t = find_phrase_time(captions, ep) if isinstance(ep, str) and ep.strip() else None
        anchors.append((st, ep, t))

    first_cap_start = captions[0].start if captions else 0.0
    usable: List[Tuple[str, Optional[str], float]] = []
    for i, (st, ep, t) in enumerate(anchors):
        if t is None:
            if i == 0 and captions:
                usable.append((st, ep, first_cap_start))
        else:
            usable.append((st, ep, float(t)))

    if not usable and captions:
        out.append({"segment_type": "Default", "entry_point_phrase": "", "start": captions[0].start, "end": stream_total_end(captions)})
        return out

    usable.sort(key=lambda x: x[2])
    total_end = stream_total_end(captions)
    for i, (st, ep, t) in enumerate(usable):
        seg_start = t
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
    parser.add_argument("--project_id", type=str, default="qtow-148623")
    parser.add_argument("--location", type=str, default="global")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro")
    parser.add_argument("--srt", type=str, default=None, help="Optional explicit SRT path; otherwise auto-discovered.")
    args = parser.parse_args()

    p = from_args(args)
    script_name = Path(__file__).stem
    success_flag = p.workspace / f"{script_name}.success"
    fail_flag = p.workspace / f"{script_name}.fail"

    if success_flag.exists():
        print(f"[+] Script {script_name} has already run successfully. Skipping.")
        return

    if fail_flag.exists():
        print(f"[i] Retrying script {script_name} after previous failure.")
        fail_flag.unlink()

    try:
        working_root = Path(p.working_root)
        base = Path(p.base)
        plans_dir = Path(p.plans_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)

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
                print("[!] Could not locate a timeline SRT in the active project.")
                print("    Searched the following candidate paths:")
                for cand in sorted(list(set(tried))):
                    print(f"      - {cand}")
                print("\n    Drop your file as 'subtitle 1.srt' in:", working_root)
                raise FileNotFoundError("Could not locate a timeline SRT")

        captions = parse_srt(srt_path)
        prod41 = load_production_order(p)
        segments = derive_segments_from_41(captions, prod41)
        gemini_client = GeminiClient(args.project_id, args.location, args.model)

        print(f"[+] Active project base: {p.base}")
        print(f"[+] Working root:      {working_root}")
        print(f"[+] Plans dir:         {plans_dir}")
        print(f"[+] Using subtitles at: {srt_path}")
        print(f"[+] Loaded {len(captions)} caption items.")
        print(f"[+] Planned {len(segments)} segment(s).")

        all_changes: List[Dict[str, Any]] = []
        per_segment_changes: List[Dict[str, Any]] = []

        for i, seg in enumerate(segments, start=1):
            stype = seg["segment_type"]
            s_start = float(seg["start"])
            s_end = float(seg["end"])
            ephrase = seg.get("entry_point_phrase", "")

            print(f"\n -> Segment {i}: '{stype}' | Anchor: {ephrase or '(none)'} | Window: {s_start:.2f}s -> {s_end:.2f}s")

            changes = run_pacing_over_captions(
                captions=captions,
                gemini=gemini_client,
                project_name=get_active_project_name(),
                segment_type=stype,
                segment_start=s_start,
                segment_end=s_end,
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
        out_path = plans_dir / f"{base_stem}_changes.json"
        out_path.write_text(json.dumps(out_json, indent=2), encoding="utf-8")
        print(f"\n[+] Saved {len(all_changes)} total angle changes -> {out_path}")
        success_flag.touch()

    except Exception as e:
        print(f"[!] An error occurred: {e}")
        fail_flag.touch()
        raise


if __name__ == "__main__":
    main()