#!/usr/bin/env python3
# 44_producers_markup.py
# Purpose: Merges a script library into a scene manifest, populating 'final_edit_takes'.
# Version: 2.3 (Windowed Search - Triplet Fix)
# - MODIFIED: Replaced brittle split logic with "Look-Ahead Window" (MAX_WINDOW_SIZE=3).
# - MODIFIED: Can now find phrases spanning up to 3 consecutive transcript segments.

import json
import argparse
from pathlib import Path
import re
from typing import List, Dict, Any
from copy import deepcopy
import difflib

# --- Pipeline Imports ---
from project_paths import add_base_arg, from_args

# ==============================================================================
# CONFIGURATION
# ==============================================================================
USE_FUZZY_MATCHING = False  # Keep False unless you hit a transcription disaster
MAX_WINDOW_SIZE = 3         # Look ahead up to 3 segments (Solves triplets)
# ==============================================================================

# -------------------------------
# Helper Functions
# -------------------------------

def normalize_text(text: str) -> str:
    """A consistent normalization function for matching."""
    text = text.lower()
    text = text.strip()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text

def is_fuzzy_match(target: str, candidate: str, threshold: float = 0.85) -> bool:
    """Checks if 'target' is roughly inside 'candidate'."""
    if len(target) > len(candidate): 
        return False
    if target in candidate:
        return True
    s = difflib.SequenceMatcher(None, target, candidate)
    match = s.find_longest_match(0, len(target), 0, len(candidate))
    if match.size / len(target) >= threshold:
        return True
    return False

