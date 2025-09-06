# gemini_is_being_a_dork.ps1
# v1.0 (2025-08-28)
# - This script serves as the pipeline-native wrapper for the Python remapping script.
# - It automatically finds the active project root and calls the Python script
#   with the correct arguments, requiring no manual input.

# --- Configuration ---
$basePath = "E:\davinci\projects\"
$projectsFolder = "E:\ai_editing_assistant\fcpxml\projects\"
$activeProjectFile = Join-Path $projectsFolder "active_project.txt"
$pythonScriptPath = ".\116_remap_broll_timestamps.py" # The Python script this wrapper calls

# --- Initialization ---
try {
    $active_project = Get-Content -Path $activeProjectFile
}
catch {
    Write-Error "FATAL: Could not read active project file. Error: $_"
    exit 1
}

$projectRoot = Join-Path $basePath $active_project

# =================================================================================
# SCRIPT EXECUTION STARTS HERE
# =================================================================================
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Remapping B-Roll Timestamps for: $active_project" -ForegroundColor Cyan
Write-Host "==========================================================="

if (-not (Test-Path $projectRoot)) {
    Write-Error "FATAL: Project root directory not found at '$projectRoot'."
    exit 1
}

# --- Build Arguments and Execute Python Script ---
$arguments = @(
    "--project-root", "`"$projectRoot`""
)

Write-Host "Calling Python remapper with project root: $projectRoot"
& python $pythonScriptPath $arguments

if ($LASTEXITCODE -ne 0) {
    Write-Error "FATAL: 116_remap_broll_timestamps.py failed. Halting pipeline."
    exit 1
}

Write-Host "`n===========================================================" -ForegroundColor Cyan
Write-Host "  B-Roll Timestamp Remapping Complete." -ForegroundColor Cyan
Write-Host "==========================================================="
