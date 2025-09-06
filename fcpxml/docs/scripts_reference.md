# Scripts Reference (Python)

This document catalogs all Python scripts in this directory with their purpose, stage, typical inputs/outputs, and how to run them. Stage numbers indicate pipeline order when present.

> Tip: For most scripts, run `python <script>.py --help` to see live CLI options. For exact, current flags and subcommands, also see `cli_usage.md`.

---

## 000_pacing.py
- Stage: 000 (experimental)
- Summary: Early/experimental pacing analysis utilities and scaffolding.
- Inputs: Intermediate manifests, timing data (varies).
- Outputs: Debug metrics/logs for pacing.
- Run: `python 000_pacing.py`
- Notes: Superseded by the 45x pacing analyzers; keep for reference.

## 00_pacing.py
- Stage: 00 (experimental)
- Summary: Pacing utilities (predecessor to step 45 analyzers/tests).
- Inputs/Outputs: Similar to `000_pacing.py`.
- Run: `python 00_pacing.py`

## 01_dehydrate.py
- Stage: 01
- Summary: Dehydrates an FCPXML timeline into a template and manifests for downstream processing.
- Inputs: `Info.fcpxml` (or `--fcpxml <path>`), media present at repo root.
- Outputs: `15_dehydrated_template/`, manifests at root (e.g., `master_manifest.json`).
- Run: `python 01_dehydrate.py [--fcpxml .\26_adjusted.fcpxml]`
- Notes: Can be re-run after rebaseline (Step 25) using `--fcpxml 26_adjusted.fcpxml`.

## 03_transcription.py
- Stage: 03
- Summary: Generates transcriptions (e.g., Whisper/WhisperX integration) for multicam/takes.
- Inputs: Media files referenced by manifests; configs.
- Outputs: `scene_manifest_transcribed.json`, per-take transcripts.
- Run: `python 03_transcription.py`

## 03_reworked_transcription.py
- Stage: 03 (variant)
- Summary: Alternative implementation of transcription flow (experimentation or rework).
- Inputs/Outputs: Mirrors `03_transcription.py` with different internals.
- Run: `python 03_reworked_transcription.py`

## 04a_waveform_annotator.py
- Stage: 04a
- Summary: Extracts/annotates audio waveforms to assist detection (e.g., chatter, pacing).
- Inputs: Audio from media or intermediates.
- Outputs: Waveform annotations or sidecar JSONs.
- Run: `python 04a_waveform_annotator.py`

## 10_fcpxml_ripper.py
- Stage: 10
- Summary: Support utilities to rip/extract structures from FCPXML into usable intermediates.
- Inputs: FCPXML.
- Outputs: Extracted timelines/clips metadata.
- Run: `python 10_fcpxml_ripper.py`

## 15_enriched_manifest.py
- Stage: 15
- Summary: Builds/updates `15_enriched_manifest.json` augmenting angle IDs and resolution metadata.
- Inputs: Dehydrated manifests/resources.
- Outputs: `15_enriched_manifest.json`.
- Run: `python 15_enriched_manifest.py`

## 20_detect_chatter.py
- Stage: 20
- Summary: Detects head chatter and writes countdown offsets for each Take/Scene.
- Inputs: Transcripts, audio features; manifests.
- Outputs: `20_countdown_offsets.json`
- Run: `python 20_detect_chatter.py`
- Notes: Replaces historical step 42 in this repo family.

## 21_detect_tail_chatter.py
- Stage: 21
- Summary: Detects tail chatter/silence; companion to head chatter step.
- Inputs: Transcripts/audio features.
- Outputs: Tail trim recommendations (JSON or logs).
- Run: `python 21_detect_tail_chatter.py`

## 25_rebaseline_offsets.py
- Stage: 25
- Summary: Applies head trims from countdown offsets and ripples offsets in the baseline timeline.
- Inputs: `--fcpxml <input.fcpxml>`, `--countdown 20_countdown_offsets.json`.
- Outputs: `--outfcpxml 26_adjusted.fcpxml`.
- Run: `python 25_rebaseline_offsets.py --fcpxml .\Info.fcpxml --countdown .\20_countdown_offsets.json --outfcpxml .\26_adjusted.fcpxml`

## 25_rebaseline_offsets copy.py
- Stage: 25 (duplicate)
- Summary: Copy/variant of the rebaseline step; use the primary `25_rebaseline_offsets.py`.

## 39_chomp_me.py
- Stage: 39
- Summary: Content-tailoring utility; trims/condenses segments (internal tooling).
- Inputs/Outputs: Intermediate manifests/resources.
- Run: `python 39_chomp_me.py`

