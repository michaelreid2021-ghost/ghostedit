# 120_remap_angle_changes.ps1
# v1.0 (2025-08-28)
# - Pipeline wrapper for the Python angle change remapping script.
# - Automatically finds the active project and necessary paths, then calls
#   the Python script with the correct arguments.

# --- Configuration ---
$basePath = "E:\davinci\projects\"
$projectsFolder = "E:\ai_editing_assistant\fcpxml\projects\"
$activeProjectFile = Join-Path $projectsFolder "active_project.txt"
$pythonScriptPath = ".\119_remap_angle_changes.py"

# --- Initialization ---
try {
    $active_project = Get-Content -Path $activeProjectFile
}
catch {
    Write-Error "FATAL: Could not read active project file. Error: $_"
    exit 1
}

$projectRoot = Join-Path $basePath $active_project

# --- Automatically determine paths ---
$mapFile = Join-Path $projectRoot "timecode_map.json"
$plansDir = Join-Path $projectRoot "ai_working_files\45_ai_pacing_plans"

# =================================================================================
# SCRIPT EXECUTION STARTS HERE
# =================================================================================
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Remapping Angle Change Timestamps for: $active_project" -ForegroundColor Cyan
Write-Host "==========================================================="

if (-not (Test-Path $mapFile)) { Write-Error "FATAL: Timecode map not found at '$mapFile'."; exit 1 }
if (-not (Test-Path $plansDir)) { Write-Error "FATAL: Pacing plans directory not found at '$plansDir'."; exit 1 }

# --- Build Arguments and Execute Python Script ---
$arguments = @(
    "--map", "`"$mapFile`"",
    "--plans-dir", "`"$plansDir`""
)

Write-Host "Calling Python angle remapper..."
& python $pythonScriptPath $arguments

if ($LASTEXITCODE -ne 0) {
    Write-Error "FATAL: 119_remap_angle_changes.py failed. Halting pipeline."
    exit 1
}

Write-Host "`n===========================================================" -ForegroundColor Cyan
Write-Host "  Angle Change Timestamp Remapping Complete." -ForegroundColor Cyan
Write-Host "==========================================================="