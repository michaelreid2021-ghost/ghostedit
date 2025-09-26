#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 46_srt_b_roll_suggester.py
# Version: 3.3.0 (Nuanced Pacing Update)
# Last Modified: 2025-09-16
#
# Purpose:
#   [REFACTORED] Generate a comprehensive, timecoded B-roll plan by analyzing a
#   timeline-level SRT file. This version includes nuanced rules for pacing,
#   clip spacing ("breathing room"), and handling of sequential shots.

import json
import argparse
import os
import re
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from project_paths import add_base_arg, from_args, ProjectPaths, get_active_project_name
from token_logger import log_token_usage

# --- Gemini / Vertex imports ---
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except Exception:
    _GENAI_AVAILABLE = False

# --------------------------------
# SRT Parsing & Helpers (Aligned with 45_subtitle_based_pacing_test.py)
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

def parse_srt(path: Path) -> List[Caption]:
    if not path.exists(): raise FileNotFoundError(f"SRT not found: {path}")
    content = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", content.strip(), flags=re.MULTILINE)
    out: List[Caption] = []
    for i, block in enumerate(blocks):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2: continue
        try:
            if "-->" not in lines[0] and "-->" in lines[1]: lines.pop(0)
            start_str, end_str = (x.strip() for x in lines[0].split("-->"))
            start_s = srt_time_to_seconds(start_str)
            end_s = srt_time_to_seconds(end_str)
            text = " ".join(re.sub(r'<[^>]+>', '', line) for line in lines[1:])
            out.append(Caption(index=i + 1, start=start_s, end=end_s, text=text))
        except (ValueError, IndexError):
            continue
    out.sort(key=lambda c: (c.start, c.end))
    return out

def normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s']", "", s)
    return s.strip()

def stream_total_end(caps: List[Caption]) -> float:
    return max((c.end for c in caps), default=0.0)

def find_phrase_time(captions: List[Caption], phrase: str, window_size: int = 5) -> Optional[float]:
    if not phrase or not phrase.strip() or not captions: return None
    target_text = normalize_text(phrase)
    if not target_text: return None

    for i in range(len(captions) - window_size + 1):
        window_captions = captions[i : i + window_size]
        window_text = " ".join(normalize_text(c.text) for c in window_captions)
        if target_text in window_text:
            return window_captions[0].start
            
    for c in captions:
        if target_text in normalize_text(c.text):
            return c.start

    return None

def discover_srt(working_root: Path) -> Path:
    preferred_names = ["subtitle 1.srt", "subtitle_1.srt", "subtitles.srt", "captions.srt"]
    for name in preferred_names:
        if (p := working_root / name).exists(): return p
    for p in working_root.glob("*.srt"):
        if "subtitle" in p.name.lower() or "caption" in p.name.lower():
            return p
    raise FileNotFoundError(f"Could not auto-discover a timeline SRT file in '{working_root}'")

# --------------------------------
# Segment & Pacing Plan Logic
# --------------------------------
def load_production_order(p: ProjectPaths) -> Optional[Dict[str, Any]]:
    path = p.working_root / "41_production_order.json"
    return safe_json_load(path) if path.exists() else None

