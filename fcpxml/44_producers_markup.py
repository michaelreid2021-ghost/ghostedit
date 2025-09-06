#!/usr/bin/env python3
# 44_producers_markup.py
# Purpose: Analyzes a transcribed manifest to categorize all host dialogue into
#          'final_edit_takes', 'alternate_takes', and 'rejected_takes' to create
#          a comprehensive producer's markup for editing.
# Version: 4.1 (Gap-Aware Grouping)
# - ADDED: A configurable GAP_THRESHOLD and a new rule in the prompt to instruct
#          the LLM to split final takes if it detects a pause longer than the
#          threshold between dialogue segments.

import json
import argparse
import os
import warnings
from pathlib import Path
from typing import List, Dict, Any

# Assuming project_paths.py is in the same directory or accessible
from project_paths import add_base_arg, from_args

# --- Gemini / Vertex imports ---
from google import genai
from google.genai import types

warnings.filterwarnings("ignore", message="Field name .* shadows an attribute in parent.*", category=UserWarning)

# -------------------------------
# Gemini client wrapper
# -------------------------------
class GeminiClient:
    def __init__(self, project_id: str, location: str, model_name: str):
        print("[i] Initializing Vertex-integrated Gemini client for JSON output...")
        self.client = genai.Client(vertexai=True, project=project_id, location=location)
        self.model_name = model_name
        self.safety_settings = [
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ]

    def get_structured_takes(self, system_instruction: str, user_prompt: str, response_schema: dict):
        response = None
        try:
            config = types.GenerateContentConfig(
                temperature=0.3,
                top_p=0.95,
                seed=42,
                max_output_tokens=32768,
                safety_settings=self.safety_settings,
                thinking_config=types.ThinkingConfig(thinking_budget=32768),
                system_instruction=[types.Part.from_text(text=system_instruction)],
                response_mime_type="application/json",
                response_schema=response_schema,
            )
            
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[user_prompt],
                config=config,
            )
            
            if response.text is None or not response.text.strip():
                print(" [!] Gemini API Warning: Received a null or empty response.text.")
                print(" --- RAW API RESPONSE DUMP ---"); print(response); print(" --- END RAW DUMP ---")
                return None
            
            return json.loads(response.text)

        except json.JSONDecodeError as e:
            print(f" [!] JSON Decode Error: Could not parse Gemini's response. Error: {e}")
            if response:
                print(" --- RAW API TEXT DUMP ON JSON ERROR ---"); print(response.text); print(" --- END RAW DUMP ---")
            return None
        except Exception as e:
            print(f" [!] Gemini API Error: {e}")
            if response:
                print(" --- RAW API RESPONSE DUMP ON EXCEPTION ---"); print(response); print(" --- END RAW DUMP ---")
            return None

