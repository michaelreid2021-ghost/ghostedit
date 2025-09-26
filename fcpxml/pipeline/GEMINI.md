# GEMINI Project Analysis: GhostEdit Pipeline

This document provides a comprehensive overview of the GhostEdit project, an AI-assisted video editing pipeline. It is intended as a guide for future interactions and development.

## 1. Project Overview

**Purpose:** GhostEdit is an automated pipeline designed to process raw video footage and an accompanying FCPXML file into a near-final edited video. It leverages AI for creative and tedious tasks, aiming to produce a high-quality A-Roll (dialogue cut) and a planned B-Roll sequence.

**Architecture:** The project is architected as a resilient, station-based "Assembly Line". Each station is a logical step in the editing process (e.g., Intake, A-Roll Assembly, B-Roll Automation) and is managed by watcher and doer scripts. This modular design allows for scalability and easier debugging.

**Core Technologies:**
- **Primary Language:** Python
- **Orchestration:** A combination of Python scripts (`001_intake_watcher.py`, `batch_pipeline.py`) and PowerShell.
- **Video/Audio Processing:** `ffmpeg` and `ffprobe` are used for media analysis and placeholder generation.
- **AI & Machine Learning:**
    - **Transcription:** `whisper` and `whisperx` for generating timed transcripts.
    - **Creative AI:** Google's Gemini models (`genai`) are used for high-level planning, including paper edits, pacing analysis, and B-roll suggestions.
- **NLE Integration:** The pipeline is built around the FCPXML format and includes scripts to interact directly with DaVinci Resolve's scripting API.

**Project Configuration:**
A robust configuration system (`project_config.py`, `project_paths.py`) allows the pipeline to manage multiple distinct projects. An active project is set via `projects/active_project.txt`, and all scripts use this context to locate media and save outputs to the correct workspace.

## 2. The Assembly Line Pipeline

The primary mechanism for using the GhostEdit pipeline is the "Assembly Line," a series of automated stations, each with a watcher and a doer script. Here’s how a project moves through the pipeline:

### Station 1: Intake and Project Setup
*   **Watcher:** `001_intake_watcher.py`
    *   **Function:** Monitors a `TODO` directory for new folders with raw video files.
    *   **Trigger:** A new, stable folder of media.
    *   **Action:** Moves the folder to the `IN_PROGRESS` directory and calls the doer.
*   **Doer:** `002_setup_project.py`
    *   **Function:** Initializes the project, analyzes media with `ffprobe`, and creates a multicam FCPXML file.
    *   **Output:** Creates `Info.fcpxml`, `asset_manifest.json`, and a `.project_ready_for_assembly` trigger file.

### Station 2: A-Roll (Dialogue) Assembly
*   **Watcher:** `003_aroll_watcher.py`
    *   **Function:** Waits for the project to be ready for A-roll assembly.
    *   **Trigger:** The `.project_ready_for_assembly` file.
    *   **Action:** Calls the doer.
*   **Doer:** `004_aroll_doer.py`
    *   **Function:** Orchestrates the A-roll generation by running a series of scripts to create a "producer's markup" and then builds the timeline using `115_timeline_builder.py`.
    *   **Output:** An updated `Info.fcpxml` with the A-roll edit and a `.aroll_processing_complete` trigger file.

### Station 3: A-Roll Compilation and Review
*   **Watcher:** `005_aroll_compile_watcher.py`
    *   **Function:** Waits for the A-roll assembly to be complete.
    *   **Trigger:** The `.aroll_processing_complete` file.
    *   **Action:** Calls the doer.
*   **Doer:** `006_compile_and_drop_aroll.py`
    *   **Function:** This script is currently a redundant step that mirrors `115_timeline_builder.py`. It places the final A-Roll FCPXML into a `review_aroll` directory for human approval.

### Station 4: B-Roll (Supplemental Footage) Generation
*   **Watcher:** `007_broll_watcher.py`
    *   **Function:** Waits for a human to approve the A-roll.
    *   **Trigger:** A project folder appearing in the `AROLL_APPROVED` directory.
    *   **Action:** Calls the doer.
*   **Doer:** `008_broll_doer.py`
    *   **Function:** Orchestrates the B-roll generation. It uses DaVinci Resolve to export an SRT, generates a B-roll plan with the Gemini API, creates placeholder clips, and applies them to the timeline.
    *   **Output:** An updated `Info.fcpxml` with B-roll placeholders. The project is then moved to the `completed` directory.

## 3. Running the Pipeline

### Environment Setup

1.  **Activate Virtual Environment:** The project uses a Python virtual environment located in the `myenv/` directory.
    *   **Windows:** `myenv\Scripts\activate`
    *   **Linux/macOS:** `source myenv/bin/activate`

2.  **Dependencies:** Ensure all necessary Python packages are installed (e.g., `google-generativeai`, `openai-whisper`, `torch`). External tools like `ffmpeg` and `ffprobe` must be in the system's PATH.

### Running the Automated Pipeline

1.  **Start the Watchers:** In separate terminals, start the watcher script for each station (e.g., `python fcpxml/pipeline/001_intake_watcher.py --watch-dir /path/to/TODO ...`).
2.  **Submit a Job:** Drop a new folder containing raw media files into the `TODO` directory watched by Station 1.
3.  **Monitor Progress:** The project folder will automatically move through the station directories (`IN_PROGRESS`, `REVIEW_AROLL`, `DONE`) as the pipeline executes.

### Manual Execution

While the automated pipeline is the intended workflow, individual scripts can be run manually for development and debugging.

1.  **Initialize a Project:**
    ```bash
    # Sets up the project config in fcpxml/projects/
    python fcpxml/pipeline/project_config.py init --name "MyNewProject" --base "/path/to/project/media" --set-active
    ```

2.  **Run a Single Pipeline Stage:**
    ```bash
    # Example: Run the dehydration script for the active project
    python fcpxml/pipeline/01_dehydrate.py
    ```

## 4. Development Conventions

*   **Modular Scripts:** The pipeline is broken down into dozens of single-responsibility scripts. Script names are prefixed with numbers (e.g., `01_`, `03c_`, `40_`) to indicate their logical order in the workflow.
*   **Path Management:** **All scripts MUST use the `project_paths.py` module** to interact with the file system. This ensures that all file operations are relative to the active project's base and workspace directories. Do not use hardcoded paths.
*   **Configuration:** Project-specific details are managed via JSON files in the `fcpxml/projects/` directory, controlled by `project_config.py`.
*   **Idempotency:** Where possible, scripts should be idempotent. For example, the re-baseline script calculates changes based on the *current* state of the timeline, not its original state, making it safer to re-run.
*   **Logging:** Scripts should use Python's `logging` module to provide clear, consistent output about their progress and any errors.