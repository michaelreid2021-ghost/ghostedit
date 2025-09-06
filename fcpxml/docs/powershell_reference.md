# PowerShell Scripts Reference

This document catalogs all PowerShell scripts in this directory with their purpose and usage. Run these from a PowerShell prompt with appropriate execution policy.

> Tip: If execution policy blocks scripts, you can use: `pwsh -NoProfile -ExecutionPolicy Bypass -File .\\script.ps1`.

---

## 110_markup_to_subclips.ps1
- Purpose: Converts editorial markup (producer notes/regions) into subclips in the timeline.
- Inputs: Markup/notes files; project/timeline identifiers.
- Outputs: Subclip items in the timeline or an updated FCPXML.
- Run: `pwsh -File .\\110_markup_to_subclips.ps1 [args]`

## 113_setup_timeline.ps1
- Purpose: Prepares a working timeline with required tracks, roles, and structure.
- Inputs: Project config; initial FCPXML or timeline handle.
- Outputs: Initialized timeline ready for assembly.
- Run: `pwsh -File .\\113_setup_timeline.ps1 [args]`

## 114_assemble_clean_takes.ps1
- Purpose: Assembles clean takes/scenes into a rough cut timeline (preâ€‘effects).
- Inputs: Dehydrated templates; manifests.
- Outputs: Intermediate assembled timeline.
- Run: `pwsh -File .\\114_assemble_clean_takes.ps1 [args]`

## 116_build_timeline_from_takes.ps1
- Purpose: Builds the final or near-final timeline by stitching takes and applying ordering rules.
- Inputs: Ordered takes manifest; rehydrated mcâ€‘clips.
- Outputs: Finalized timeline or FCPXML.
- Run: `pwsh -File .\\116_build_timeline_from_takes.ps1 [args]`

## edit_starts.ps1
- Purpose: Adjusts start offsets for selected clips/spans in a timeline (batch head trims).
- Inputs: Selection criteria or time ranges.
- Outputs: Updated timeline offsets.
- Run: `pwsh -File .\\edit_starts.ps1 [args]`

## update_angles.ps1
- Purpose: Batch-updates angle assignments for multicam clips.
- Inputs: Angle mapping/plan; target clips.
- Outputs: Updated multicam angle attributes.
- Run: `pwsh -File .\\update_angles.ps1 [args]`

---

## Notes
- Use Windows PowerShell (`powershell.exe`) or PowerShell 7+ (`pwsh`); prefer `pwsh` for cross-platform compatibility.
- Scripts may expect the working directory to be the repo root; use `Set-Location` accordingly.
- For reproducibility, avoid absolute drive letters; use paths relative to the repo.


## edit_starts.ps1
- Params: none declared. Uses config variables at top of file.
- Behavior: Reads projects/active_project.txt, loads i_working_files/master_manifest.json and 20_countdown_offsets.json, and calls 103-reflow-and-more.py to trim the head of the first multicam instance per take.
- Run: pwsh -File .\edit_starts.ps1
- Notes: Update $basePath and $projectsFolder at the top if your environment differs.

## update_angles.ps1
- Params: none declared. Uses config variables at top of file.
- Behavior: Iterates 45_ai_pacing_plans/*_changes.json and uses 104_split-last.py to split and apply angle changes for the associated multicam ref.
- Run: pwsh -File .\update_angles.ps1
- Notes: Expects projects/active_project.txt and standard i_working_files layout.

## 110_markup_to_subclips.ps1 / 113_setup_timeline.ps1 / 114_assemble_clean_takes.ps1 / 116_build_timeline_from_takes.ps1
- Params: none declared in repo (no Param() blocks found).
- Behavior: Project-specific orchestration helpers that assume the standard project layout (projects/active_project.txt → base → i_working_files).
- Run: pwsh -File .\<script>.ps1
- Notes: If needed, adjust paths inside the scripts to your environment.
