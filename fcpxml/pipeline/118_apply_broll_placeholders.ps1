# 118_apply_broll_placeholders.ps1
# v1.3 (2025-08-27)
# - FIX: Do NOT pre-clone Info.fcpxml. Read current Info.fcpxml, write to a UNIQUE temp per item,
#        then backup+promote. Prevents “last insert wins” behavior.
# - Mirrors 116’s structure and logging.

# --- Configuration ---
$basePath = "E:\ghostedit\assembly_line\projects\"
$projectsFolder = "E:\ghostedit\fcpxml\pipeline\projects\"
$activeProjectFile = Join-Path $projectsFolder "active_project.txt"
$pythonScriptPath = ".\117_insert_broll_asset_clip.py"

# --- Initialization ---
try {
    Add-Type -AssemblyName System.Web.Extensions
    $jsonSerializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $jsonSerializer.MaxJsonLength = [int]::MaxValue
    $active_project = Get-Content -Path $activeProjectFile
}
catch {
    Write-Error "FATAL: Could not read active project file or initialize serializer. Error: $_"
    exit 1
}

$projectRoot = Join-Path $basePath $active_project
$fcpxml_main = Join-Path $projectRoot "Info.fcpxml"
$assetsDir   = Join-Path $projectRoot "assets"

if (-not (Test-Path $fcpxml_main)) { Write-Error "FATAL: Missing '$fcpxml_main'."; exit 1 }
if (-not (Test-Path $assetsDir))   { New-Item -ItemType Directory -Path $assetsDir | Out-Null }

# --- Helper Function ---
function Update-TimelineFile {
    param([string]$currentFile, [string]$tempFile)
    if (Test-Path $tempFile) {
        $timestamp = Get-Date -Format "yyyyMMddHHmmss"
        $rand = Get-Random -Minimum 1000 -Maximum 9999
        $backupFile = Join-Path (Split-Path $currentFile) "Info.bak.$($timestamp)_$($rand).fcpxml"
        Move-Item -Path $currentFile -Destination $backupFile -Force
        Move-Item -Path $tempFile   -Destination $currentFile -Force
        Write-Host "SUCCESS: Timeline updated. Backup: $backupFile" -ForegroundColor Green
    } else {
        Write-Error "FAILURE: Python script did not create the output file '$tempFile'. Halting."
        exit 1
    }
}

# =================================================================================
# SCRIPT EXECUTION STARTS HERE
# =================================================================================
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Applying B-Roll Placeholders for: $active_project" -ForegroundColor Cyan
Write-Host "==========================================================="

# --- Load Placeholder Data ---
$brollPlansDir = Join-Path $projectRoot "ai_working_files\46_ai_b_roll_plans"
if (-not (Test-Path $brollPlansDir)) {
    Write-Error "FATAL: B-roll plans directory not found at '$brollPlansDir'."
    exit 1
}

# Dynamically find the latest B-roll plan file
$latestBrollPlan = Get-ChildItem -Path $brollPlansDir -Filter "*_broll_plan.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $latestBrollPlan) {
    Write-Error "FATAL: No '*_broll_plan.json' file found in '$brollPlansDir'."
    exit 1
}

Write-Host "-> Using latest B-roll plan: $($latestBrollPlan.FullName)"
$placeholderRoot = $jsonSerializer.DeserializeObject((Get-Content -Path $latestBrollPlan.FullName -Raw))

# ** THE FIX IS HERE: Use the correct key from the new JSON structure **
$placeholderData = $placeholderRoot.b_roll_plan 

if ($null -eq $placeholderData -or $placeholderData.Count -eq 0) {
    Write-Warning "No placeholders to process in the plan."
    exit 0
}
# --- Main Processing Loop ---
$brollAssetsDir = Join-Path $projectRoot "ai_working_files\59_generated_b_roll"
$baseName = [System.IO.Path]::GetFileNameWithoutExtension($latestBrollPlan.Name).Replace("_broll_plan", "")

for ($i = 0; $i -lt $placeholderData.Count; $i++) {
    $placeholder = $placeholderData[$i]
    $clipCounter = $i + 1

    # Read the correct keys from the JSON
    $timestamp = [double]$placeholder.timestamp_start
    $duration = [double]$placeholder.duration_seconds

    # Construct the placeholder filename using the same logic as the generator script
    $placeholderFileName = "{0}_placeholder_{1:0.00}s.mp4" -f $baseName, $timestamp
    $placeholderFilePath = Join-Path $brollAssetsDir $placeholderFileName

    if (-not (Test-Path $placeholderFilePath)) {
        Write-Warning " -> Skipping placeholder #$clipCounter. File not found at '$placeholderFilePath'."
        continue
    }
    
    Write-Host "`n -> Processing Placeholder #$($clipCounter) ('$placeholderFileName')"
    Write-Host "    Start=$timestamp sec, Duration=$duration sec"

    $fcpxml_temp = Join-Path $projectRoot ("Info_temp.{0:D4}.fcpxml" -f $clipCounter)

    $arguments = @(
        "`"$fcpxml_main`"",
        "`"$fcpxml_temp`"",
        "`"$projectRoot`"",
        "`"$placeholderFilePath`"", # Pass the full path to the generated placeholder
        $timestamp,
        $duration
    )

    & python $pythonScriptPath $arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Error "FATAL: 117_insert_broll_asset_clip.py failed for '$placeholderFileName'. Halting."
        if (Test-Path $fcpxml_temp) { Remove-Item $fcpxml_temp -Force }
        exit 1
    }

    Update-TimelineFile -currentFile $fcpxml_main -tempFile $fcpxml_temp
    if (Test-Path $fcpxml_temp) { Remove-Item $fcpxml_temp -Force -ErrorAction SilentlyContinue }
}


Write-Host "`n===========================================================" -ForegroundColor Cyan
Write-Host "  B-Roll Placeholder Insertion Complete." -ForegroundColor Cyan
Write-Host "==========================================================="
