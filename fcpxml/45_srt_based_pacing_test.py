#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 45_subtitle_based_pacing_test.py
# Version: 3.4.0
# Last Modified: 2025-09-02
#
# Purpose:
#   Guided AI pacing over a timeline-level SRT ("subtitle 1.srt") already aligned to the *clean-take* timeline.
#   - Pipeline-aware: auto-discovers active project (ProjectPaths) and SRT in working_root.
#   - Reads 41_production_order.json to define segment windows (skips Hook by default).
#   - Iterates *within* each segment window using fixed strides and 45s analysis windows.
#   - Builds rich, multi-line prompts (not one-line trickles), shows full console debug per turn.
#   - Validates AI proposals; if out-of-window, snaps to nearest sensible boundary and continues.
#   - Fallback (when Vertex/Gemini not available) proposes ~8s cuts repeatedly within window.
#
# Inputs (auto):
#   - <working_root>/subtitle 1.srt   (smart discovery; other common names tried)
#   - <working_root>/41_production_order.json  (segment layout; if absent, one Default segment for full SRT)
#
# Outputs:
#   - <working_root>/45_ai_pacing_plans/<srt_stem>_changes.json
#
# Notes:
#   - “Hook” is ignored by default for pacing (configurable below).
#   - Angle toggles A↔B on each accepted cut.
#   - This file is standalone and runnable as-is.

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

from project_paths import add_base_arg, from_args, ProjectPaths

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
SKIP_SEGMENT_TYPES = {"Hook"}  # “Hook” typically held single-angle; skip for pacing
ANALYSIS_WINDOW_SEC = 45.0     # size of transcript chunk fed to AI
STRIDE_SEC = 10.0              # how far we move forward when we advance the window
MIN_CUT_AFTER = 2.0            # AI-proposed cut must be at least this many seconds into the window
PROMPT_PREVIEW_CHARS = 500     # console preview of prompt body
MAX_REPEATED_FPS = 12          # fingerprint history len to detect repeats

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
        # index (optional)
        idx = None
        try:
            idx = int(lines[0].strip())
            lines = lines[1:]
        except Exception:
            idx = idx_auto
        idx_auto += 1
        # time line
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
        # text
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
    # flatten
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

def next_caption_start_at_or_after(caps: List[Caption], t: float) -> Optional[float]:
    for c in caps:
        if c.start + 1e-6 >= t:
            return c.start
    return None

def captions_in_window(caps: List[Caption], start_time: float, duration: float) -> List[Caption]:
    end_time = start_time + duration
    return [c for c in caps if c.start >= start_time and c.start < end_time]

def gather_window_lines(caps: List[Caption], start_time: float, duration: float) -> List[str]:
    lines: List[str] = []
    end_time = start_time + duration
    added_marker = False
    for c in caps:
        if c.start >= start_time and c.start < end_time:
            line = f"[{c.start:.2f}s] - {c.text.strip()}"
            if not added_marker:
                line = line.replace("] -", "][current position] -", 1)
                added_marker = True
            lines.append(line)
    return lines

def compute_window_fingerprint(caps: List[Caption], start_time: float, duration: float) -> str:
    end_time = start_time + duration
    parts: List[str] = []
    for c in caps:
        if c.start >= start_time and c.start < end_time:
            parts.append(f"{c.start:.2f}|{normalize_text(c.text)}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

def snap_to_sentence_end_or_next(caps: List[Caption], t: float, t_max: float) -> float:
    # Prefer next caption that ends with . ? !
    for c in caps:
        if c.start + 1e-6 >= t and c.start < t_max:
            if c.text.rstrip().endswith(('.', '?', '!')):
                return c.start
    # else just the next caption start
    ns = next_caption_start_at_or_after(caps, t)
    if ns is not None and ns < t_max:
        return ns
    return min(t_max, t + STRIDE_SEC)

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

    def call(self, system_instruction: str, user_text: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            # Offline fallback: propose ~8s cut within window; actual clamping happens upstream.
            return {
                "propose_cut": True,
                "cut_after_seconds": 8.0,
                "reason": "Offline fallback pacing (~8s).",
                "ignore_previous_angle_change": False
            }
        try:
            config = types.GenerateContentConfig(
                temperature=0.2,
                top_p=0.95,
                seed=42,
                max_output_tokens=4096,
                safety_settings=self.safety_settings,
                thinking_config=types.ThinkingConfig(thinking_budget=4096),
                system_instruction=[types.Part.from_text(text=system_instruction)],
                response_mime_type="application/json",
                response_schema=self.response_schema,
            )
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_text)])],
                config=config,
            )
            if response.text is None or response.text.strip().lower() == "null":
                return None
            return json.loads(response.text)
        except Exception:
            return None

