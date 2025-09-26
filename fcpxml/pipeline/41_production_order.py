#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 41_production_order.py (subtitle-aware, AI-driven, with logging + consolidation)
# Version: 2.1.0
# Last Modified: 2025-09-02
#
# Behavior:
# - Reads "subtitle 1.srt" from the project's working_root (per project_paths.py).
# - Calls Gemini to segment the clean transcript into production segments.
# - Logs EXACTLY what was sent to AI (system + user) and the raw AI JSON response.
# - Consolidates/cleans segments to avoid over-segmentation and duplicates.
# - Writes standard pipeline output: 41_production_order.json
#
# Outputs (in working_root):
#   - 41_ai_segmentation_prompt.txt     (system + user content sent to AI)
#   - 41_ai_segmentation_raw.json       (verbatim AI JSON response)
#   - 41_ai_segmentation_consolidated.json (post-processing preview)
#   - 41_production_order.json          (pipeline contract)

import argparse
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from project_paths import add_base_arg, from_args, get_active_project_name
from token_logger import log_token_usage

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

# =========================
# Gemini client (Vertex)
# =========================
try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None

class GeminiClient:
    def __init__(self, project_id: str, location: str, model_name: str):
        if genai is None or types is None:
            raise RuntimeError("google.genai is not available in this environment.")
        self.client = genai.Client(vertexai=True, project=project_id, location=location)
        self.model_name = model_name
        self.safety = [
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ]

    def infer_segments(self, transcript_text: str, max_segments: int, prompt_log_path: Path) -> Optional[types.GenerateContentResponse]:
        system_instruction = (
            "You classify a clean-take talking-head transcript into production segments. "
            "Return JSON only. Choose segment_type from this set: "
            "['Hook','Introduction','Disclaimer','Problem Statement','Objection/Anxiety','Reframe/Encouragement',"
            "'Solution/Value','Proof','How-To/Lesson','Story/Example','Roadmap','Transition','Call to Action','Outro/Thanks','Default']. "
            "Each segment must include an entry_point_phrase that appears verbatim in the transcript. "
            "Prefer phrases that align with a natural new idea/start. Keep segments ordered by first occurrence. "
            "Avoid over-segmentation; 5–7 segments is typical for a ~10-minute single-voice piece."
        )
        schema = {
            "type": "OBJECT",
            "properties": {
                "segments": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "segment_type": {"type": "STRING"},
                            "entry_point_phrase": {"type": "STRING"}
                        },
                        "required": ["segment_type", "entry_point_phrase"]
                    }
                }
            },
            "required": ["segments"]
        }

        # Build payload
        user_text = (
            "Transcript (clean subtitles, chronological, one line per caption):\n\n"
            + transcript_text[:120000] +  # safeguard; enough for ~10+ minutes
            "\n\nRespond with JSON only per the schema."
        )

        # LOG EXACT PROMPT
        prompt_log_path.write_text(
            "=== SYSTEM INSTRUCTION ===\n"
            + system_instruction
            + "\n\n=== USER CONTENT ===\n"
            + user_text,
            encoding="utf-8"
        )

        contents = [
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
        ]
        cfg = types.GenerateContentConfig(
            temperature=0.2,
            top_p=0.9,
            seed=42,
            max_output_tokens=24576,
            safety_settings=self.safety,
            thinking_config=types.ThinkingConfig(thinking_budget=12288),
            system_instruction=[types.Part.from_text(text=system_instruction)],
            response_mime_type="application/json",
            response_schema=schema,
        )
        try:
            resp = self.client.models.generate_content(model=self.model_name, contents=contents, config=cfg)
            return resp
        except Exception:
            return None

