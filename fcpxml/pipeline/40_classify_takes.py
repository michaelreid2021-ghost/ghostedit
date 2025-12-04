#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: 40_paper_edit_outline.py
Version: 1.1.1
Last Modified: 2025-08-23
Purpose: Use Gemini to generate a high-level paper edit from all transcripts (flexible, taxonomy-aware).
         Adds narrative style labeling, topic boundary detection, series-context flags, and keeps
         the original video_sequence contract for downstream compatibility.
"""

import json
import argparse
import os
import re
import warnings
from pathlib import Path
from project_paths import add_base_arg, from_args, get_active_project_name
from token_logger import log_token_usage

# --- Imports and GeminiClient Class ---
from google import genai
from google.genai import types
warnings.filterwarnings("ignore", message="Field name .* shadows an attribute in parent.*", category=UserWarning)


class GeminiClient:
    """A client to handle interaction using the genai.Client(vertexai=True) method."""
    def __init__(self, project_id: str, location: str, model_name: str):
        print("[i] Initializing Vertex-integrated Gemini client...")
        self.client = genai.Client(vertexai=True, project=project_id, location=location)
        self.model = model_name
        self.safety_settings = [
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ]

    def query(self, system_instruction: str, user_prompt: str, response_schema: dict):
        """Sends a query and gets a structured JSON response."""
        try:
            config = types.GenerateContentConfig(
                temperature=0.1,
                top_p=0.55,
                seed=42,
                max_output_tokens=16000,
                safety_settings=self.safety_settings,
                system_instruction=[types.Part.from_text(text=system_instruction)],
                response_mime_type="application/json",
                response_schema=response_schema,
            )
            contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]
            response = self.client.models.generate_content(
                model=f"{self.model}", contents=contents, config=config
            )
            return response
        except Exception as e:
            print(f" [!] Gemini API Error: {e}")
            return None


def parse_take_from_filename(filename: str) -> str | None:
    """Extracts the take number (e.g., '02') from the filename."""
#-  match = re.search(r"(\d+)\.(mp4|mov|braw)$", filename, re.IGNORECASE)
    # This new pattern looks for a space or underscore, captures the digits,
    # and allows for other characters (like _HD) before the file extension.
    match = re.search(r"[\s_](\d+)[^\.]*\.(?:mp4|mov|braw)$", filename, re.IGNORECASE)
    return match.group(1).zfill(2) if match else None

def build_system_prompt() -> str:
    styles = (
        "Motivational-Explainer, Educational-Explainer, Testimonial-Narrative, "
        "Workshop/Lesson, Sermon-like, Academic-Lecture, PSA/Announcement, Sales/Promo, "
        "Documentary-Host, Coaching-Dialogue, NOVEL"
    )
    purposes = (
        "Hook (opener/cold-open); Introduction (who/what/why); Roadmap (we'll cover...); "
        "Disclaimer (legal/safety/sponsorship/scope note); "
        "Problem Statement (pain/need); Objection/Anxiety (e.g., 'I'm too old/too late'); "
        "Reframe/Encouragement (e.g., 'you're right on time'); "
        "Solution/Value (what this gives, as opposed to steps/tactics); "
        "Proof (authority/examples/testimonials); How-To/Lesson (steps/tactics, as opposed to broad outline); "
        "Story/Example (anecdote); Transition (move to next topic); "
        "Call to Action (do/try/next step); Outro/Thanks"
    )
    return (
        "You are an expert video story editor and taxonomy tagger for narrative structure. "
        "When analyzing any transcript:\n"
        f"- Name the overall message style (one label) chosen from: {styles}.\n"
        "- Use an industry lexicon of segment purposes. A segment may be omitted if absent; never force categories. "
        f"Preferred purposes (aliases allowed): {purposes}.\n"
        "- Detect topic boundaries. Mark natural topic changes (title + one-sentence rationale). Do not over-segment; "
        "3-7 boundaries max per take.\n"
        "- If script implies multiple parts/episodes, mark series context (e.g., 'Series context: yes; part: 1 of N (unknown)').\n"
        "- Be selective. If a purpose isn't present, exclude it. If a style doesn't fit, return NOVEL.\n"
        "- No hallucinations. Only use content present in the transcripts; do not invent quotes or takes.\n"
        "Finally, produce the final paper edit sequence."
    )


def build_user_prompt_header() -> str:
    return (
        "Here are all the available takes from the shoot. Analyze them and create the final video sequence.\n\n"
        "Task additions:\n"
        "1) First, output a one-line message style label for the overall piece.\n"
        "2) For each take, list only the purposes that actually appear (use the provided lexicon; omit if absent).\n"
        "3) Mark topic boundaries in each take (up to 7) with a short title and why it changes.\n"
        "4) If multiple-part language appears (e.g., 'Next time we'll...'), note Series context.\n"
        "5) Then produce the final video_sequence choosing the minimal, strongest set of parts; it's OK to omit categories.\n"
        "6) For each item in video_sequence, also include optional fields: "
        "'anchor_quote' (5-12 exact words from the take) and 'why' (one-sentence rationale). "
        "Avoid two identical segment_type values in a row; if the second is procedural, use 'How-To/Lesson'.\n\n"
    )


def build_response_schema() -> dict:
    # Extended schema while keeping original video_sequence contract intact (only take_number and segment_type required).
    return {
        "type": "OBJECT",
        "properties": {
            "message_style": {
                "type": "STRING",
                "description": "Overall message style label for the piece. One of: Motivational-Explainer, Educational-Explainer, Testimonial-Narrative, Workshop/Lesson, Sermon-like, Academic-Lecture, PSA/Announcement, Sales/Promo, Documentary-Host, Coaching-Dialogue, NOVEL."
            },
            "style_confidence": {
                "type": "NUMBER",
                "description": "Confidence 0..1 for the style label."
            },
            "series_context": {
                "type": "OBJECT",
                "description": "Flags/notes if the script implies a multi-part series.",
                "properties": {
                    "present": {"type": "BOOLEAN"},
                    "note": {"type": "STRING", "description": "Short explanation if present."},
                    "part_hint": {"type": "STRING", "description": "If hinted, e.g., '1 of N (unknown)'."}
                },
                "required": ["present"]
            },
            "takes_analysis": {
                "type": "ARRAY",
                "description": "Per-take analysis for purposes and topic boundaries.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "take_number": {"type": "STRING"},
                        "purposes_present": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "Subset of the lexicon purposes actually present in this take."
                        },
                        "topic_boundaries": {
                            "type": "ARRAY",
                            "description": "Up to 7 topic boundaries in order.",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "title": {"type": "STRING"},
                                    "reason": {"type": "STRING"}
                                },
                                "required": ["title", "reason"]
                            }
                        }
                    },
                    "required": ["take_number"]
                }
            },
            "video_sequence": {
                "type": "ARRAY",
                "description": "The final, ordered sequence of takes that should be used to construct the video.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "take_number": {
                            "type": "STRING",
                            "description": "The number of the take to be used for this part of the sequence (e.g., '02', '12')."
                        },
                        "segment_type": {
                            "type": "STRING",
                            "description": "Choose from the lexicon: Hook; Introduction; Roadmap; Disclaimer; Problem Statement; Objection/Anxiety; Reframe/Encouragement; Solution/Value; Proof; How-To/Lesson; Story/Example; Transition; Call to Action; Outro/Thanks."
                        },
                        "anchor_quote": {
                            "type": "STRING",
                            "description": "Optional: 5-12 exact words pulled from the chosen take."
                        },
                        "why": {
                            "type": "STRING",
                            "description": "Optional: one-sentence rationale for picking this take for this purpose."
                        }
                    },
                    "required": ["take_number", "segment_type"]
                }
            }
        },
        "required": ["video_sequence"]
    }


# --- MAIN ORCHESTATION for Paper Edit Generation ---
def main(cfg):
    print("Program Loaded - No Import Errors")
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
        print("[+] Starting AI Paper Edit Generation Pass (High Outline)...")
        gemini_client = GeminiClient(
            project_id=cfg.project_id, location=cfg.location, model_name=cfg.model
        )
        print(f"input file: {cfg.input_file}")
        if not os.path.exists(cfg.input_file):
            print(f"[!] ERROR: Input file not found: {cfg.input_file}")
            raise FileNotFoundError(f"Input file not found: {cfg.input_file}")

        with open(cfg.input_file, "r", encoding="utf-8") as f:
            scenes = json.load(f)

        # 1) Consolidate transcripts, using 'proxy_filename' to determine take_number.
        transcripts_by_take: dict[str, str] = {}
        for scene in scenes:
            proxy_filename = scene.get("proxy_filename", "") or ""
            if "CAM 1" in proxy_filename:  # Only need one camera's transcript
                take_number = parse_take_from_filename(proxy_filename)
                transcript_text = scene.get("transcript") or ""
                if take_number and transcript_text:
                    transcripts_by_take[take_number] = transcript_text

        # 2) Build prompts
        system_prompt = build_system_prompt()
        consolidated_prompt = build_user_prompt_header()
        for take_number, transcript in sorted(transcripts_by_take.items(), key=lambda kv: kv[0]):
            consolidated_prompt += f"--- TAKE {take_number} TRANSCRIPT ---\n{transcript}\n\n"

        
        # 3) Define output schema (extended)
        paper_edit_schema = build_response_schema()

#-      # --- START: Added Debug Logging ---
#-      debug_prompt_path = p.working_root / "40_debug_prompt_sent_to_api.txt"
#-      try:
#-          debug_prompt_path.write_text(consolidated_prompt, encoding="utf-8")
#-          print(f"[i] DEBUG: Full prompt saved to -> {debug_prompt_path}")
#-      except Exception as e:
#-          print(f"[!] WARNING: Could not write debug prompt file: {e}")
#-      # --- END: Added Debug Logging ---

        # --- START: Console Output of Full Prompt ---
        print("\n" + "="*80)
        print("--- PROMPT BEING SENT TO GEMINI API (CONSOLE OUTPUT) ---")
        print(system_prompt)
        print("-------------")
        print(consolidated_prompt)
        print("--- END OF PROMPT ---")
        print("="*80 + "\n")
        # --- END: Console Output of Full Prompt ---

        print("[i] Sending all transcripts to AI for high-level analysis...")

        # 4) Make the single API call
        response = gemini_client.query(
            system_instruction=system_prompt,
            user_prompt=consolidated_prompt,
            response_schema=paper_edit_schema
        )

        # 5) Save the output
        if response and response.text:
            paper_edit_json = json.loads(response.text)
            if response.usage_metadata:
                log_token_usage(
                    project_name=get_active_project_name(),
                    script_name=Path(__file__).name,
                    model_name=cfg.model,
                    prompt_token_count=response.usage_metadata.prompt_token_count,
                    candidates_token_count=response.usage_metadata.candidates_token_count,
                    total_token_count=response.usage_metadata.total_token_count
                )

            out_path = getattr(cfg, "out_file", None)
            if not out_path:
                out_path = str((p.working_root / "40_high_level_plan.json"))
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(paper_edit_json, f, indent=2, ensure_ascii=False)
            print(f"\n[+] Success! High-level plan saved to: {out_path}")
            print(json.dumps(paper_edit_json, indent=2, ensure_ascii=False))
            success_flag.touch()
        else:
            print("\n[!] Failed to generate a paper edit from the model.")
            raise Exception("Failed to generate paper edit from model")

    except Exception as e:
        print(f"[!] An error occurred: {e}")
        fail_flag.touch()
        raise



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Use Gemini to generate a high-level paper edit from all transcripts (taxonomy-aware).")
    add_base_arg(parser)
    parser.add_argument("--project_id", type=str, default="qtow-148623", help="Your Google Cloud Project ID.")
    parser.add_argument("--location", type=str, default="global", help="The GCP location for the Vertex AI endpoint.")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro", help="The Gemini model to use.")
    parser.add_argument("--input_file", type=str, default=None, help="Input JSON with all transcripts.")
    parser.add_argument("--out_file", type=str, default=None, help="Output JSON for the high-level plan.")
    config = parser.parse_args()

    if config.input_file is None:
        config.input_file = str(from_args(config).scene_manifest_enriched)
    main(config)
