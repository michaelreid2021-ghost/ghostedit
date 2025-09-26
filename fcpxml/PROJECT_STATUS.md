# Project GhostEdit: Pipeline Status

This document tracks the overall goal and progress of the AI-assisted video editing pipeline.

## Overall Goal

The primary objective is to create a fully-automated, end-to-end video editing pipeline. The system is designed to take raw project files (FCPXML, video media) and produce a high-quality, near-final video edit.

This involves several automated stages:
- **Analysis & Pre-processing:** Dehydrating FCPXML files, transcribing audio, and enriching the data with waveform analysis.
- **AI-driven Creative Planning:** Using language models to generate a paper edit, define the narrative structure, create pacing plans for camera cuts, and suggest relevant B-roll.
- **Automated Timeline Assembly:** Reconstructing the FCPXML timeline based on the AI-generated plans, including inserting clean takes, applying angle changes, and adding placeholder B-roll.
- **NLE Integration:** Interacting with DaVinci Resolve to perform final tasks like subtitle generation and final export.

## Current Progress & Recent Accomplishments

The core pipeline structure is in place, with numerous scripts handling distinct stages.

**Recent Integration Task: Subtitle Generation**

The most recent work involved integrating a previously experimental script for subtitle generation into the main pipeline.

- **Accomplishments:**
    1.  **Reviewed Project Architecture:** Confirmed understanding of the `project_config.py` and `project_paths.py` system, which is central to making pipeline stages modular and configurable.
    2.  **Integrated Subtitle Script:** The script `212_generate_and_export_subtitles.py`, which uses the DaVinci Resolve API to auto-caption a timeline, has been fully integrated.
    3.  **Refactored for Pipeline Awareness:** The script was modified to use the standard `project_paths` system. It no longer saves files to a hardcoded home directory; instead, it correctly places the output `.srt` file into the active project's workspace.
    4.  **Re-numbered Pipeline Stages:** To improve logical ordering and create space for future enhancements, the script was renamed to `210_generate_and_export_subtitles.py`. The subsequent export script was also moved from `213` to `220`.

**Next Steps:**
- Continue integrating and hardening other experimental scripts.
- Further develop the AI-driven creative planning stages.
- Build out robust orchestration to run the entire pipeline from start to finish.