# -------------------------------
# Pacing rules
# -------------------------------

PACING_RULES = {
    "Hook": "Hold a single shot; avoid cuts unless absolutely necessary.",
    "Introduction": "Deliberate pace; let shots breathe (10–20s).",
    "Disclaimer": "Single, uninterrupted shot; avoid angle changes.",
    "Problem": "Build urgency; avoid choppy cuts; 15–35s spacing.",
    "Reframe": "Moderate; cut after a supportive thought (7–12s).",
    "Value": "Dynamic; mix 8–11s with occasional 4–7s on key points.",
    "Proof": "Moderate; cut around evidence beats (8–12s).",
    "Lesson": "Teach cadence; cut at step boundaries (8–14s).",
    "Story": "Let it land; sentence ends or scene beats (10–16s).",
    "Roadmap": "Short and clear; 6–10s per beat.",
    "Transition": "Quick, purposeful; 4–8s.",
    "CTA": "Impactful; ensure final sentence on Angle A.",
    "Outro": "Direct and conclusive; ensure last line on Angle A.",
    "Default": "Standard conversational pace; average 7–9s.",
}

ALLOW_CUTS = {
    "Disclaimer": False,
    "Hook": False,  # protect hook (redundant with SKIP_SEGMENT_TYPES, belt-and-suspenders)
}

# -------------------------------
# Core pacing loop (bounded by a segment)
# -------------------------------

