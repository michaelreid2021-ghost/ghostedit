# CLI Usage Reference

Accurate, script-by-script command-line flags as implemented in this repository. Most pipeline stages accept `--base` and `--workspace-subdir` via `project_paths.add_base_arg(...)` to resolve paths automatically from an active project.

Note: Use `python <script>.py --help` for runtime help text.

---

## 01_dehydrate.py
- Flags: `--base`, `--workspace-subdir`, `--fcpxml`, `--out`
- Example: `python 01_dehydrate.py --fcpxml .\Info.fcpxml --out .\master_manifest.json`

## 03_transcription.py
- Flags: `--base`, `--workspace-subdir`, `--proxies`, `--ffmpeg`, `--whisper_model`, `--manifest`, `--out`
- Example: `python 03_transcription.py --proxies .\proxies --manifest .\master_manifest.json --out .\scene_manifest_transcribed.json`

## 03_reworked_transcription.py
- Flags: `--base`, `--workspace-subdir`, `--proxies`, `--manifest`, `--out`, `--ffmpeg`, `--whisper_model`, `--device [auto|cuda|cpu]`, `--language`, `--temperature`, `--beam_size`, `--best_of`, `--condition_on_previous_text`, `--fp16`/`--no-fp16`, `--initial_prompt`, `--lexicon`, `--no-word-timestamps`, `--keep-audio-cache`
- Example: `python 03_reworked_transcription.py --proxies .\proxies --manifest .\master_manifest.json --out .\scene_manifest_transcribed.json --whisper_model large`

## 04a_waveform_annotator.py
- Flags: `--base`, `--workspace-subdir`, `--proxies`, `--manifest_in`, `--out`, `--ffmpeg`, `--stereo`, `--vad [webrtc|rms]`, `--vad_aggr 0..3`, `--lexicon`, `--keep_audio`, `--sr`
- Example: `python 04a_waveform_annotator.py --manifest_in .\scene_manifest_transcribed.json --out .\scene_manifest_enriched.json --vad webrtc`

## 10_fcpxml_ripper.py
- Flags: `--base`, `--workspace-subdir`, `--fcpxml`
- Example: `python 10_fcpxml_ripper.py --fcpxml .\Info.fcpxml`

## 15_enriched_manifest.py
- Flags: `--base`, `--workspace-subdir`, `--fcpxml`, `--out`
- Example: `python 15_enriched_manifest.py --fcpxml .\Info.fcpxml --out .\15_enriched_manifest.json`

## 20_detect_chatter.py
- Flags: `--base`, `--workspace-subdir`, `--transcripts`, `--out`, `--nudge`
- Example: `python 20_detect_chatter.py --transcripts .\scene_manifest_transcribed.json --out .\20_countdown_offsets.json --nudge -0.01`

## 21_detect_tail_chatter.py
- Flags: `--base`, `--workspace-subdir`, `--transcripts`, `--out`, `--min-silence`, `--nudge`, `--lexicon`
- Example: `python 21_detect_tail_chatter.py --transcripts .\scene_manifest_transcribed.json --out .\21_tailcut_offsets.json --min-silence 1.2 --nudge -0.02`

## 25_rebaseline_offsets.py
- Flags: `--fcpxml`, `--countdown`, `--outfcpxml`, `--adjustmap`, `--mc-registry`, `--nudge`, `--source [proposed|first|two|midpoint]`, `--contiguity`, `--print-paths`, `--baseline [auto|current|tcStart|override]`, `--override-seconds <float>`, `--audit-baselines`, `--threshold-seconds <float>`
- Example: `python 25_rebaseline_offsets.py --fcpxml .\Info.fcpxml --countdown .\20_countdown_offsets.json --outfcpxml .\26_adjusted.fcpxml --source proposed --contiguity`

## 3c_whisperx_clean_inout.py
- Flags: `--proxies`, `--manifest`, `--out`, `--ffmpeg`, `--whisper_model`, `--device [auto|cuda|cpu]`, `--batch_size`, `--compute_type`, `--align_off`, `--diarize`, `--min_speakers`, `--hf_token_env`, `--lexicon`, `--min_content_sec`, `--min_seg_sec`, `--head_guard`, `--tail_guard`, `--min_word_conf`, `--max_countdown_sec`, `--pre_anchor_window`, `--keep_audio_cache`, `--emit_words`

## 39_chomp_me.py
- Flags: `--base`, `--workspace-subdir`, `--project_id`, `--location`, `--model`, `--input_file`, `--out_file`