## 3c_whisperx_clean_inout.py
- Stage: 3c
- Summary: Cleans up WhisperX in/out boundaries to match content.
- Inputs: WhisperX outputs.
- Outputs: Adjusted timecodes/segments.
- Run: `python 3c_whisperx_clean_inout.py`

## 40_classify_takes.py
- Stage: 40
- Summary: Classifies Takes/Scenes from transcripts and structure.
- Inputs: Transcribed manifests, enriched metadata.
- Outputs: Classified scene manifest.
- Run: `python 40_classify_takes.py`

## 41_production_order.py
- Stage: 41
- Summary: Computes production order for takes/scenes.
- Inputs: Classified scene manifest.
- Outputs: Ordered manifest; logs.
- Run: `python 41_production_order.py`

## 42_chatter_detector.py
- Stage: 42 (legacy)
- Summary: Historical chatter detector; superseded by step 20.
- Inputs/Outputs: Similar to `20_detect_chatter.py`.
- Run: `python 42_chatter_detector.py`

## 44_producers_markup.py
- Stage: 44
- Summary: Applies producer markup notes into manifests/timelines.
- Inputs: Producer notes, manifests.
- Outputs: Marked-up intermediates; logs.
- Run: `python 44_producers_markup.py`

## 44b_producers_markup.py
- Stage: 44b
- Summary: Variant/extension of producer markup step.
- Run: `python 44b_producers_markup.py`

## 45a_pacing_analyzer.py
- Stage: 45a
- Summary: Pacing analyzer focusing on initial heuristics.
- Inputs: Transcripts, timing; manifests.
- Outputs: Pacing metrics and plans.
- Run: `python 45a_pacing_analyzer.py`

## 45b_pacing_test.py
- Stage: 45b (test)
- Summary: Test harness for pacing decisions.
- Outputs: `45_pacing_debug.log` (or similar) and console diagnostics.
- Run: `python 45b_pacing_test.py`

## 45c_pacing_test.py
- Stage: 45c (test)
- Summary: Additional pacing tests/benchmarks.
- Run: `python 45c_pacing_test.py`

## 45_pacing_analyzer.py
- Stage: 45
- Summary: Primary pacing analyzer producing angle-change plans.
- Inputs: Enriched manifests, transcripts, prior steps.
- Outputs: Plans under `45_ai_pacing_plans/` (`*_changes.json` etc.), logs.
- Run: `python 45_pacing_analyzer.py`

## 46_b_roll_suggester.py
- Stage: 46
- Summary: Suggests bâ€‘roll insertions based on transcript cues and pacing.
- Inputs: Transcripts/manifests.
- Outputs: Bâ€‘roll suggestion plan JSON.
- Run: `python 46_b_roll_suggester.py`

## 47_build_angle_changes.py
- Stage: 47
- Summary: Builds rehydrated multicam fragments from angle-change plans (with optional chatter trims).
- Inputs: `--resources 15_dehydrated_template/01_resources.xml`, `--process-clips 15_dehydrated_template/process_clips`, `--plans 45_ai_pacing_plans/*_changes.json`, `--enriched 15_enriched_manifest.json`.
- Outputs: `--outdir 15_rehydrated_template/mc_clips` (placeholder mc-clip XMLs).
- Run: `python 47_build_angle_changes.py --resources ... --process-clips ... --plans ... --outdir ... --enriched ...`
- Flags:
-  - `--fill-tail` to preserve original placeholder duration by adding a tail segment.
-  - `--apply-countdown` and `--countdown` to trim head chatter without shifting offsets.
- Notes: Keep first fragment `offset` fixed; adjust only `duration` unless skipping to the first valid fragment after trim.

## 47_fetch_submitted_veo_ops.py
- Stage: 47 (aux)
- Summary: Fetches pending/approved VEO ops (external ops queue) used by step 47.
- Outputs: Updates plans or local ops cache.
- Run: `python 47_fetch_submitted_veo_ops.py`

## 47_generate_b_roll.py
- Stage: 47 (aux)
- Summary: Generates bâ€‘roll asset tasks or placeholders from suggestions.
- Inputs: Bâ€‘roll suggestion plan.
- Outputs: Bâ€‘roll asset manifest/placeholders.
- Run: `python 47_generate_b_roll.py`

## 47_make_broll_placeholders.py
- Stage: 47 (aux)
- Summary: Creates placeholder clips for bâ€‘roll to be filled later.
- Outputs: Placeholder XML/OTIO snippets.
- Run: `python 47_make_broll_placeholders.py`

