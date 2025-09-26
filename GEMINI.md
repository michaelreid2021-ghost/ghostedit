# GEMINI Project Analysis: GhostEdit Pipeline

This document provides a comprehensive overview of the GhostEdit project, an AI-assisted video editing pipeline. It is intended as a guide for future interactions and development.

## 1. Project Overview

**Purpose:** GhostEdit is an automated pipeline designed to process raw video footage and an accompanying FCPXML file into a near-final edited video. It leverages AI for creative and tedious tasks, aiming to produce a high-quality A-Roll (dialogue cut) and a planned B-Roll sequence.

**Architecture:** The project is architected as a resilient, station-based "Assembly Line" (`The Assembly Line.md`). Each station is a logical step in the editing process (e.g., Intake, A-Roll Assembly, B-Roll Automation) and is managed by watcher and doer scripts. This modular design allows for scalability and easier debugging.

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

## 2. Building and Running

### Environment Setup

1.  **Activate Virtual Environment:** The project uses a Python virtual environment located in the `myenv/` directory.
    *   **Windows:** `myenv\Scripts\activate`
    *   **Linux/macOS:** `source myenv/bin/activate`

2.  **Dependencies:** Ensure all necessary Python packages are installed (e.g., `google-generativeai`, `openai-whisper`, `torch`). External tools like `ffmpeg` and `ffprobe` must be in the system's PATH.

### Running the Pipeline

The pipeline is designed to be run via the "Assembly Line" model, but individual scripts can also be executed manually for development and debugging.

**A. The Assembly Line Model (Automated)**

This is the intended production workflow:

1.  **Start the Watchers:** In separate terminals, start the watcher script for each station (e.g., `python fcpxml/pipeline/001_intake_watcher.py --watch-dir /path/to/TODO ...`).
2.  **Submit a Job:** Drop a new folder containing raw media files into the `TODO` directory watched by Station 1.
3.  **Monitor Progress:** The project folder will automatically move through the station directories (`IN_PROGRESS`, `REVIEW_AROLL`, `DONE`) as the pipeline executes.

**B. Manual Project Setup & Execution**

1.  **Initialize a Project:**
    ```bash
    # Sets up the project config in fcpxml/projects/
    python fcpxml/pipeline/project_config.py init --name "MyNewProject" --base "/path/to/project/media" --set-active
    ```

2.  **Run a Single Pipeline Stage:**
    ```bash
    # Example: Run the dehydration script for the active project
    # The script will automatically find the correct paths.
    python fcpxml/pipeline/01_dehydrate.py
    ```

3.  **Run the Batch Orchestrator:**
    The `batch_pipeline.py` script can run the main A-Roll assembly sequence.
    ```bash
    python fcpxml/pipeline/batch_pipeline.py
    ```

## 3. Development Conventions

*   **Modular Scripts:** The pipeline is broken down into dozens of single-responsibility scripts. Script names are prefixed with numbers (e.g., `01_`, `03c_`, `40_`) to indicate their logical order in the workflow.
*   **Path Management:** **All scripts MUST use the `project_paths.py` module** to interact with the file system. This ensures that all file operations are relative to the active project's base and workspace directories. Do not use hardcoded paths.
*   **Configuration:** Project-specific details are managed via JSON files in the `fcpxml/projects/` directory, controlled by `project_config.py`.
*   **Idempotency:** Where possible, scripts should be idempotent. For example, the re-baseline script calculates changes based on the *current* state of the timeline, not its original state, making it safer to re-run.
*   **Logging:** Scripts should use Python's `logging` module to provide clear, consistent output about their progress and any errors.
