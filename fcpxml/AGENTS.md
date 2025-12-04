# Repository Guidelines







## Project Structure & Module Organization



- Source scripts: numbered Python stages in the repo root (e.g., `01_dehydrate.py`, `03_transcription.py`, `41_production_order.py`, `50_final_merge.py`). The number reflects pipeline order.



- Templates/assets: `15_dehydrated_template/`, `15_rehydrated_template/`, `proxies/`, `temp_audio/`.



- Data/outputs: manifests and logs at root (e.g., `master_manifest.json`, `scene_manifest_transcribed.json`, `45_pacing_debug.log`).



- Media: large `.braw`, `.mov`, `.mp4`, `.otio`, `.fcpxml`, `.edl` files live at root; avoid moving without updating scripts.







## Terminology (Important)



- Take: In this production house, "Take" explicitly refers to a Scene.



- Multicam: A single timeline source containing multiple angles (Angle A/B) for one Take/Scene.



- Placeholder mc-clip: The dehydrated XML representing a single multicam slot in the spine; its filename prefix (e.g., `05_`, `06_`) preserves sequence.







Note: Historical references in code/comments may use "take" and "scene" interchangeably; for all documentation and new changes, prefer "Take = Scene".







## Build, Test, and Development Commands



- Environment: activate the local venv before running scripts - Linux/macOS: `source myenv/bin/activate`; Windows: `myenv\Scripts\activate`.



