# AI Editing Assistant â€” Documentation

This folder documents the Python and PowerShell scripts in this directory, their roles in the pipeline, and how to run them. It complements in-repo guidelines and provides practical, per-script reference.
\n## Contents\n\n- `cli_usage.md`: Command-line flags and usage for each script.\n
- `scripts_reference.md`: Comprehensive reference for all Python scripts here.
- `powershell_reference.md`: Reference for PowerShell scripts here.

## Conventions and Terminology

- "Take" explicitly refers to a Scene (preferred: Take = Scene).
- Multicam: a single timeline source with multiple angles for one Take.
- Placeholder mc-clip: dehydrated XML representing a single multicam slot in the spine; its numeric filename prefix (e.g., `05_`, `06_`) preserves sequence.

## Project Structure

- Stages are numbered Python scripts at the repo root (e.g., `01_dehydrate.py`, `03_transcription.py`, `41_production_order.py`, `50_final_merge.py`). The number reflects pipeline order.
- Templates/assets: `15_dehydrated_template/`, `15_rehydrated_template/`, `proxies/`, `temp_audio/`.
- Data/outputs: manifests and logs at root (e.g., `master_manifest.json`, `scene_manifest_transcribed.json`, `45_pacing_debug.log`).
- Media: large `.braw`, `.mov`, `.mp4`, `.otio`, `.fcpxml`, `.edl` at root; do not move without updating scripts.

## Environment and Running

Activate your virtual environment first.

- macOS/Linux: `source myenv/bin/activate`
- Windows (PowerShell): `myenv\Scripts\Activate.ps1`

Run a stage (examples):

- `python 01_dehydrate.py`
- `python 03_transcription.py`
- `python 41_production_order.py`
- `python 50_final_merge.py`

Formatting and linting (if configured):

- `black .`
- `ruff check .` or `flake8`

## Chatter Detection & Countdown Offsets

- Step `20_detect_chatter.py` produces `20_countdown_offsets.json` to identify non-content chatter near the head.
- Integration points:
  - Step 25 (rebaseline): Apply head trims per Take and ripple offsets; write `26_adjusted.fcpxml`.
  - Step 21 (rehydrate adjusted): Re-run dehydration using `26_adjusted.fcpxml` as input (or `01_dehydrate.py --fcpxml 26_adjusted.fcpxml`).
  - Step 47 (rehydration): With an already-trimmed baseline, build angle-change fragments; countdown flags not needed.

Step 47 flags (as implemented in this repo family):

- `--enriched`: path to `15_enriched_manifest.json` for robust angle ID resolution.
- `--fill-tail`: preserve original placeholder duration by adding a tail segment.
- `--apply-countdown`: trim initial chatter using `42_countdown_offsets.json` without shifting offsets.
- `--countdown`: custom path to countdown file (default `./42_countdown_offsets.json`).

Guidance for trimming (Step 47):

- Keep the first fragmentâ€™s timeline `offset` unchanged; reduce only its `duration`.
- If trim exceeds the first cut, skip that initial fragment and start at the first valid angle-change fragment.
- Use `--fill-tail` only if total occupancy must match the original placeholderâ€™s duration; otherwise drop chatter time from the timeline.
- Resolve angle IDs from `15_enriched_manifest.json` or `resources`. Avoid brittle angle-name heuristics.

## Typical Flow

```
python 01_dehydrate.py
python 03_transcription.py
python 41_production_order.py
python 50_final_merge.py
```

Chatter-aware flow (Windows examples):

```
python 20_detect_chatter.py
python 25_rebaseline_offsets.py --fcpxml .\Info.fcpxml --countdown .\20_countdown_offsets.json --outfcpxml .\26_adjusted.fcpxml
python 01_dehydrate.py --fcpxml .\26_adjusted.fcpxml
python 47_build_angle_changes.py --resources .\15_dehydrated_template\01_resources.xml --process-clips .\15_dehydrated_template\process_clips --plans .\45_ai_pacing_plans\*_changes.json --outdir .\15_rehydrated_template\mc_clips --enriched .\15_enriched_manifest.json
python 50_final_merge.py
```

## Validation

- Validate on a small sample; inspect generated artifacts (`*_manifest*.json`, `ai_final_cut.fcpxml`).
- For pacing/detection changes, check or extend logging (e.g., `45_pacing_debug.log`).
- Add lightweight assertions/guardrails around I/O; consider a dry-run flag when practical.

## Next Steps (Roadmap)

- Step 48 (trim/smooth): detect and clamp small gaps/overlaps; recompute contiguous `offset`s from placeholder start.
- Editor-first pass: run steps 47â†’50, collect feedback, export new FCPXML.
- Banners/Bâ€‘roll proposals: new steps after editor pass; keep placeholders lightweight.
- Asset generation (later): optional image/video generation for bâ€‘roll slots.
- Validation: quick validator for monotonic start times, butt-join continuity, overrun clamps, and angle-ID resolution health before final merge.