# -------------------------------
# Helpers
# -------------------------------
def simplify_transcript(timed_transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reduces the verbose transcript object to only the essential fields for the prompt."""
    if not timed_transcript:
        return []
    
    simplified = []
    for segment in timed_transcript:
        audio_features = segment.get("audio_features", {})
        simplified.append({
            "start": segment.get("start", 0.0),
            "end": segment.get("end", 0.0),
            "text": segment.get("text", "").strip(),
            "role_hint": audio_features.get("role_hint", "unknown"),
            "silence_before_s": audio_features.get("silence_before_s"),
            "energy_peak_count": audio_features.get("energy_peak_count"),
            "energy_band": audio_features.get("energy_band"),
            "pitch_trend": audio_features.get("pitch_trend"),
        })
    return simplified

def format_transcript_for_prompt(simplified_transcript: List[Dict[str, Any]]) -> str:
    """Formats the simplified transcript into a rich, readable string for the prompt."""
    lines = []
    for segment in simplified_transcript:
        seg_start = segment.get("start", 0)
        seg_end = segment.get("end", 0)
        text = segment.get("text", "")
        
        hints = [
            f"ROLE:{segment.get('role_hint', 'N/A')}",
            f"PAUSE_BEFORE:{segment.get('silence_before_s', 'N/A')}s",
            f"ENERGY_PEAKS:{segment.get('energy_peak_count', 'N/A')}",
            f"PITCH:{segment.get('pitch_trend', 'N/A')}"
        ]
        
        hints_str = " | ".join(str(h) for h in hints)
        lines.append(f"[{seg_start:.2f}s -> {seg_end:.2f}s] ({hints_str}) - {text}")
    return "\n".join(lines)

# -------------------------------
# Main
# -------------------------------
def main(cfg):
    print("[+] Starting Producer's Markup Generator (Step 44)...")

    p = from_args(cfg)
    
    input_transcript_file = str(p.scene_manifest_enriched)
    output_markup_file = str(p.working_root / "44_producers_markup.json")
    
    # --- CONFIGURATION FOR GAP DETECTION ---
    # Any pause between segments longer than this will force a new take.
    GAP_THRESHOLD = 1.5 # in seconds
    
    print(f"[i] Reading manifest from: {input_transcript_file}")
    print(f"[i] Will write producer's markup to: {output_markup_file}")
    print(f"[i] Using a gap threshold of {GAP_THRESHOLD} seconds to split takes.")

    producers_markup_schema = {
        "type": "OBJECT",
        "properties": {
            "final_edit_takes": {
                "type": "ARRAY",
                "description": "The primary list of clean, consecutive dialogue blocks that represent the ideal final edit.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start_seconds": {"type": "NUMBER"}, "end_seconds": {"type": "NUMBER"}, "full_text": {"type": "STRING"}
                    }, "required": ["start_seconds", "end_seconds", "full_text"]
                }
            },
            "alternate_takes": {
                "type": "ARRAY",
                "description": "A list of intentionally recorded, valid alternate versions of lines that an editor could consider using.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start_seconds": {"type": "NUMBER"}, "end_seconds": {"type": "NUMBER"}, "full_text": {"type": "STRING"}
                    }, "required": ["start_seconds", "end_seconds", "full_text"]
                }
            },
            "rejected_takes": {
                "type": "ARRAY",
                "description": "A list of all flubbed lines, false starts, and other mistakes made by the host that should be excluded from the edit.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start_seconds": {"type": "NUMBER"}, "end_seconds": {"type": "NUMBER"}, "full_text": {"type": "STRING"}
                    }, "required": ["start_seconds", "end_seconds", "full_text"]
                }
            }
        },
        "required": ["final_edit_takes", "alternate_takes", "rejected_takes"]
    }

    try:
        with open(input_transcript_file, "r", encoding="utf-8") as f:
            scenes = json.load(f)
    except FileNotFoundError:
        print(f"[!] FATAL ERROR: Input file not found: {input_transcript_file}"); return
    except json.JSONDecodeError as e:
        print(f"[!] FATAL ERROR: Could not parse JSON from {input_transcript_file}. Error: {e}"); return

    if not scenes or not isinstance(scenes, list):
        print(f"[!] FATAL ERROR: The manifest '{input_transcript_file}' is empty or invalid. Halting."); return
    
    print(f"[i] Found {len(scenes)} scenes in the manifest to process.")

    gemini_client = GeminiClient(cfg.project_id, cfg.location, cfg.model)

    system_instruction = (
        "You are an expert video editor's assistant acting as a producer. Your task is to analyze a raw transcript containing host dialogue and director chatter. "
        "You must meticulously categorize every segment of the host's dialogue into one of three lists: 'final_edit_takes', 'alternate_takes', or 'rejected_takes'. Your analysis provides the blueprint for the final edit."
    )
    
    # MODIFIED: Rewritten user prompt with new gap detection rule
    user_request_template = (
        "Your goal is to categorize all host dialogue into three lists. Follow these rules with precision:\n\n"
        "1.  **`final_edit_takes` (The Main Edit):**\n"
        "    - This list represents the single, perfect path through the script.\n"
        "    - If the host repeats a line to fix a mistake, ONLY the FINAL successful attempt goes here.\n"
        "    - Group consecutive, successful paragraphs into a single, continuous take.\n"
        f"   - **Split on Long Pauses**: The time between one segment's end and the next segment's start is a gap. If this gap is GREATER THAN {GAP_THRESHOLD} seconds, you MUST end the current take and start a new one. Do not group segments across long gaps.\n\n"
        "2.  **`alternate_takes` (Editor Options):**\n"
        "    - If the host intentionally records a DIFFERENT but VALID version of a line (e.g., two different intros), place the non-primary versions here.\n"
        "    - These are not mistakes; they are creative options for the editor.\n\n"
        "3.  **`rejected_takes` (The Cutting Room Floor):**\n"
        "    - Place all mistakes here: flubbed lines, false starts, coughs, stumbles, etc.\n"
        "    - If a line is repeated to fix an error, all the FAILED attempts go into this list.\n\n"
        "4.  **General Rules:**\n"
        "    - **Ignore Director Chatter**: Completely ignore all lines with `ROLE:director_chatter` or `ROLE:director_cue`.\n"
        "    - **Use Hints**: The metadata hints (ROLE, PAUSE_BEFORE, etc.) are critical for your decisions.\n"
        "    - **Timestamps**: For any given take, `start_seconds` must be the start time of the FIRST segment in the take. `end_seconds` must be the end time of the LAST segment in the take.\n\n"
        "**Transcript for Analysis:**\n{timed_transcript}"
    )

    for scene in scenes:
        is_multicam = scene.get('is_multicam', False)
        identifier_filename = scene.get('proxy_filename') if is_multicam else (scene.get("source_filename") or scene.get("braw_filename"))
        timed_transcript = scene.get("timed_transcript")

        if not identifier_filename or not timed_transcript:
            print(f" -> Skipping scene due to missing identifier or transcript. Data: { {k: v for k, v in scene.items() if k != 'timed_transcript'} }")
            continue

        print(f"\n[+] Analyzing take: {identifier_filename}...")
        
        simplified_transcript = simplify_transcript(timed_transcript)
        transcript_text = format_transcript_for_prompt(simplified_transcript)
        user_prompt = user_request_template.format(timed_transcript=transcript_text)

        markup_data = gemini_client.get_structured_takes(system_instruction, user_prompt, producers_markup_schema)

        if markup_data and isinstance(markup_data, dict) and all(k in markup_data for k in ["final_edit_takes", "alternate_takes", "rejected_takes"]):
            scene["producers_markup"] = markup_data
            num_final = len(markup_data.get("final_edit_takes", []))
            num_alt = len(markup_data.get("alternate_takes", []))
            num_rej = len(markup_data.get("rejected_takes", []))
            print(f"  -> Successfully generated markup: {num_final} final, {num_alt} alternate, {num_rej} rejected takes.")
        else:
            scene["producers_markup"] = {"error": "Markup generation failed or returned invalid format.", "final_edit_takes": [], "alternate_takes": [], "rejected_takes": []}
            print(f"  -> [!] Failed to generate producer's markup for {identifier_filename}.")

    with open(output_markup_file, "w", encoding="utf-8") as f:
        json.dump(scenes, f, indent=2)
    
    print(f"\n[+] Success! Producer's markup file saved to '{output_markup_file}'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a producer's markup from a video transcript.", allow_abbrev=False)
    add_base_arg(parser)
    parser.add_argument("--project_id", type=str, default="qtow-148623")
    parser.add_argument("--location", type=str, default="global")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro")
    config = parser.parse_args()
    main(config)