## 40_classify_takes.py
- Flags: `--base`, `--workspace-subdir`, `--project_id`, `--location`, `--model`, `--input_file`, `--out_file`

## 41_production_order.py
- Flags: `--base`, `--workspace-subdir`, `--project_id`, `--location`, `--model`, `--input_file`, `--plan40`, `--clip_library`, `--ignore_file`, `--out_file`

## 42_chatter_detector.py (legacy)
- Similar to Step 20; legacy interface.

## 45_pacing_analyzer.py
- Flags: `--base`, `--workspace-subdir`, `--project_id`, `--location`, `--model`, `--plan_file`, `--transcripts_file`, `--offsets_file`, `--output_dir`, `--log_file`, `--ignore_file`

## 45a_pacing_analyzer.py / 45b_pacing_test.py / 45c_pacing_test.py
- Flags: include `--base`, `--workspace-subdir` in analyzers/tests; see `--help`.

## 46_b_roll_suggester.py
- Flags: `--base`, `--workspace-subdir`, `--project_id`, `--location`, `--model`, `--seed`, `--output_dir`, `--ignore_file`, `--debug_log_dir`

## 47_build_angle_changes.py
- Flags: `--base`, `--workspace-subdir`, `--src-enable`, `--fill-tail`, `--apply-countdown`
- Notes: Paths for resources, manifests, and plans resolve from the active project. Use `project_config.py`/`project_paths.py` to set the active project.

## 47_fetch_submitted_veo_ops.py
- No explicit CLI flags detected.

## 47_generate_b_roll.py
- Flags: `--base`, `--workspace-subdir`

## 47_make_broll_placeholders.py
- Flags: `--base`, `--workspace-subdir`

## 50_final_merge.py
- Flags: `--base`, `--workspace-subdir`, `--header`, `--resources`, `--library_open`, `--footer`, `--ignored_clips`, `--mc_clips`, `--out`
- Example: `python 50_final_merge.py --out .\ai_final_cut.fcpxml`

## 103-reflow-and-more.py
- Base flags: `--fcpxml <in>`, `--out <out>`; then one subcommand:
  - `adjust-clip-start --ref <id> --seconds <float>`
  - `adjust-clip-end --ref <id> --seconds <float>`
  - `delete-clip --offset <tc>`
  - `stretch-clip --offset <tc> --delta <float>`
  - `merge-clips --offsets <tc1,tc2> [--angle keep_first|keep_second]`
  - `reflow --anchor [tcstart|first]`

## 104_split-last.py
- Flags: `--fcpxml`, `--out`; Subcommand: `split-last --ref <id> --seconds <float>`

## 105_angle_cuts.py
- Flags: `--fcpxml`, `--out`, `--format [json|txt]`, plus script-specific filters.

## 106_lock_audio_angle.py
- Flags: `--base`, `--workspace-subdir`, `--angle-name`

## 107_layer_audio.py
- Flags: includes `--base`, `--workspace-subdir`.

## 108_layer_multicam_audio.py
- Flags: `--base`, `--fcpxml`, `--out`

## 109_sub_clip_creator.py
- Flags: `--fcpxml`, `--out`; Subcommand `sub-clip --source-ref <id> --start-seconds <float> --duration-seconds <float> --name <str>`

## 111_broll_insert.py
- Flags: `--fcpxml`, `--out`; Subcommand `insert --target-offset <float> --broll-path <Path> --insert-time <float> --duration <float> [--format-id rX] [--lane N]`

## apply_tail_trims.py
- Flags: `--base <projects_root>`, `--projects <assistant_projects_dir>`, `--active-file <path>`, `--manifest <path>`, `--tail-offsets <path>`, `--python-exe <exe>`, `--tool <path>`

---

## Orchestrators & Utilities
- `batch_pipeline.py`: Batch runner with phases (`--skip-03`, `--include-47`, `--dry-run`, `--nudge20`, `--source25`, `--nudge25`, `--baseline25`, `--contiguity`/`--no-contiguity`, `--audit-baselines`, `--threshold-seconds`, `--print-paths`). See `--help`.
- `cam_transcript_extractor.py`: `--transcripts_file`, `--output_dir`, `--ignore_file`.
- `project_config.py`: Subcommands for project configs (`init`, `show`, `active`, `update` etc.). See `--help`.
- `project_paths.py`: Provides `--base`, `--workspace-subdir` integration helpers.
- `triplet_analyzer.py`: `--manifest`, triplet detection tuning flags.