- Run a stage: `python 40_classify_takes.py` (substitute the script you're working on).



- Typical flow: `python 01_dehydrate.py` -> `python 03_transcription.py` -> `python 41_production_order.py` -> `python 50_final_merge.py` (adjust per task).



- Formatting (if configured): prefer `black .` for formatting and `ruff check .` or `flake8` for linting.







### Step 47 Flags



- `--enriched`: path to `15_enriched_manifest.json` for robust angle ID resolution.



- `--fill-tail`: preserve original placeholder duration by adding a tail segment.



- `--apply-countdown`: trim initial chatter using `42_countdown_offsets.json` without shifting offsets.



- `--countdown`: custom path to countdown file (default: `./42_countdown_offsets.json`).







## Coding Style & Naming Conventions



- Python 3, 4-space indents, `snake_case` for files, functions, and variables.



- New stages: prefix with two digits + concise action, e.g., `47_build_angle_changes.py`.



- Prefer `pathlib` and relative paths; avoid hard-coded absolute drives like `E:\`.



- Keep imports side-effect free; put execution under `if __name__ == "__main__":`.







## Testing Guidelines



- No formal test suite yet. Validate on a small sample first and inspect generated artifacts (`*_manifest*.json`, `ai_final_cut.fcpxml`).



- For pacing and detection changes, check or extend logging (see `45_pacing_debug.log`).



- Add lightweight assertions and guardrails around I/O; consider a dry-run flag when practical.







## Commit & Pull Request Guidelines



- Commits: imperative, scoped messages referencing the stage, e.g., `50_final_merge: align audio handles`, `42_chatter_detector: tune threshold`.



- PRs: include a clear description, affected scripts, linked issue (if any), and before/after evidence (counts, sample paths, or small screenshots of timelines).







## Security & Configuration Tips



- Don't commit large binaries or secrets; honor `.ai_ignore`/`.gitignore`. Keep outputs deterministic (avoid timestamped filenames when possible).



- Document any environment variables or external tool versions touched (e.g., Resolve, FCPXML/OTIO expectations).







## Chatter Detection & Countdown Offsets



- Step `20_detect_chatter.py` (migrated from step 42) produces `20_countdown_offsets.json` to identify non-content chatter (e.g., director chatter) near the head.



- Integration points:



  - Step 25 (rebaseline): Apply head trims per Take and ripple offsets; write `26_adjusted.fcpxml`.



  - Step 21 (rehydrate adjusted): Re-run dehydration using `26_adjusted.fcpxml` as input (or reuse `01_dehydrate.py` with a `--fcpxml` arg).



  - Step 47 (rehydration): With an already-trimmed baseline, build angle-change fragments; countdown flags not needed.



- Guidance:



  - When trimming at step 47, keep the first fragment's timeline `offset` unchanged, reduce only its `duration`. If trimming exceeds the first cut, skip that initial fragment and start at the first angle-change fragment.



  - Use the `--fill-tail` option in step 47 only if you need the total occupancy to match the original placeholder duration; otherwise leave it off to drop chatter time from the timeline.



  - Avoid brittle angle-name heuristics; resolve angle IDs from `15_enriched_manifest.json` or `resources`.







Examples (Windows):



- Detect chatter: `python 20_detect_chatter.py`



- Rebaseline offsets (scaffold): `python 25_rebaseline_offsets.py --fcpxml .\Info.fcpxml --countdown .\20_countdown_offsets.json --outfcpxml .\26_adjusted.fcpxml`



- Dehydrate adjusted: `python 01_dehydrate.py --fcpxml .\26_adjusted.fcpxml` (or a dedicated `21_dehydrate_adjusted.py`)



- Angle changes: `python 47_build_angle_changes.py --resources .\15_dehydrated_template\01_resources.xml --process-clips .\15_dehydrated_template\process_clips --plans .\45_ai_pacing_plans\*_changes.json --outdir .\15_rehydrated_template\mc_clips --enriched .\15_enriched_manifest.json`



- Merge final timeline: `python 50_final_merge.py`







## Automation Roadmap



- Goal: end-to-end agentic run from a watched folder: drop media + `Info.fcpxml`, then "Go".



- Near-term: minimize flags with sensible defaults; auto-discover resources, plans, and manifests.



- Future: a single entry that orchestrates steps 01->50, with pluggable tools (e.g., step 42) gated by config.







## Next Steps (Roadmap)



- Step 48 (trim/smooth): detect small gaps/overlaps between adjacent mc-clip fragments, clamp edge durations, and recompute contiguous `offset`s from the original placeholder start without shifting it. Add thresholds (e.g., `--max-gap-frames`) and warnings when smoothing exceeds a limit.



- Editor-first pass: run steps 47->50, let an editor approve/tweak angle changes in a clean timeline (no banners/b-roll yet), then export a fresh FCPXML.



- Banners/B-roll proposals: after editor pass, generate recommended banner/lower-third placements and b-roll placeholders (new steps) referencing time ranges from the edited FCPXML; keep placeholders lightweight to avoid clutter.



- Asset generation (later): wire optional on-demand image/video generation for b-roll slots, with a review queue and replace-in-place behavior.



- Validation: add a quick validator to check monotonic start times, butt-join continuity, overrun clamps, and angle-ID resolution health before final merge.







## Watcher Orchestration

- Launch watchers from the repo root with `python fcpxml/run_watchers.py --ffmpeg-dir <ffmpeg_bin> --workspace-dir <workspace> --shared-root <client_root> --log-dir <logdir>` to pin both local and shared paths.
- Ensure DaVinci Resolve is open before starting; the launcher exits early with `Please launch DaVinci Resolve before starting Watchers` if the process is not running.
- Intake watches <shared_root>/TODO, moves projects into <workspace>/IN_PROGRESS/<project>, and keeps success/failure flags inside the workspace instead of the shared drive.
- B-Roll watcher drops `.broll_processing.lock` alongside the shared project folder while a job is active; clear the lock manually only if a run aborts mid-stream.
- WhisperX cleanup (step 03c) now requires `typing_extensions`; `pip install typing_extensions` was run on 2025-09-30 so the dependency is satisfied locally.
- 118_apply_broll_placeholders.ps1 now receives the project workspace via PowerShell `-ProjectDir`, so it no longer depends on the old assembly_line paths.
- If a run fails mid-stream, delete `.broll_processing_failed` under `workspace/IN_PROGRESS/<project>` before restarting to allow the watchers to re-trigger.
- 46_srt_b_roll_suggester.py now re-runs if a previous success flag exists but the B-roll plan directory is empty; it exits with status 1 when no suggestions are produced so the watcher holds the project in IN_PROGRESS.
- 118_apply_broll_placeholders.ps1 treats an empty B-roll plan as fatal (exit 1), keeping the run marked as a failure instead of archiving incomplete work.
- run_watchers.py now also launches 009_final_review_watcher.py so `.broll_processing_complete` hand-offs immediately trigger the review package build into <shared_root>/BROLL_REVIEW/<project>.
- 009_final_review_watcher.py and 010_final_review_doer.py accept `--review-dir`/`--projects-root` overrides; use them when pointing at alternate workspaces so `.final_review_complete` lands under <workspace>/completed/<project>.