def derive_segments_from_41(captions: List[Caption], prod41: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    plan = (prod41 or {}).get("project_plan", [])
    if not isinstance(plan, list) or not plan:
        if not captions: return []
        return [{"segment_type": "Default", "start": captions[0].start, "end": stream_total_end(captions)}]

    segs = [s for item in plan for s in item.get("segments", []) if isinstance(s, dict)]
    
    anchors = []
    for s in segs:
        seg_type = s.get("segment_type", "Default")
        entry_phrase = s.get("entry_point_phrase", "")
        found_time = find_phrase_time(captions, entry_phrase)
        if found_time is not None:
            anchors.append({"type": seg_type, "time": found_time})

    if not anchors: 
        if not captions: return []
        return [{"segment_type": "Default", "start": captions[0].start, "end": stream_total_end(captions)}]

    anchors.sort(key=lambda x: x["time"])
    total_end = stream_total_end(captions)
    
    segments_out = []
    if captions and anchors[0]["time"] > captions[0].start + 1.0:
        segments_out.append({"segment_type": "Introduction", "start": captions[0].start, "end": anchors[0]["time"]})

    for i, anchor in enumerate(anchors):
        start_time = anchor["time"]
        end_time = anchors[i + 1]["time"] if i + 1 < len(anchors) else total_end
        if end_time - start_time > 1.0:
            segments_out.append({"segment_type": anchor["type"], "start": start_time, "end": end_time})
            
    return segments_out

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

# --- Other Utilities ---
def safe_json_load(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception: return None

def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)

# --------------------------------
# Gemini Client & Prompting
# --------------------------------
class GeminiClient:
    def __init__(self, project_id: str, location: str, model_name: str, seed: int = 42):
        self.client = genai.Client(vertexai=True, project=project_id, location=location) if _GENAI_AVAILABLE else None
        self.model_name = model_name
        self.seed = seed
        self.safety_settings = [ types.SafetySetting(category=c, threshold="OFF") for c in ["HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT"]] if _GENAI_AVAILABLE else []

    def generate(self, user_content: str, system_instruction: str, response_schema: dict,
        max_output_tokens: int = 20000, temperature: float = 0.7, top_p: float = 0.45,
    ) -> Optional[types.GenerateContentResponse]:
        if not self.client: return None
        try:
            config = types.GenerateContentConfig(
                temperature=temperature, top_p=top_p, seed=self.seed, max_output_tokens=max_output_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=12000),
                safety_settings=self.safety_settings,
                system_instruction=[types.Part.from_text(text=system_instruction)],
                response_mime_type="application/json", response_schema=response_schema,
            )
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_content)])],
                config=config,
            )
            return response
        except Exception as e:
            print(f" [!] Gemini API error: {e}")
            return None

def suggestion_system_prompt() -> str:
    # --- REVISED "MAXIMUM EFFORT" PROMPT V2 (WITH PACING RULES) ---
    return """You are a Creative Director for a talking-head educational video. Your task is to generate a comprehensive B-roll plan that is not only visually illustrative but also rhythmically sound and professional.

## Core Directive
Your primary goal is to find every meaningful opportunity for B-roll. It is better to provide a few extra shots that can be deleted than to miss an opportunity. When in doubt, suggest a shot.

## Pacing & Rhythm
This is the most important part of your job. Visuals must serve the speaker's cadence.
1.  **Enforce 'Breathing Room'**: After a B-roll clip (or a sequence of them) ends, you MUST leave a gap of **at least two full subtitle captions** where the main speaker is on-screen before suggesting the next B-roll shot. This prevents a jarring, "wall-to-wall" B-roll effect and gives the speaker necessary screen time.
2.  **Minimum Duration**: Individual B-roll shots should be at least 1.5 seconds. Do not suggest clips shorter than this. Durations of 2.0 to 4.0 seconds are typical and preferred.

## Technical Rules
- **Handle Sequences Correctly**: For rapid-fire lists or sequences in the dialogue (e.g., "...essays, documents, and tests"), generate a **separate B-roll suggestion for each item**. This creates a 'doublet' or 'triplet' sequence of clips. **Do not combine them into one prompt.**
- **Motivation**: Every suggestion MUST be directly motivated by specific words in the transcript. State the motivation clearly in the `reasoning` field.
- **Accuracy**: All timestamps must be precise and align with the provided SRT caption data.
- **Forbidden Segments**: Do NOT suggest B-roll for segments identified as "Hook" or "Disclaimer".
- **Timeline Offset**: The timeline may start at an offset (e.g., 3600 seconds). All your timestamps must respect this offset.

Output strictly as JSON matching the provided schema.
"""

