#!/usr/bin/env python3
# 44_find_clean_takes.py
# Purpose: Analyzes a transcribed manifest to identify the start and end times
#          of clean, usable takes, ignoring director chatter and flubs.
#          It simplifies the transcript input for the AI, but enriches it
#          with key audio features to improve decision-making.
# Version: 3.0 (Enriched and Simplified Input)

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
                temperature=0.2,
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
        text = segment.get("text", "")
        
        # Build a string of the metadata hints
        hints = [
            f"ROLE:{segment.get('role_hint', 'N/A')}",
            f"PAUSE_BEFORE:{segment.get('silence_before_s', 'N/A')}s",
            f"ENERGY_PEAKS:{segment.get('energy_peak_count', 'N/A')}",
            f"PITCH:{segment.get('pitch_trend', 'N/A')}"
        ]
        
        hints_str = " | ".join(str(h) for h in hints)
        lines.append(f"[{seg_start:.2f}s] ({hints_str}) - {text}")
    return "\n".join(lines)

# -------------------------------
# Main
# -------------------------------
def main(cfg):
    print("[+] Starting Clean Take Finder with Enriched Metadata (Step 44)...")

    p = from_args(cfg)
    
    input_transcript_file = str(p.scene_manifest_enriched)
    output_takes_file = str(p.working_root / "44_clean_takes.json")
    
    print(f"[i] Reading manifest from: {input_transcript_file}")
    print(f"[i] Will write clean takes list to: {output_takes_file}")

    clean_takes_schema = {
        "type": "OBJECT",
        "properties": {
            "clean_takes": {
                "type": "ARRAY",
                "description": "A list of clean, usable takes delivered by the host, ignoring all director chatter and flubbed lines.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start_seconds": {"type": "NUMBER", "description": "The start time of the clean take in seconds."},
                        "end_seconds": {"type": "NUMBER", "description": "The end time of the clean take in seconds."},
                        "full_text": {"type": "STRING", "description": "The complete transcribed text of the clean take."}
                    },
                    "required": ["start_seconds", "end_seconds", "full_text"]
                }
            }
        },
        "required": ["clean_takes"]
    }

    try:
        with open(input_transcript_file, "r", encoding="utf-8") as f:
            scenes = json.load(f)
    except FileNotFoundError:
        print(f"[!] FATAL ERROR: Input file not found: {input_transcript_file}")
        return
    except json.JSONDecodeError as e:
        print(f"[!] FATAL ERROR: Could not parse JSON from {input_transcript_file}. Error: {e}")
        return

    if not scenes or not isinstance(scenes, list):
        print(f"[!] FATAL ERROR: The manifest '{input_transcript_file}' is empty or invalid. Halting.")
        return
    
    print(f"[i] Found {len(scenes)} scenes in the manifest to process.")

    gemini_client = GeminiClient(cfg.project_id, cfg.location, cfg.model)

    system_instruction = (
        "You are an expert video editor's assistant. Your single, focused task is to analyze a raw transcript containing both on-screen talent dialogue and off-screen director chatter. "
        "You must identify ONLY the clean, usable takes delivered by the on-screen talent. A take is a continuous block of successful, on-script dialogue."
    )
    
    user_request_template = (
        "Your goal is to identify every block of continuous, successful dialogue that should be part of the final edit. Follow these rules with precision:\n\n"
        "1.  **Handle Retakes of a Specific Line**: If the host repeats **substantially the same lines or phrases** due to a mistake, you must ONLY include the FINAL successful attempt of those lines in your list.\n"
        "2.  **Group Consecutive Dialogue**: If the host delivers multiple **different** paragraphs in a row without being interrupted by director chatter or flubbing a line, you MUST group them all into a single, continuous take. A natural pause between paragraphs is NOT an interruption.\n"
        "3.  **Use the Hints**: The metadata hints in parentheses for each line (ROLE, PAUSE_BEFORE, ENERGY_PEAKS, PITCH) provide critical context for making your decisions.\n"
        "4.  **Ignore Chatter**: Completely ignore all director chatter, countdowns, and false starts (`ROLE:director_chatter`, `ROLE:director_cue`, etc.).\n\n"
        "From the transcript below, generate a JSON object with a single key, `clean_takes`. The `end_seconds` should be the timestamp of the last word of the complete take.\n\n"
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
        
        # Simplify and enrich the transcript data before sending it to the model
        simplified_transcript = simplify_transcript(timed_transcript)
        
        transcript_text = format_transcript_for_prompt(simplified_transcript)
        user_prompt = user_request_template.format(timed_transcript=transcript_text)

        clean_takes_data = gemini_client.get_structured_takes(system_instruction, user_prompt, clean_takes_schema)

        if clean_takes_data and isinstance(clean_takes_data, dict) and "clean_takes" in clean_takes_data:
            scene["clean_takes_list"] = clean_takes_data
            num_takes = len(clean_takes_data.get("clean_takes", []))
            print(f"  -> Successfully identified {num_takes} clean takes for {identifier_filename}.")
        else:
            scene["clean_takes_list"] = {"error": "Clean takes generation failed or returned invalid format.", "clean_takes": []}
            print(f"  -> [!] Failed to generate clean takes for {identifier_filename}.")

    # Save the updated data structure with the new clean takes list
    with open(output_takes_file, "w", encoding="utf-8") as f:
        json.dump(scenes, f, indent=2)
    
    print(f"\n[+] Success! Clean takes file saved to '{output_takes_file}'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find clean, usable takes from a video transcript.", allow_abbrev=False)
    add_base_arg(parser)
    parser.add_argument("--project_id", type=str, default="qtow-148623")
    parser.add_argument("--location", type=str, default="global")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro")
    config = parser.parse_args()
    main(config)