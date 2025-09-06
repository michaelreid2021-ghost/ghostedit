# 116_build_timeline_from_takes.ps1
# v1.3 (2025-09-04)
# - REMOVED: The '--is-first-take' logic. The orchestrator now simply
#   calls the Python script for every take and relies on the Python
#   script's enhanced logic to handle replacing vs. inserting.
# - Orchestrates the creation of a new timeline from clean takes.
# - For each clean take, it calls the python script to create a subclip resource
#   and place it on the timeline spine.

# --- Configuration ---
$basePath = "E:\davinci\projects\"
$projectsFolder = "E:\ai_editing_assistant\fcpxml\projects\"
$activeProjectFile = Join-Path $projectsFolder "active_project.txt"
$pythonScriptPath = ".\115_timeline_builder.py"

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
$fcpxml_temp = Join-Path $projectRoot "Info_temp.fcpxml"

# --- Helper Function ---
function Update-TimelineFile {
    param([string]$currentFile, [string]$tempFile)
    if (Test-Path $tempFile) {
        # The Python script now manages its own backups if needed.
        # This function will just overwrite the main file with the temp file.
        Move-Item -Path $tempFile -Destination $currentFile -Force
        Write-Host "SUCCESS: Timeline updated." -ForegroundColor Green
    } else {
        Write-Error "FAILURE: Python script did not create the output file '$tempFile'. Halting."
        exit 1
    }
}

# =================================================================================
# SCRIPT EXECUTION STARTS HERE
# =================================================================================
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Building Fresh Timeline from Clean Takes for: $active_project" -ForegroundColor Cyan
Write-Host "==========================================================="

# --- Load Markup Data ---
$markupPath = Join-Path $projectRoot "ai_working_files\44_producers_markup.json"
if (-not (Test-Path $markupPath)) { Write-Error "FATAL: Markup file not found at '$markupPath'."; exit 1 }
$markupData = $jsonSerializer.DeserializeObject((Get-Content -Path $markupPath -Raw))

# --- Create a single, definitive backup of the original timeline before processing ---
$initialTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$initialBackupPath = Join-Path $projectRoot "Info.bak.PRE-SCRIPT.$($initialTimestamp).fcpxml"
Write-Host " -> Creating initial backup of timeline at: $initialBackupPath" -ForegroundColor Yellow
Copy-Item -Path $fcpxml_main -Destination $initialBackupPath -Force

# --- Main Processing Loop ---
foreach ($scene in $markupData) {
    $sourceMediaName = $scene.multicam_name
    if ([string]::IsNullOrEmpty($sourceMediaName)) { Write-Warning "Scene is missing 'multicam_name', skipping."; continue }

    # We need to read the FCPXML content fresh inside the loop to get the correct source ID
    [xml]$fcpxmlContent = Get-Content -Path $fcpxml_main
    $mediaNode = $fcpxmlContent | Select-Xml -XPath "//resources/media[@name='$sourceMediaName']" | Select-Object -ExpandProperty Node
    if ($null -eq $mediaNode) { Write-Error "FATAL: Could not find media with name='$sourceMediaName' in the FCPXML. This can happen if the original timeline has been modified."; continue }
    $sourceMediaRefId = $mediaNode.id
    Write-Host "Found Source Media: Name='$sourceMediaName', ID='$sourceMediaRefId'"

    $finalTakes = $scene.producers_markup.final_edit_takes
    if ($null -eq $finalTakes -or $finalTakes.Count -eq 0) {
        Write-Warning " -> No 'final_edit_takes' found for '$sourceMediaName'. Skipping scene."; continue
    }

    Write-Host " -> Found $($finalTakes.Count) clean takes to process for this scene."
    
    for ($i = 0; $i -lt $finalTakes.Count; $i++) {
        $take = $finalTakes[$i]
        $takeCounter = $i + 1
        
        $start = [Math]::Max(0, ($take.start_seconds - 0.5))
        $duration = ($take.end_seconds - $start) + 0.5
        $subclipName = "$($sourceMediaName) - Subclip $($takeCounter)".Trim()

        Write-Host "`n -> Processing Take #$($takeCounter) ('$subclipName')"
        
        $arguments = @(
            "--fcpxml", "`"$fcpxml_main`"",
            "--out", "`"$fcpxml_temp`"",
            "process-take",
            "--source-ref", $sourceMediaRefId,
            "--start-seconds", $start,
            "--duration-seconds", $duration,
            "--name", "`"$subclipName`""
        )

        & python $pythonScriptPath $arguments
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Python script failed. Halting execution."
            exit 1
        }
        Update-TimelineFile -currentFile $fcpxml_main -tempFile $fcpxml_temp
    }
}

Write-Host "`n===========================================================" -ForegroundColor Cyan
Write-Host "  Timeline build complete." -ForegroundColor Cyan
Write-Host "==========================================================="