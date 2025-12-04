#!/usr/bin/env python3
# 010_final_review_doer.py
import argparse
import json
import logging
import shutil
import sys
import warnings
from pathlib import Path

# --- Gemini / Vertex imports ---
from google import genai
from google.genai import types

warnings.filterwarnings("ignore", message="Field name .* shadows an attribute in parent.*", category=UserWarning)

# --- Logging Setup ---
script_dir = Path(__file__).parent
log_file_path = script_dir / "010_final_review_doer.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

# -------------------------------
# NEW: Self-contained Gemini client class
# (Copied from 230_aroll_notes_for_editor.py)
# -------------------------------
class GeminiClient:
    def __init__(self, project_id: str, location: str, model_name: str):
        logging.info("[i] Initializing Vertex-integrated Gemini client...")
        # Ensure GOOGLE_APPLICATION_CREDENTIALS is set in your environment
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
                max_output_tokens=16000,
                safety_settings=self.safety_settings,
                thinking_config=types.ThinkingConfig(thinking_budget=8000),
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
                logging.warning(" [!] Gemini API Warning: Received a null or empty response.text.")
                logging.warning(f" --- RAW API RESPONSE DUMP ---\n{response}\n --- END RAW DUMP ---")
                return None
            
            return json.loads(response.text)

        except json.JSONDecodeError as e:
            logging.error(f" [!] JSON Decode Error: Could not parse Gemini's response. Error: {e}")
            if response:
                logging.error(f" --- RAW API TEXT DUMP ON JSON ERROR ---\n{response.text}\n --- END RAW DUMP ---")
            return None
        except Exception as e:
            logging.error(f" [!] Gemini API Error: {e}")
            if response:
                logging.error(f" --- RAW API RESPONSE DUMP ON EXCEPTION ---\n{response}\n --- END RAW DUMP ---")
            return None

