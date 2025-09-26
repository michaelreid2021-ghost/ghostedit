#!/usr/bin/env python3
# 230_aroll_notes_for_editor.py
# Purpose: Reads the producer's markup (final, alternate, rejected takes) and uses
#          an LLM to generate a high-level summary memo for the human editor,
#          providing context on the A-Roll assembly.
# Version: 1.2 (Corrected Argparse)

import json
import argparse
import os
import warnings
from pathlib import Path
from typing import List, Dict, Any

# --- MODIFIED: Removed add_base_arg, it was causing issues ---
from project_paths import from_args, ProjectPaths

# --- Gemini / Vertex imports ---
from google import genai
from google.genai import types

warnings.filterwarnings("ignore", message="Field name .* shadows an attribute in parent.*", category=UserWarning)

# -------------------------------
# Gemini client wrapper
# -------------------------------
class GeminiClient:
    def __init__(self, project_id: str, location: str, model_name: str):
        print("[i] Initializing Vertex-integrated Gemini client for editor notes...")
        self.client = genai.Client(vertexai=True, project=project_id, location=location)
        self.model_name = model_name
        self.safety_settings = [
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ]

    def generate_notes(self, system_instruction: str, user_prompt: str, response_schema: dict):
        response = None
        try:
            config = types.GenerateContentConfig(
                temperature=0.4,
                top_p=0.95,
                seed=42,
                max_output_tokens=8192,
                safety_settings=self.safety_settings,
                thinking_config=types.ThinkingConfig(thinking_budget=4096),
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
def prepare_summary_for_prompt(scenes: List[Dict[str, Any]]) -> str:
    """Consolidates markup data into a readable summary for the LLM prompt."""
    summary_lines = []
    total_final, total_alt, total_rej = 0, 0, 0

    for scene in scenes:
        markup = scene.get("producers_markup", {})
        final_takes = markup.get("final_edit_takes", [])
        alt_takes = markup.get("alternate_takes", [])
        rejected_takes = markup.get("rejected_takes", [])
        
        num_final = len(final_takes)
        num_alt = len(alt_takes)
        num_rej = len(rejected_takes)

        total_final += num_final
        total_alt += num_alt
        total_rej += num_rej

        take_id = scene.get('multicam_name') or scene.get('proxy_filename', f"Scene {scene.get('scene_number')}")
        summary_lines.append(f"Source Take: {take_id}")
        summary_lines.append(f"- Selection Stats: {num_final} final, {num_alt} alternate, {num_rej} rejected dialogue segments.")
        
        if final_takes:
            summary_lines.append("- Content of Final Takes:")
            for i, take in enumerate(final_takes):
                clean_text = take.get('full_text', 'N/A').replace('\n', ' ')
                summary_lines.append(f"  - Final Segment {i+1}: \"{clean_text}\"")
        summary_lines.append("-" * 20)

    # Add a project-wide summary at the top
    project_summary = [
        "Project-Wide Summary:",
        f"- Total Final Segments Selected: {total_final}",
        f"- Total Alternate Options Provided: {total_alt}",
        f"- Total Rejected Segments (Outtakes): {total_rej}\n"
    ]
    
    return "\n".join(project_summary + summary_lines)

# -------------------------------
# Main
# -------------------------------
def main(cfg):
    print("[+] Starting A-Roll Editor Notes Generator (Step 230)...")

    if cfg.project_dir:
        print(f"[i] Using provided project directory: {cfg.project_dir}")
        p = ProjectPaths(base=cfg.project_dir, workspace=(cfg.project_dir / "ai_working_files"))
    else:
        p = from_args(cfg)
        print(f"[i] No project directory provided. Auto-detected active project: {p.base}")

    input_markup_file = p.workspace / "44_producers_markup.json"
    output_notes_file = p.workspace / "230_aroll_editor_notes.md"
    
    print(f"[i] Reading producer's markup from: {input_markup_file}")
    print(f"[i] Will write editor notes to: {output_notes_file}")

    if not input_markup_file.exists():
        print(f"[!] FATAL ERROR: Input markup file not found: {input_markup_file}"); return

    with open(input_markup_file, "r", encoding="utf-8") as f:
        scenes_data = json.load(f)

    gemini_client = GeminiClient(cfg.project_id, cfg.location, cfg.model)

    system_instruction = (
        "You are an expert AI Post-Production Supervisor. Your task is to draft a collaborative memo to a video editor. "
        "The goal of this memo is to present the initial A-roll 'clean take' selections as a proposal for the editor's review and approval. "
        "This is a critical checkpoint. The editor needs to review the selects, adjust the timing (in/out points), and sign off on the foundation "
        "BEFORE any further work like, Angle Changes, or B-roll Begins. The pipeline doesn't add Graphics, don't mention Graphics."
        "Your tone should be professional and encouraging, positioning the editor as the key decision-maker at this stage."
        "The pipeline doesn't add Graphics, don't mention Graphics."
    )

    
    summary_text = prepare_summary_for_prompt(scenes_data)
    
    user_prompt = (
        "Based on the following production summary and selected final takes, please generate the editor's review memo. Structure it exactly as follows:\n\n"
        "1.  **Opening:** Start with a brief, collaborative opening. Frame the assembly not as 'complete,' but as a 'proposed sequence' or 'initial selects ready for review'.\n"
        "2.  **Selection Overview:** From the 'Project-Wide Summary' section of the data below, extract and state the total number of final, alternate, and rejected segments to give the editor context on the selection effort.\n" 
        "3.  **Proposed Narrative Flow:** Describe the story constructed from the final takes. Explain the beginning, middle, and end, and state the core message.\n"
        "4.  **Key Decision Points for Your Review:** Highlight specific areas needing the editor's expert opinion, such as available alternate takes or performance notes on specific clips.\n"
        "5.  **Next Steps:** This is the most important section. State clearly that the editor's primary task is to review the selects and fine-tune the in/out points. Emphasize that their approval is the crucial next step needed to lock the A-roll and proceed Angle Changes and  proposing B-roll\n"
        "6.  **Closing:** End with a concise, positive closing that awaits their feedback.\n\n"
        "**Production Summary Data:**\n\n"
        f"{summary_text}"
    )

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "editor_notes": {
                "type": "STRING",
                "description": "A markdown-formatted summary memo for the human editor, covering the key points requested."
            }
        },
        "required": ["editor_notes"]
    }

    ai_response = gemini_client.generate_notes(system_instruction, user_prompt, response_schema)

    if ai_response and "editor_notes" in ai_response:
        editor_notes = ai_response["editor_notes"]
        with open(output_notes_file, "w", encoding="utf-8") as f:
            f.write(editor_notes)
        print(f"\n[+] Success! Editor notes memo saved to '{output_notes_file}'.")
        print("\n--- Generated Notes ---")
        print(editor_notes)
        print("-----------------------")
    else:
        print("\n[!] Failed to generate valid editor notes from the model.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate AI-powered notes for the editor based on the producer's markup.")
    
    # --- MODIFIED: Explicitly define --project-dir as optional ---
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="The path to the project directory. If not provided, the script will attempt to find the active project automatically."
    )
    # --- END MODIFICATION ---

    parser.add_argument("--project_id", type=str, default="qtow-148623", help="Your Google Cloud Project ID.")
    parser.add_argument("--location", type=str, default="global", help="The GCP location for the Vertex AI endpoint.")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro", help="The Gemini model to use.")
    config = parser.parse_args()
    main(config)