# Repository Guidelines

## Project Structure & Module Organization
GhostEdit''s production pipeline lives in `fcpxml/pipeline`, containing the numbered stage scripts (01,03,...50) plus the batch orchestrator `batch_pipeline.py`. Project routing data (including `active_project.txt` and `*.json` configs) resides under `fcpxml/pipeline/projects`; keep those paths accurate to resolve working directories like `<project>/ai_working_files`. Supporting utilities sit in `fcpxml/tools`, while reference docs live in `fcpxml/docs`. Use `drafts/` only for experiments that are not invoked by the batch pipeline, and leave `myenv/` for the local virtual environment that should stay untracked.

## Build, Test, and Development Commands
- `myenv\Scripts\Activate.ps1` (PowerShell) - activate the provided virtual environment.
- `python fcpxml/pipeline/batch_pipeline.py --print-paths` - confirm the orchestrator sees the correct project and working folders.
- `python fcpxml/pipeline/batch_pipeline.py` - run the default Phase A->Phase B flow; add `--include-47` when angle-change output is required.
- `python fcpxml/pipeline/40_classify_takes.py --base <working_dir>` - execute a single stage while iterating; replace script name as needed.
- `black fcpxml` and `ruff check fcpxml` - format and lint before opening a review.

## Coding Style & Naming Conventions
Use Python 3 with 4-space indentation, `snake_case` identifiers, and `NN_component.py`/`NNN_component.py` filenames that preserve pipeline order. Prefer `pathlib.Path` over raw strings, isolate script entrypoints under `if __name__ == "__main__":`, and keep logging informative yet concise (`print` statements already surface in batch logs). PowerShell helpers should mirror their Python counterpart names (e.g., `116_build_timeline_from_takes.ps1`).

## Testing Guidelines
There is no automated test suite yet. After code changes, run the relevant stage (or the batch wrapper) against a small project listed in `fcpxml/pipeline/projects`, then inspect generated artifacts such as `ai_working_files/master_manifest.json`, `20_countdown_offsets.json`, or `ai_final_cut.fcpxml`. Review the log timestamps in `fcpxml/*.log` to confirm expected steps executed and add assertions or dry-run flags when touching new I/O.

## Commit & Pull Request Guidelines
Follow the existing Conventional Commit style (`type: summary`), e.g., `feat: tighten chatter window` or `fix: guard empty manifests`. Limit each commit to a logical slice, reference the stages touched in the body, and note any required downstream reruns. Pull requests should include a brief problem statement, the commands executed, before/after artifacts or metrics, and links to related issues or media previews when applicable. Document config or schema changes in the description so other agents update their local `projects/*.json`.

## Watcher Orchestration
- `python fcpxml/run_watchers.py --ffmpeg-dir <path> --workspace-dir <local_assembly_line> --shared-root <client_share>` launches intake -> A-Roll -> B-Roll watchers and writes the aggregate log to `fcpxml/run_watchers.log` (override with `--log-dir`).
- Launch DaVinci Resolve before starting; the runner preflights for `Resolve.exe`/`Resolve` and exits early with guidance if it is not found.
- `004_aroll_doer.py` now handles the hand-off into `REVIEW_AROLL`, so stages `005_aroll_compile_watcher.py`/`006_compile_and_drop_aroll.py` are archived under `fcpxml/pipeline/archive/`. The watcher chain continues with `007_broll_watcher.py` + `008_broll_doer.py`, then `009`/`010` for final review as needed.
- B-Roll runs drop `.broll_processing.lock` in the shared queue while success/failure flags live in `workspace/IN_PROGRESS/<project>`; clear the workspace failure flag to retry.
- Keep `IN_PROGRESS`, `projects`, and `completed` inside the private workspace while exposing only `TODO`, `REVIEW_AROLL`, `AROLL_APPROVED`, and `BROLL_REVIEW` under the shared client root. The launcher auto-creates missing folders and logs the resolved paths.
- Use the explicit overrides (`--todo-dir`, `--review-aroll-dir`, `--aroll-approved-dir`, `--broll-review-dir`, `--in-progress-dir`, `--projects-dir`, `--completed-dir`, and `--resolve-process`) when a client requires non-standard naming or process detection.
- `003_aroll_watcher.py` and `007_broll_watcher.py` accept CLI overrides so the launcher can inject the shared/workspace paths without edits.
- Next: rerun an end-to-end watcher test with the shared root separation; WhisperX (step 03c) remains green after `pip install typing_extensions`.