# --- Helper Functions ---
def timecode_to_mmss(seconds: float) -> str:
    """Converts seconds to a MM:SS string format."""
    # This handles timecodes that might be relative to a 1-hour start (3600s)
    actual_seconds = seconds % 3600
    minutes = int(actual_seconds // 60)
    secs = int(actual_seconds % 60)
    return f"{minutes:02}:{secs:02}"

def format_pacing_plan(data: dict) -> str:
    """Turns the angle changes JSON into readable Markdown."""
    lines = ["## Pacing & Angle Change Plan", ""]
    for segment in data.get("segments", []):
        seg_start = timecode_to_mmss(segment.get("start_time", 0))
        seg_end = timecode_to_mmss(segment.get("end_time", 0))
        lines.append(f"### Segment: \"{segment['segment_type']}\" ({seg_start} - {seg_end})")
        lines.append("| Cut Time | Angle | Reason |")
        lines.append("|----------|-------|--------|")
        for change in segment.get("angle_changes", []):
            cut_time = timecode_to_mmss(change.get("absolute_cut_time", 0))
            angle = change.get("angle_choice", "N/A")
            reason = change.get("reason", "No reason provided.")
            lines.append(f"| {cut_time} | {angle} | {reason} |")
        lines.append("")
    return "\n".join(lines)

def format_broll_plan(data: dict) -> str:
    """Turns the B-Roll suggestions JSON into readable Markdown."""
    lines = ["## B-Roll Suggestion Plan", ""]
    lines.append("| Timecode | Dialogue Segment | B-Roll Suggestion | Reasoning |")
    lines.append("|----------|------------------|-------------------|-----------|")
    for item in data.get("b_roll_plan", []):
        start_time = timecode_to_mmss(item.get("timestamp_start", 0))
        dialogue = item.get("source_segment", {}).get("text", "N/A")
        prompt = item.get("video_prompt", "N/A")
        reasoning = item.get("reasoning", "N/A")
        lines.append(f"| {start_time} | *{dialogue}* | {prompt} | {reasoning} |")
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Generates the Final Cut review package for the editor.")
    parser.add_argument("--project-dir", required=True, type=Path, help="The path to the project directory in the 'completed' folder.")
    # Add Gemini config arguments
    parser.add_argument("--project_id", type=str, default="qtow-148623", help="Your Google Cloud Project ID.")
    parser.add_argument("--location", type=str, default="global", help="The GCP location for the Vertex AI endpoint.")
    parser.add_argument("--model", type=str, default="gemini-2.5-pro", help="The Gemini model to use.")
    parser.add_argument("--review-dir", type=Path, default=None, help="Override root directory for final review packages.")
    parser.add_argument("--projects-root", type=Path, default=None, help="Workspace projects root (defaults to sibling 'projects').")
    parser.add_argument("--global-lock", type=Path, help="Path to global lock file to release on success.")
    args = parser.parse_args()
    
    project_name = args.project_dir.name
    logging.info(f"--- Final Review Doer Started for {project_name} ---")

    try:
        # Define paths
        assembly_line_root = args.project_dir.parent.parent
        projects_root = (args.projects_root or (assembly_line_root / "projects")).resolve()
        review_root = (args.review_dir or (assembly_line_root / "BROLL_REVIEW")).resolve()
        review_output_dir = review_root / project_name

        logging.info("Using projects root: %s", projects_root)
        logging.info("Review output root: %s", review_root)

        source_project_dir = projects_root / project_name
        workspace = source_project_dir / "ai_working_files"
        source_fcpxml = source_project_dir / "Info.fcpxml"
        
        pacing_plan_json = workspace / "45_ai_pacing_plans/subtitle_1_changes.json"
        broll_plan_json = workspace / "46_ai_b_roll_plans/subtitle_1_broll_plan.json"
        output_memo_md = review_output_dir / "Final_Cut_Notes_for_Editor.md"
        dest_fcpxml = review_output_dir / "Info.fcpxml"

        # Ensure all source files exist
        for f in [source_fcpxml, pacing_plan_json, broll_plan_json]:
            if not f.exists():
                raise FileNotFoundError(f"Required input file not found: {f}")

        # Create output directory
        review_output_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"Created review package directory: {review_output_dir}")

        # Load and format the data
        pacing_data = json.loads(pacing_plan_json.read_text(encoding='utf-8'))
        broll_data = json.loads(broll_plan_json.read_text(encoding='utf-8'))
        
        pacing_md = format_pacing_plan(pacing_data)
        broll_md = format_broll_plan(broll_data)
        
        # --- AI Memo Generation ---
        gemini_client = GeminiClient(project_id=args.project_id, location=args.location, model_name=args.model)
        
        system_instruction = (
            "You are an expert AI Post-Production Supervisor. Your task is to write a final memo to a human video editor. "
            "The A-Roll has been locked, pacing decisions have been made, and a complete B-roll sequence has been proposed. "
            "Your memo should provide a high-level creative overview and guide the editor's final review, focusing on storytelling and impact. "
            "Your tone is collaborative, insightful, and respectful of the editor's creative authority."
        )

        user_prompt = f"""
        Please generate the "Final Cut Notes for the Editor" memo based on the provided Pacing Plan and B-Roll Plan.

        The memo must include the following sections in Markdown format:
        1.  **Opening:** A brief, positive opening acknowledging that the project has reached the final creative review stage.
        2.  **High-Level Summary:** Describe the overall narrative and emotional arc of the video as constructed by the pacing and B-roll. What story is it telling?
        3.  **Key Areas for Your Review:** This is the most important part. Analyze both plans and highlight 3-4 specific timecodes or segments that are particularly effective or deserve special attention. For each, explain *why* it's a key moment (e.g., "Note the rapid pacing at 02:15 to build tension," or "The metaphorical B-roll at 05:30 for 'unlock your future' is a powerful storytelling moment.").
        4.  **Next Steps:** Clearly state that the editor's task is to perform the final creative pass, implement or replace the B-roll, and prepare the project for color and sound.
        5.  **Closing:** A collaborative closing that empowers the editor to make the final creative choices.

        --- PACING & ANGLE CHANGE PLAN ---
        {pacing_md}

        --- B-ROLL SUGGESTION PLAN ---
        {broll_md}
        """

        response_schema = {"type": "OBJECT", "properties": {"final_notes_markdown": {"type": "STRING"}}, "required": ["final_notes_markdown"]}
        
        ai_response = gemini_client.generate_notes(system_instruction, user_prompt, response_schema)

        if not (ai_response and "final_notes_markdown" in ai_response):
            raise RuntimeError("Failed to get valid response from Gemini API.")
            
        final_memo = ai_response["final_notes_markdown"]
        
        # --- Package the Final Files ---
        output_memo_md.write_text(final_memo, encoding='utf-8')
        logging.info(f"Successfully generated and saved editor memo to {output_memo_md}")
        
        shutil.copy(source_fcpxml, dest_fcpxml)
        logging.info(f"Copied final FCPXML to review package.")
        
        logging.info(f"--- Final Review Doer for {project_name} Finished Successfully ---")
        # --- RELEASE LOCK ---
        if args.global_lock and args.global_lock.exists():
            logging.info("Releasing Global Pipeline Lock.")
            args.global_lock.unlink()

    except Exception as e:
        # NOTE: We do NOT release the lock on failure. 
        # If the project breaks here, we want the pipeline to STOP 
        # so you can investigate before it tries to eat the next folder.
        logging.error(f"Final Review Failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()