def run_pacing_over_captions(
    captions: List[Caption],
    gemini: GeminiClient,
    segment_type: str,
    segment_start: float,
    segment_end: float,
    window_sec: float,
    stride_sec: float,
    log_fn=print,
) -> List[Dict[str, Any]]:
    if not captions or segment_start >= segment_end:
        return []

    if not ALLOW_CUTS.get(segment_type, True):
        log_fn(f"     -> Segment policy: no cuts for '{segment_type}'.")
        return []

    guidance = PACING_RULES.get(segment_type, PACING_RULES["Default"])
    current_time = max(segment_start, 0.0)
    current_angle = "Angle A"
    changes: List[Dict[str, Any]] = []
    fp_history: List[str] = []

    # Encourage well-formed, bounded proposals
    base_system_prompt = (
        "You are an expert video editor proposing camera-angle changes for pacing. "
        "You will receive a bounded caption window from a specific segment of a video. "
        f"Only propose a cut within this window. The cut time you return must be a number of seconds "
        f"FROM the start of the window (i.e., between {MIN_CUT_AFTER:.1f} and WINDOW_SIZE seconds). "
        "Prefer cuts at sentence ends or natural pauses; avoid cutting mid-question or during emphasis peaks. "
        "If no good cut exists inside the window, set 'propose_cut' to false."
    )

    while current_time < segment_end:
        # Clamp window to segment bounds
        win_end = min(segment_end, current_time + window_sec)
        if win_end - current_time < MIN_CUT_AFTER + 0.25:
            # Not enough space to even make a valid cut
            break

        # Build caption slice and lines
        win_caps = captions_in_window(captions, current_time, win_end - current_time)
        lines = gather_window_lines(captions, current_time, win_end - current_time)

        # If no lines, move forward to next caption or stride
        if not lines:
            ns = next_caption_start_at_or_after(captions, current_time + 0.01)
            if ns is not None and ns < segment_end:
                log_fn(f"     -> Empty window; snap fwd {current_time:.2f}→{ns:.2f}.")
                current_time = ns
                continue
            else:
                break

        # Fingerprint to avoid stuck loops
        fp = compute_window_fingerprint(captions, current_time, win_end - current_time)
        if fp in fp_history:
            log_fn("     -> Repeated window detected; snapping to next sentence end/next caption.")
            new_t = snap_to_sentence_end_or_next(captions, current_time, segment_end)
            if new_t <= current_time + 0.5:
                new_t = min(segment_end, current_time + stride_sec)
            current_time = new_t
            continue
        fp_history.append(fp)
        if len(fp_history) > MAX_REPEATED_FPS:
            fp_history.pop(0)

        # Compose prompt
        body = "\n".join(lines)
        user_prompt = (
            f"SEGMENT TYPE: {segment_type}\n"
            f"PACING GUIDANCE: {guidance}\n"
            f"WINDOW SIZE: {win_end - current_time:.2f}s (from {current_time:.2f}s to {win_end:.2f}s within the segment)\n"
            f"RETURN cut_after_seconds BETWEEN {MIN_CUT_AFTER:.1f} AND {win_end - current_time:.2f} ONLY.\n\n"
            f"CAPTIONS IN WINDOW (~{int(win_end - current_time)}s):\n{body}"
        )

        # Console debug (be loud)
        print(f"     -> Window: {current_time:.2f}s → {win_end:.2f}s | span={win_end - current_time:.2f}s | lines={len(lines)}")
        if lines:
            print(f"        First: {lines[0]}")
            print(f"        Last : {lines[-1]}")
        print(f"        PromptLen={len(user_prompt)} preview:\n        {user_prompt[:PROMPT_PREVIEW_CHARS].replace(chr(10), chr(10)+'        ')}")
        print("        [AI CALL] ...", end="", flush=True)

        # Call AI
        resp = gemini.call(system_instruction=base_system_prompt, user_text=user_prompt)
        print(" done.")
        print("        RawResp:", (json.dumps(resp, indent=2) if isinstance(resp, dict) else str(resp))[:800].replace("\n", "\n        "))

        # Handle null/invalid
        if not resp or not isinstance(resp, dict):
            # No response; move ahead safely
            new_t = snap_to_sentence_end_or_next(captions, current_time, segment_end)
            print(f"        -> Null/invalid response; advance to {new_t:.2f}s.")
            current_time = new_t
            continue

        propose = bool(resp.get("propose_cut", False))
        ignore_prev = bool(resp.get("ignore_previous_angle_change", False))
        reason = str(resp.get("reason", "") or "")

        if ignore_prev and changes:
            last = changes.pop()
            current_time = float(last["previous_time"])
            current_angle = last["angle_choice"]
            print(f"        -> AI requests undo previous cut; rewind to {current_time:.2f}s. Reason: {reason or 'N/A'}")
            # Do not stride; re-evaluate from this location with next loop
            continue

        if not propose:
            # No cut in this window; step forward
            new_t = snap_to_sentence_end_or_next(captions, current_time, segment_end)
            print(f"        -> AI skips. Reason: {reason or 'N/A'} | advance to {new_t:.2f}s.")
            current_time = new_t
            continue

        # Validate cut_after_seconds
        try:
            cut_after = float(resp.get("cut_after_seconds"))
        except Exception:
            cut_after = -1.0

        win_span = win_end - current_time
        valid_lower = MIN_CUT_AFTER
        valid_upper = max(MIN_CUT_AFTER + 0.25, win_span - 0.25)

        if not (valid_lower <= cut_after <= valid_upper):
            # Attempt to salvage: clamp to nearest sentence end inside window; otherwise stride
            print(f"        -> Proposed cut_after_seconds={cut_after} outside [{valid_lower:.2f}, {valid_upper:.2f}] for this window.")
            snap_t = snap_to_sentence_end_or_next(captions, current_time + valid_lower, min(win_end, current_time + valid_upper))
            if current_time + valid_lower <= snap_t <= min(win_end, current_time + valid_upper):
                abs_time = snap_t
                record = {
                    "propose_cut": True,
                    "cut_after_seconds": abs_time - current_time,
                    "absolute_cut_time": abs_time,
                    "previous_time": current_time,
                    "angle_choice": current_angle,
                    "reason": f"Snapped invalid proposal to nearest boundary. Original: {cut_after}. {reason}".strip(),
                    "ignore_previous_angle_change": False,
                }
                changes.append(record)
                current_time = abs_time
                current_angle = "Angle B" if current_angle == "Angle A" else "Angle A"
                print(f"        -> Snapped cut to {abs_time:.2f}s ({record['cut_after_seconds']:.2f}s into window). Angle→{current_angle}.")
                continue
            else:
                # Could not snap; stride forward
                new_t = min(segment_end, current_time + stride_sec)
                print(f"        -> Could not snap; advancing by stride to {new_t:.2f}s.")
                current_time = new_t
                continue

        # Accept cut
        abs_time = current_time + cut_after
        if abs_time >= win_end - 1e-3 or abs_time > segment_end:
            # Guard: if at/after window end, nudge earlier to within window
            abs_time = min(segment_end - 1e-3, win_end - 1.0)
        record = {
            "propose_cut": True,
            "cut_after_seconds": abs_time - current_time,
            "absolute_cut_time": abs_time,
            "previous_time": current_time,
            "angle_choice": current_angle,
            "reason": reason,
            "ignore_previous_angle_change": False,
        }
        changes.append(record)
        current_time = abs_time
        current_angle = "Angle B" if current_angle == "Angle A" else "Angle A"
        print(f"        -> AI decision accepted: Cut at {abs_time:.2f}s. Angle→{current_angle}.")

        # If we’re very close to window end, roll into next window naturally
        if win_end - current_time < MIN_CUT_AFTER:
            current_time = min(segment_end, win_end)

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
    # 1) working_root direct
    for name in preferred_names:
        p = working_root / name
        tried.append(p)
        if p.exists():
            return p, tried
    # 2) peers of common fcpxmls
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
    # 3) CWD
    cwd = Path.cwd()
    for name in preferred_names:
        p = cwd / name
        tried.append(p)
        if p.exists():
            return p, tried
    # 4) smart glob shallow then rglob
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
    # Expect shape:
    # { "project_plan": [ { "take_number": "...", "segments": [ {"segment_type": "...", "entry_point_phrase": "..."} ... ] } ] }
    out: List[Dict[str, Any]] = []
    plan = (prod41 or {}).get("project_plan") or []
    if not isinstance(plan, list) or not plan:
        # fallback: one default segment covering entire SRT
        if captions:
            out.append({"segment_type": "Default", "entry_point_phrase": "", "start": captions[0].start, "end": stream_total_end(captions)})
        return out

    # Flatten only first item's segments (typical single-take speech); if multiple, concatenate in order
    segs: List[Dict[str, Any]] = []
    for item in plan:
        s = item.get("segments") or []
        if isinstance(s, list):
            segs.extend(s)

    # Resolve start times by phrase; end = next segment start or SRT end
    # Skip SKIP_SEGMENT_TYPES
    anchors: List[Tuple[str, Optional[str], Optional[float]]] = []
    for seg in segs:
        st = str(seg.get("segment_type", "Default"))
        ep = seg.get("entry_point_phrase")
        if st in SKIP_SEGMENT_TYPES:
            continue
        t = find_phrase_time(captions, ep) if isinstance(ep, str) and ep.strip() else None
        anchors.append((st, ep, t))

    # Filter out anchors with completely missing timestamps (but keep if it's the first and we can start from the first caption)
    first_cap_start = captions[0].start if captions else 0.0
    usable: List[Tuple[str, Optional[str], float]] = []
    for i, (st, ep, t) in enumerate(anchors):
        if t is None:
            if i == 0 and captions:
                usable.append((st, ep, first_cap_start))
        else:
            usable.append((st, ep, float(t)))

    if not usable and captions:
        # fallback entire
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
    parser.add_argument("--model", type=str, default="gemini-2.5-flash")
    parser.add_argument("--srt", type=str, default=None, help="Optional explicit SRT path; otherwise auto-discovered.")
    parser.add_argument("--window_seconds", type=float, default=ANALYSIS_WINDOW_SEC)
    parser.add_argument("--stride_seconds", type=float, default=STRIDE_SEC)
    args = parser.parse_args()

    p = from_args(args)
    working_root = Path(p.working_root)
    base = Path(p.base)
    plans_dir = Path(p.plans_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)

    # Locate SRT
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
            for cand in tried:
                print(f"      - {cand}")
            print("\n    Drop your file as 'subtitle 1.srt' in:", working_root)
            sys.exit(1)

    # Parse captions
    captions = parse_srt(srt_path)

    # Load 41 production order (segments)
    prod41 = load_production_order(p)
    segments = derive_segments_from_41(captions, prod41)

    # Init client
    gemini_client = GeminiClient(args.project_id, args.location, args.model)

    print(f"[+] Active project base: {p.base}")
    print(f"[+] Working root:       {working_root}")
    print(f"[+] Plans dir:          {plans_dir}")
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

        print(f"  -> Segment {i}: '{stype}' | Anchor: {ephrase or '(none)'} | Window: {s_start:.2f}s → {s_end:.2f}s")

        changes = run_pacing_over_captions(
            captions=captions,
            gemini=gemini_client,
            segment_type=stype,
            segment_start=s_start,
            segment_end=s_end,
            window_sec=float(args.window_seconds),
            stride_sec=float(args.stride_seconds),
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
    print(f"[+] Saved {len(all_changes)} total angle changes → {out_path}")

if __name__ == "__main__":
    main()
