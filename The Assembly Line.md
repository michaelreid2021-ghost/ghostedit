***
### The "Assembly Line" Model
The core idea is a chain of command:
`[Watched Folder] -> Watcher detects trigger -> Watcher calls Doer -> Doer performs task -> Doer creates trigger in next Watched Folder`

---
### Station 1: Project Intake
This station's job is to accept new work and prepare it for the main pipeline.

* **Watcher**: `001_intake_watcher.py`
* **Watched Location**: A shared `.../TODO/` directory.
* **Trigger**: A new subdirectory (e.g., `client_project_name`) is dropped in. The watcher waits for file activity to cease before acting.
* **Doer**: `002_setup_project.py`
* **Key Actions**:
    1.  Creates the full, standardized project directory structure in an `.../IN_PROGRESS/` location.
    2.  Parses the source video filenames to create the initial `asset_manifest.json` (using take numbers for sequence order, as planned).
    3.  Copies a static `template.fcpxml` into the new project root to serve as a bootstrap timeline.
* **Output**: Creates a trigger file like `.project_ready_for_assembly` inside the new project directory.

---
### Station 2: A-Roll Assembly & Handoff
This station runs the core AI pipeline to create the clean A-Roll and prepares it for human review.

* **Watcher**: `003_aroll_watcher.py`
* **Watched Location**: The `.../IN_PROGRESS/` directory.
* **Trigger**: The `.project_ready_for_assembly` file.
* **Doer**: `004_aroll_doer.py` (This script orchestrates the sequence you've already perfected).
* **Key Actions**:
    1.  Procedurally builds the initial multicam timeline based on the `asset_manifest.json`.
    2.  Calls the scripts for transcription (`3c`), annotation (`04a`), high-level planning (`40`), and producer's markup (`44`).
    3.  Calls the scripts (`116`/`115`) to build the final, clean-take A-roll `Info.fcpxml`.
* **Output**: Moves the entire project folder to a `.../REVIEW_AROLL/` directory and potentially sends a notification.

---
### Station 3: B-Roll Automation (Post-Approval)
This station waits for a human to approve the A-Roll and then automatically adds all the B-Roll.

* **Watcher**: `005_broll_watcher.py`
* **Watched Location**: An `.../APPROVED_AROLL/` directory where the human editor places the tweaked project.
* **Trigger**: A new project folder appears.
* **Doer**: `006_broll_doer.py`
* **Key Actions**:
    1.  Generates the clean `.srt` file from the approved `Info.fcpxml`.
    2.  Runs the B-roll suggestion script (`46`) to create the AI plan.
    3.  Runs the script to generate the physical placeholder video files.
    4.  Runs the scripts (`118`/`117`) to place the B-roll placeholders onto the timeline.
* **Output**: Creates a trigger file like `.broll_complete_ready_for_qa`.

---
### Station 4: Automated QA & Final Delivery
The final automated check before the project is considered complete.

* **Watcher**: `007_qa_watcher.py`
* **Watched Location**: The `.../APPROVED_AROLL/` directory.
* **Trigger**: The `.broll_complete_ready_for_qa` file.
* **Doer**: `008_qa_doer.py`
* **Key Actions**:
    1.  Performs the DaVinci Resolve import/export round-trip to normalize the FCPXML.
    2.  Programmatically scans the final FCPXML for anomalies (extra lanes, bad timecodes).
    3.  Generates the final "AI Editor Notes" summary.
* **Output**: Moves the completed project folder to a final `.../DONE/` directory.

### Benefits of this Approach
* **Resilience**: If the B-roll doer fails, the project simply stays in the "approved A-roll" state. The pipeline doesn't crash; you can just fix the doer and it will pick up where it left off.
* **Scalability**: You can easily see where every project is in the pipeline just by looking at the directory structure. In the future, you could have multiple "doer" machines all pulling from the same queue.
* **Debuggability**: Each doer is a focused tool. If something goes wrong, you know exactly which part of the process failed and have all the input files for that stage ready for inspection.