# =========================
# Anchoring + Consolidation
# =========================
def build_corpus(caps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    corpus = []
    for c in caps:
        norm = re.sub(r"\s+", " ", c["text"]).strip().lower()
        corpus.append({"start": c["start"], "end": c["end"], "text": c["text"], "norm": norm})
    return corpus

def anchor_phrase_to_time(phrase: str, corpus: List[Dict[str, Any]]) -> Optional[float]:
    target = re.sub(r"\s+", " ", (phrase or "")).strip().lower()
    if not target:
        return None
    # exact substring inside any caption
    for c in corpus:
        if target in c["norm"]:
            return c["start"]
    # fallback: first caption containing any 5+ char token
    tokens = [t for t in re.findall(r"[a-z0-9']+", target) if len(t) >= 5]
    for t in tokens:
        for c in corpus:
            if t in c["norm"]:
                return c["start"]
    return None

def consolidate_segments(
    raw_segments: List[Dict[str, str]],
    captions: List[Dict[str, Any]],
    max_segments: int = 7,
    min_spacing_s: float = 60.0
) -> List[Dict[str, Any]]:
    """
    Rules:
      - Anchor every entry_point_phrase to caption start time (or -1 if not found).
      - Drop Roadmap/Transition as standalone segments (inherit pacing from neighbors).
      - Keep at most one Solution/Value and one How-To/Lesson unless anchors are >= 90s apart.
      - Merge Reframe/Encouragement into Introduction if within 90s of Intro anchor.
      - Enforce a priority floor to always try to keep: Hook, Introduction, one Solution/Value, one How-To/Lesson, Call to Action, Outro/Thanks.
      - Enforce min spacing (default 60s) between final anchors by dropping the later-in-time of too-close neighbors (except CTA/Outro near the end).
    """
    corpus = build_corpus(captions)

    # Anchor times
    anchored = []
    for seg in raw_segments:
        st = (seg.get("segment_type") or "Default").strip()
        ep = (seg.get("entry_point_phrase") or "").strip()
        t = anchor_phrase_to_time(ep, corpus)
        anchored.append({"segment_type": st, "entry_point_phrase": ep, "anchor_time": t if t is not None else -1.0})

    # Sort by time (unknowns at end)
    anchored.sort(key=lambda x: (1e12 if x["anchor_time"] is None or x["anchor_time"] < 0 else x["anchor_time"]))

    # 1) Cull light types
    collapse_types = {"Roadmap", "Transition"}
    anchored = [a for a in anchored if a["segment_type"] not in collapse_types]

    # 2) Merge Reframe -> Introduction if within 90s
    intro_time = next((a["anchor_time"] for a in anchored if a["segment_type"] == "Introduction" and a["anchor_time"] >= 0), None)
    if intro_time is not None:
        merged = []
        for a in anchored:
            if a["segment_type"] == "Reframe/Encouragement" and a["anchor_time"] >= 0:
                if abs(a["anchor_time"] - intro_time) <= 90.0:
                    # skip this entry (merged logically into Introduction pacing)
                    continue
            merged.append(a)
        anchored = merged

    # 3) Limit repeated Solution/Value and How-To unless far apart
    def limit_type(entries: List[Dict[str, Any]], tname: str, min_gap: float = 90.0) -> List[Dict[str, Any]]:
        kept: List[Dict[str, Any]] = []
        last_time: Optional[float] = None
        for a in entries:
            if a["segment_type"] != tname:
                kept.append(a); continue
            if last_time is None or a["anchor_time"] < 0 or abs(a["anchor_time"] - last_time) >= min_gap:
                kept.append(a)
                if a["anchor_time"] >= 0:
                    last_time = a["anchor_time"]
            # else: drop as duplicate too-close
        return kept

    anchored = limit_type(anchored, "Solution/Value", 90.0)
    anchored = limit_type(anchored, "How-To/Lesson", 90.0)

    # 4) Enforce min spacing globally, but keep CTA/Outro near end
    prioritized_types = {"Hook", "Introduction", "Solution/Value", "How-To/Lesson", "Call to Action", "Outro/Thanks"}
    final: List[Dict[str, Any]] = []
    last_kept_time: Optional[float] = None
    for a in anchored:
        t = a["anchor_time"]
        if t is None or t < 0:
            continue
        if last_kept_time is None or (t - last_kept_time) >= min_spacing_s:
            final.append(a)
            last_kept_time = t
        else:
            # if the current is CTA/Outro and we're later, prefer it
            if a["segment_type"] in {"Call to Action", "Outro/Thanks"}:
                # replace previous if previous is non-priority
                if final and final[-1]["segment_type"] not in {"Call to Action", "Outro/Thanks"}:
                    final[-1] = a
                    last_kept_time = t
                # else drop this one (too close)
            # else: drop this one
            pass

    # 5) Ensure priority floor presence if possible (add back first occurrences in order if missing and budget allows)
    def first_occurrence(tname: str) -> Optional[Dict[str, Any]]:
        for a in anchored:
            if a["segment_type"] == tname and a["anchor_time"] >= 0:
                return a
        return None

    for tname in ["Hook", "Introduction", "Solution/Value", "How-To/Lesson", "Call to Action", "Outro/Thanks"]:
        if all(x["segment_type"] != tname for x in final):
            cand = first_occurrence(tname)
            if cand is not None:
                # insert keeping chronological order and spacing if possible
                inserted = False
                for i in range(len(final) + 1):
                    trial = final[:i] + [cand] + final[i:]
                    trial.sort(key=lambda x: x["anchor_time"])
                    ok = True
                    last = None
                    for it in trial:
                        if last is not None and (it["anchor_time"] - last) < min_spacing_s:
                            ok = False; break
                        last = it["anchor_time"]
                    if ok and len(trial) <= max_segments:
                        final = trial
                        inserted = True
                        break
                # if not inserted, skip

    # Trim to max
    final.sort(key=lambda x: x["anchor_time"])
    if len(final) > max_segments:
        # Keep prioritized ones first
        pri = [a for a in final if a["segment_type"] in prioritized_types]
        non = [a for a in final if a["segment_type"] not in prioritized_types]
        final = (pri + non)[:max_segments]

    # Ensure at least a minimal set if we somehow lost everything
    if not final and captions:
        final = [
            {"segment_type": "Introduction", "entry_point_phrase": captions[0]["text"], "anchor_time": captions[0]["start"]},
            {"segment_type": "Call to Action", "entry_point_phrase": captions[-1]["text"], "anchor_time": captions[-1]["start"]},
        ]

    # Strip anchor_time for contract
    return [{"segment_type": a["segment_type"], "entry_point_phrase": a["entry_point_phrase"]} for a in final]

# =========================
# Main
# =========================
def main():
    parser = argparse.ArgumentParser(description="Build 41_production_order.json from 'subtitle 1.srt' using AI segmentation (logged) + consolidation.")
    add_base_arg(parser)
    parser.add_argument("--project_id", type=str, default="qtow-148623")
    parser.add_argument("--location", type=str, default="global")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro")
    parser.add_argument("--max_segments", type=int, default=7)
    parser.add_argument("--min_spacing_s", type=float, default=60.0)
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
        print(f"[+] Active project base: {p.base}")
        print(f"[+] Working root:       {working_root}")

        srt_path = working_root / "subtitle 1.srt"
        print(f"[+] Using subtitles at: {srt_path}")
        captions = parse_srt(srt_path)
        print(f"[+] Loaded {len(captions)} caption items.")

        # Build transcript text (each caption on its own line to preserve anchorability)
        transcript_text = "\n".join([c["text"] for c in captions])

        # Log paths
        prompt_log_path = working_root / "41_ai_segmentation_prompt.txt"
        raw_json_path = working_root / "41_ai_segmentation_raw.json"
        consolidated_preview_path = working_root / "41_ai_segmentation_consolidated.json"

        segments_ai: List[Dict[str, Any]] = []
        used_ai = False

        # Attempt AI segmentation with full logging
        try:
            client = GeminiClient(args.project_id, args.location, args.model)
            ai_resp = client.infer_segments(transcript_text, args.max_segments, prompt_log_path)
            if ai_resp and ai_resp.text:
                if ai_resp.usage_metadata:
                    log_token_usage(
                        project_name=get_active_project_name(),
                        script_name=Path(__file__).name,
                        model_name=args.model,
                        prompt_token_count=ai_resp.usage_metadata.prompt_token_count,
                        candidates_token_count=ai_resp.usage_metadata.candidates_token_count,
                        total_token_count=ai_resp.usage_metadata.total_token_count
                    )
                ai_json = json.loads(ai_resp.text)
                raw_json_path.write_text(json.dumps(ai_json, indent=2), encoding="utf-8")
                if isinstance(ai_json.get("segments"), list) and ai_json["segments"]:
                    segments_ai = ai_json["segments"]
                    used_ai = True
                else:
                    raw_json_path.write_text(json.dumps({"error": "no_segments_or_empty_response"}, indent=2), encoding="utf-8")
            else:
                raw_json_path.write_text(json.dumps({"error": "no_segments_or_empty_response"}, indent=2), encoding="utf-8")
        except Exception as e:
            prompt_log_path.write_text("AI client unavailable or error occurred.\n", encoding="utf-8")
            raw_json_path.write_text(json.dumps({"error": f"{e.__class__.__name__}: {e}"}, indent=2), encoding="utf-8")
            used_ai = False

        # Fallback if AI failed completely
        if not segments_ai:
            # naive 3-part seed
            if captions:
                segments_ai = [
                    {"segment_type": "Introduction", "entry_point_phrase": captions[0]["text"]},
                    {"segment_type": "How-To/Lesson", "entry_point_phrase": captions[len(captions)//2]["text"]},
                    {"segment_type": "Call to Action", "entry_point_phrase": captions[-1]["text"]},
                ]
            else:
                segments_ai = [{"segment_type": "Default", "entry_point_phrase": ""}]

        # Consolidate/clean
        segments_final = consolidate_segments(segments_ai, captions, max_segments=args.max_segments, min_spacing_s=args.min_spacing_s)
        consolidated_preview_path.write_text(json.dumps({"segments": segments_final}, indent=2), encoding="utf-8")

        print(f"[+] Planned {len(segments_final)} segments ({'AI' if used_ai else 'fallback'} + consolidation).")

        out = {
            "project_plan": [
                {
                    "take_number": "1",
                    "proxy_filename": "subtitle 1.srt",
                    "braw_filename": "subtitle 1.srt",
                    "segments": segments_final
                }
            ]
        }
        out_path = working_root / "41_production_order.json"
        out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"[+] Wrote production order -> {out_path}")
        print(f"[i] Debug artifacts:\n    - {prompt_log_path}\n    - {raw_json_path}\n    - {consolidated_preview_path}")
        success_flag.touch()

    except Exception as e:
        print(f"[!] An error occurred: {e}")
        fail_flag.touch()
        raise

if __name__ == "__main__":
    main()