def suggestion_response_schema() -> dict:
    return {"type": "OBJECT", "properties": {"b_roll_suggestions": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"video_prompt": {"type": "STRING"}, "type": {"type": "STRING", "enum": ["establishing", "cutaway", "insert", "overlay", "product", "stock", "generated", "transition", "motion_graphic"]},"reasoning": {"type": "STRING"},"timestamp_start": {"type": "NUMBER"},"duration_seconds": {"type": "NUMBER"},"source_segment": {"type": "OBJECT", "properties": {"start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "text": {"type": "STRING"}}, "required": ["start", "end", "text"]},}, "required": ["video_prompt", "type", "reasoning", "timestamp_start", "duration_seconds", "source_segment"],}}}, "required": ["b_roll_suggestions"],}

def build_suggestion_user_payload(captions: List[Caption]) -> str:
    minimal = [{"start": round(c.start, 3), "end": round(c.end, 3), "text": c.text} for c in captions]
    return json.dumps({"captions": minimal}, ensure_ascii=False, indent=2)

# --------------------------------
# Main
# --------------------------------
def main(cfg):
    p = from_args(cfg)
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
        print("[+] Starting AI B-Roll Suggester (SRT-Based, Nuanced Pacing)...")
        
        srt_path = discover_srt(p.working_root)
        captions = parse_srt(srt_path)
        prod41 = load_production_order(p)
        segments = derive_segments_from_41(captions, prod41)
        
        pacing_file = find_pacing_plan_file(p, srt_path)
        angle_changes = load_angle_changes(pacing_file)

        print(f"\n[i] Using SRT: {srt_path}")
        print(f"[i] Using Angle Plan: {pacing_file.name if pacing_file else 'None Found'}")
        print(f"[i] Derived {len(segments)} segments to process.")

        if not segments:
            print("[!] No segments were derived. Check that `41_production_order.json` phrases exist in the SRT content.")
            return

        gemini = GeminiClient(cfg.project_id, cfg.location, cfg.model, seed=cfg.seed)
        output_dir = cfg.output_dir or str(p.b_roll_dir)
        os.makedirs(output_dir, exist_ok=True)
        out_path = Path(output_dir) / f"{srt_path.stem}_broll_plan.json"
        all_suggestions: List[Dict[str, Any]] = []
        
        SKIP_TYPES = {"Hook", "Disclaimer"}
        for seg in segments:
            seg_type, start_time, end_time = seg["segment_type"], seg["start"], seg["end"]
            
            if seg_type in SKIP_TYPES:
                print(f"\n-> Skipping segment '{seg_type}' [{start_time:.2f}s – {end_time:.2f}s]")
                continue

            print(f"\n-> Analyzing segment '{seg_type}' [{start_time:.2f}s – {end_time:.2f}s]")
            
            seg_captions = [c for c in captions if start_time <= c.start < end_time]
            if not seg_captions: continue
            
            user_payload = build_suggestion_user_payload(seg_captions)
            
            print(f"   -> Calling AI for {len(seg_captions)} captions...")
            response = gemini.generate(
                user_content=user_payload,
                system_instruction=suggestion_system_prompt(),
                response_schema=suggestion_response_schema(),
                temperature=cfg.suggest_temp
            )

            if response and response.text:
                if response.usage_metadata:
                    log_token_usage(
                        project_name=get_active_project_name(),
                        script_name=Path(__file__).name,
                        model_name=cfg.model,
                        prompt_token_count=response.usage_metadata.prompt_token_count,
                        candidates_token_count=response.usage_metadata.candidates_token_count,
                        total_token_count=response.usage_metadata.total_token_count
                    )
                parsed = json.loads(response.text)
                if parsed and isinstance(parsed.get("b_roll_suggestions"), list):
                    suggestions = parsed["b_roll_suggestions"]
                    print(f"   -> Received {len(suggestions)} suggestions for this segment.")
                    all_suggestions.extend(suggestions) 
                else:
                    print(f"   -> No valid suggestions returned for this segment. Raw response: {response.text[:200] if response.text else 'N/A'}")
            else:
                print(f"   -> No valid suggestions returned for this segment. Raw response: {response.text[:200] if response and response.text else 'N/A'}")

        all_suggestions.sort(key=lambda x: x.get("timestamp_start", 0.0))
        
        output_data = {
            "source_srt": str(srt_path),
            "b_roll_plan": all_suggestions,
            "generator_metadata": {"model": cfg.model, "effort_level": "maximum_nuanced"}
        }
        atomic_write_json(str(out_path), output_data)
        print(f"\n[+] Success! Saved {len(all_suggestions)} total B-roll suggestions to '{out_path}'")
        success_flag.touch()

    except Exception as e:
        print(f"\n[!] An unexpected error occurred: {e}")
        fail_flag.touch()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SRT-based B-roll suggestions.")
    add_base_arg(parser)
    parser.add_argument("--project_id", type=str, default="qtow-14862_")
    parser.add_argument("--location", type=str, default="us-central1")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_-argument("--output_dir", type=str, default=None, help="Override for the 46_ai_b_roll_plans directory.")
    # REMOVED: No longer needed as the prompt is now hardcoded for max effort.
    # parser.add_argument("--max_suggestions_per_chunk", type=int, default=30)
    parser.add_argument("--suggest_temp", type=float, default=0.5, help="Slightly higher temp for more creativity")
    config = parser.parse_args()
    main(config)