def merge_segments_into_sentences(timed_transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Joins fragmented segments based on punctuation for initial granularization.
    """
    if not timed_transcript:
        return []

    merged_segments: List[Dict[str, Any]] = []
    current_merged_segment: Dict[str, Any] = None
    PUNCTUATION = ('.', '?', '!')

    for segment in timed_transcript:
        clean_segment = {
            "start": segment.get("start"),
            "end": segment.get("end"),
            "text": segment.get("text", "").strip(),
            "role_hint": segment.get("audio_features", {}).get("role_hint")
        }
        
        if not clean_segment["text"]:
            continue

        if current_merged_segment is None:
            current_merged_segment = deepcopy(clean_segment)
        else:
            last_text = current_merged_segment.get("text", "").strip()
            if last_text and not last_text.endswith(PUNCTUATION):
                current_merged_segment["text"] += " " + clean_segment["text"]
                current_merged_segment["end"] = clean_segment["end"]
            else:
                merged_segments.append(current_merged_segment)
                current_merged_segment = deepcopy(clean_segment)
    
    if current_merged_segment:
        merged_segments.append(current_merged_segment)
    
    return merged_segments

def merge_consecutive_takes(takes: List[Dict[str, Any]], gap_threshold: float) -> List[Dict[str, Any]]:
    if not takes:
        return []

    merged_takes: List[Dict[str, Any]] = []
    current_merged_take = deepcopy(takes[0])

    for next_take in takes[1:]:
        gap = next_take["start_seconds"] - current_merged_take["end_seconds"]
        if gap <= gap_threshold:
            current_merged_take["full_text"] += " " + next_take["full_text"]
            current_merged_take["end_seconds"] = next_take["end_seconds"]
        else:
            merged_takes.append(current_merged_take)
            current_merged_take = deepcopy(next_take)
    
    merged_takes.append(current_merged_take)
    return merged_takes

# -------------------------------
# Main
# -------------------------------

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
        print("[+] Starting Producers Markup Generator (Step 44)...")
        
        library_file = p.working_root / "43_script_library.json"
        manifest_file = p.scene_manifest_enriched
        output_file = p.working_root / "44_producers_markup.json"
        not_found_file = p.workspace / "44_raw_missed_phrases.txt"

        print(f"[i] Using library: {library_file.name}")
        print(f"[i] Using manifest: {manifest_file.name}")
        print(f"[i] Matching Window Size: {MAX_WINDOW_SIZE} segments")

        GAP_THRESHOLD = 1.5 

        # --- Load Inputs ---
        try:
            script_library = json.loads(library_file.read_text(encoding="utf-8"))
            script_phrases = script_library.get("script", [])
            if not script_phrases:
                raise ValueError(f"'{library_file.name}' is empty or has no 'script' key.")
        except Exception as e:
            print(f"[!] FATAL: Could not read library file: {e}")
            raise

        try:
            scenes = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] FATAL: Could not read manifest file: {e}")
            raise

        print(f"[+] Loaded {len(script_phrases)} phrases from library.")
        print(f"[+] Loaded {len(scenes)} scenes from manifest.")

        # --- Structure Setup ---
        output_scenes = deepcopy(scenes)
        scene_map: Dict[str, Dict[str, Any]] = {}
        
        for scene in output_scenes:
            if 'producers_markup' not in scene:
                scene['producers_markup'] = {}
            scene['producers_markup']['final_edit_takes'] = []
            scene['producers_markup']['alternate_takes'] = []
            scene['producers_markup']['rejected_takes'] = []

            proxy_name = scene.get('proxy_filename')
            if proxy_name: scene_map[proxy_name] = scene
            for s_file in scene.get('source_files', []):
                s_filename = s_file.get('source_filename')
                if s_filename: scene_map[s_filename] = scene

        # --- Process Phrases with WINDOW SEARCH ---
        print("[i] Processing phrases...")
        
        best_matches: Dict[str, Dict[str, Any]] = {} 
        
        for item in script_phrases:
            phrase = item.get("original_phrase") or item.get("phrase")
            if not phrase: continue
                
            phrase_norm = normalize_text(phrase)
            identifier_str = item.get("identifer_filenames", "")
            target_filenames = [f.strip() for f in identifier_str.split(',') if f.strip()]
            
            target_scenes = []
            for fname in target_filenames:
                if fname in scene_map:
                    s = scene_map[fname]
                    if s not in target_scenes: target_scenes.append(s)

            if not target_scenes: continue
            target_scenes.sort(key=lambda s: s.get('scene_number', 0))

            latest_match_info = None

            for scene in target_scenes:
                timed_transcript = scene.get("timed_transcript", [])
                merged_segments = merge_segments_into_sentences(timed_transcript)
                
                # --- Windowed Search Logic ---
                # Iterate through segments, looking ahead by MAX_WINDOW_SIZE
                for i in range(len(merged_segments)):
                    
                    # Build the look-ahead window
                    window_text_parts = []
                    window_end_seg_index = i
                    
                    # Construct combined text for i, i+1, i+2...
                    for k in range(MAX_WINDOW_SIZE):
                        if i + k < len(merged_segments):
                            window_text_parts.append(merged_segments[i+k].get("text", ""))
                            window_end_seg_index = i + k
                        else:
                            break
                    
                    window_full_text = " ".join(window_text_parts)
                    window_norm = normalize_text(window_full_text)

                    # Check Match
                    is_match = False
                    if phrase_norm in window_norm:
                        is_match = True
                    elif USE_FUZZY_MATCHING and is_fuzzy_match(phrase_norm, window_norm, 0.85):
                        is_match = True
                    
                    if is_match:
                        # We found it starting at segment i and ending somewhere in the window
                        # Construct the result based on the window span
                        start_seg = merged_segments[i]
                        end_seg = merged_segments[window_end_seg_index]
                        
                        # Optimization: If the match is strictly inside the first segment, we don't need to span
                        # But since we are matching normalized strings, calculating precise cutoff is hard.
                        # Safest bet: Take the start of the first segment and end of the last segment in window.
                        # However, if the phrase is short and the window is huge, we might grab too much tail.
                        # Refinement: Check if it fits in a smaller sub-window first.
                        
                        final_end_seg = start_seg
                        # Try sub-windows to find minimal span
                        for k in range(MAX_WINDOW_SIZE):
                            if i + k >= len(merged_segments): break
                            sub_parts = [merged_segments[i+j].get("text", "") for j in range(k+1)]
                            sub_norm = normalize_text(" ".join(sub_parts))
                            
                            sub_match = (phrase_norm in sub_norm) or \
                                        (USE_FUZZY_MATCHING and is_fuzzy_match(phrase_norm, sub_norm, 0.85))
                            
                            if sub_match:
                                final_end_seg = merged_segments[i+k]
                                break

                        match_entry = {
                            "start": start_seg.get("start"),
                            "end": final_end_seg.get("end"),
                            "text": phrase # Use the library phrase as the canonical text
                        }
                        latest_match_info = {"segment": match_entry, "scene": scene}
                        # Don't break; keep looking for later takes (Last Match Wins)

            if latest_match_info:
                best_matches[phrase] = latest_match_info

        # --- Populate Scenes ---
        found_count = 0
        for phrase, match_info in best_matches.items():
            scene_obj = match_info["scene"]
            seg = match_info["segment"]

            entry = {
                "start_seconds": seg.get("start"),
                "end_seconds": seg.get("end"),
                "full_text": seg.get("text")
            }
            
            existing = [t.get("start_seconds") for t in scene_obj['producers_markup']['final_edit_takes']]
            if entry["start_seconds"] not in existing:
                scene_obj['producers_markup']['final_edit_takes'].append(entry)
            
            found_count += 1
        
        # --- Report Misses ---
        not_found = []
        for item in script_phrases:
            p_text = item.get("original_phrase") or item.get("phrase")
            if p_text and p_text not in best_matches:
                not_found.append(p_text)

        # --- Sort & Merge ---
        print("[i] Merging 'final_edit_takes'...")
        for scene in output_scenes:
            takes = scene['producers_markup']['final_edit_takes']
            if takes:
                takes.sort(key=lambda t: t.get('start_seconds', 0))
                scene['producers_markup']['final_edit_takes'] = merge_consecutive_takes(takes, GAP_THRESHOLD)

        print("-" * 30)
        print(f"  > Matched: {found_count} / {len(script_phrases)}")
        print(f"  > Missed: {len(not_found)}")

        # --- Save ---
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_scenes, f, indent=2)
        print(f"[+] Saved: {output_file.name}")
            
        with open(not_found_file, "w", encoding="utf-8") as f:
            if not_found:
                f.write("# --- Phrases Not Found ---\n")
                for p_txt in sorted(list(set(not_found))):
                    f.write(f"{p_txt}\n")
            else:
                f.write("All phrases matched.")
        print(f"[+] Saved misses: {not_found_file.name}")

        print("[+] Script 44 finished successfully.")
        success_flag.touch()

    except Exception as e:
        print(f"[!] Error in {script_name}: {e}")
        fail_flag.touch()
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_base_arg(parser)
    main(parser.parse_args())