## 50_final_merge.py
- Stage: 50
- Summary: Merges rehydrated fragments and assets into the final timeline (FCPXML).
- Inputs: `15_rehydrated_template/mc_clips`, resources, manifests, plans.
- Outputs: Final `ai_final_cut.fcpxml` (or equivalent) and logs.
- Run: `python 50_final_merge.py`

## 101_Angle_Edit_CommandLine_Tool.py
- Stage: 101
- Summary: Angle editing CLI utilities (legacy/high-numbered toolkit).
- Inputs/Outputs: Angle cut lists, manifests.
- Run: `python 101_Angle_Edit_CommandLine_Tool.py`

## 102_clip_editor.py
- Stage: 102
- Summary: Programmatic clip edits and transformations.
- Run: `python 102_clip_editor.py`

## 103-reflow-and-more.py
- Stage: 103
- Summary: Reflows timeline segments and performs batch clip operations.
- Run: `python 103-reflow-and-more.py`

## 104_split-last.py
- Stage: 104
- Summary: Splits trailing segments / end-of-clip operations.
- Run: `python 104_split-last.py`

## 105_angle_cuts.py
- Stage: 105
- Summary: Applies angle cut lists to multicam segments.
- Run: `python 105_angle_cuts.py`

## 106_lock_audio_angle.py
- Stage: 106
- Summary: Locks audio angle choices across multicam.
- Run: `python 106_lock_audio_angle.py`

## 107_layer_audio.py
- Stage: 107
- Summary: Layers additional audio elements (e.g., music, SFX) onto timeline.
- Run: `python 107_layer_audio.py`

## 108_layer_multicam_audio.py
- Stage: 108
- Summary: Layers multicam-specific audio tracks.
- Run: `python 108_layer_multicam_audio.py`

## 109_sub_clip_creator.py
- Stage: 109
- Summary: Derives subclips based on markup/transcripts.
- Run: `python 109_sub_clip_creator.py`

## 111_broll_insert.py
- Stage: 111
- Summary: Inserts bâ€‘roll items based on suggestions/markup.
- Run: `python 111_broll_insert.py`

## 112_timeline_assembler.py
- Stage: 112
- Summary: Assembles timeline from component clips and rules.
- Run: `python 112_timeline_assembler.py`

## 115_timeline_builder.py
- Stage: 115
- Summary: High-level timeline build orchestration.
- Run: `python 115_timeline_builder.py`

## 905_dehydrate_human_edit.py
- Stage: 905
- Summary: Dehydrates timelines generated by a human editor for comparative analysis.
- Inputs: Human-edited FCPXML.
- Outputs: Dehydrated template/manifests for comparison.
- Run: `python 905_dehydrate_human_edit.py`

## apply_tail_trims.py
- Stage: Utility
- Summary: Applies tail trims (e.g., using tail chatter detection results) to fragments.
- Inputs: Trim recommendations; manifests.
- Outputs: Updated fragments or template XML.
- Run: `python apply_tail_trims.py`

## archive_50_final_merge.py
- Stage: Archive
- Summary: Archived version of the final merge step. Kept for reference.

## batch_pipeline.py
- Stage: Orchestration
- Summary: Batch runner that sequences multiple steps end-to-end with sensible defaults.
- Run: `python batch_pipeline.py`
- Notes: Roadmap aims toward a single orchestrator; this may be an interim version.

## cam_transcript_extractor.py
- Stage: Utility
- Summary: Extracts camera- or angle-specific transcript slices.
- Inputs: Transcribed manifests.
- Outputs: Per-angle transcript segments.
- Run: `python cam_transcript_extractor.py`

## project_config.py
- Stage: Config
- Summary: Project-level configuration (paths, constants, feature toggles).
- Run: Imported by other scripts; avoid side effects on import.

## project_paths.py
- Stage: Config
- Summary: Centralizes repo-relative path helpers using `pathlib`.
- Run: Imported by other scripts.

## tools/script_index_scan.py
- Stage: Tooling
- Summary: Scans repository to index scripts and generate inventories.
- Run: `python tools/script_index_scan.py`

## triplet_analyzer.py
- Stage: Utility
- Summary: Analyzes transcript triplets/segments for pacing or semantic grouping.
- Inputs: Transcripts; manifests.
- Outputs: Analysis report/JSON.
- Run: `python triplet_analyzer.py`

---

## Guidance for All Scripts
- Activate venv before running.
- Prefer `pathlib` and relative paths.
- Keep imports side-effect free; wrap execution in `if __name__ == "__main__":`.
- Validate on a small sample first; review artifacts (`*_manifest*.json`, `ai_final_cut.fcpxml`).
- For step 47 trimming: keep first fragment `offset` fixed; adjust `duration` only, and skip initial fragment if trim exceeds it.


