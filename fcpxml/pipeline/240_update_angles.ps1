# update_angles.ps1
# Version: 2025-09-03
# Purpose: Apply AI pacing plan cuts into info.fcpxml (single-take, SRT-based workflow)

# --- Configuration ---
$basePath = "E:\ghostedit\assembly_line\projects\"
$projectsFolder = "E:\ghostedit\fcpxml\pipeline\projects\"
$activeProjectFile = Join-Path $projectsFolder "active_project.txt"

# --- Resolve active project ---
try {
    $active_project = Get-Content -Path $activeProjectFile -ErrorAction Stop
}
catch {
    Write-Error "FATAL: Could not read active project file at '$activeProjectFile'. Error: $_"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($active_project)) {
    Write-Error "FATAL: Active project file is empty."
    exit 1
}

$projectRoot = Join-Path $basePath $active_project
$workspace   = Join-Path $projectRoot "ai_working_files"

# --- Locate input fcpxml (ONLY info.fcpxml) ---
$fcpxml_current = Join-Path $projectRoot "info.fcpxml"
if (-not (Test-Path $fcpxml_current)) {
    Write-Error "FATAL: Required info.fcpxml not found at '$fcpxml_current'."
    exit 1
}

$fcpxml_temp_out = Join-Path $projectRoot "temp_out.fcpxml"

# --- Locate pacing plan (latest subtitle*_changes.json) ---
$plansDir = Join-Path $workspace "45_ai_pacing_plans"
if (-not (Test-Path $plansDir)) {
    Write-Error "FATAL: Plans directory not found at '$plansDir'."
    exit 1
}

$latestPlan = Get-ChildItem -Path $plansDir -Filter "subtitle*_changes.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $latestPlan) {
    Write-Error "FATAL: No 'subtitle*_changes.json' pacing plan found in '$plansDir'."
    exit 1
}

Write-Host "Processing pacing plan: $($latestPlan.FullName)"
$pacingData = Get-Content -Path $latestPlan.FullName -Raw | ConvertFrom-Json

# --- Gather all cuts ---
$allChanges = $pacingData.angle_changes

if (-not $allChanges) {
    Write-Error "FATAL: No angle changes found in pacing plan."
    exit 1
}

# --- Normalize fields ---
function Get-TargetAngleName {
    param($angleChoice)
    if ([string]::IsNullOrWhiteSpace($angleChoice)) { return $null }
    $ac = $angleChoice.Trim().ToLower()
    # Use the correct names from the FCPXML
    if ($ac -eq "angle a") { return "Angle 2" }
    if ($ac -eq "angle b") { return "Angle 1" }
    return $null
}
$normalized =
    $allChanges |
    Where-Object { $_.propose_cut -eq $true } |
    ForEach-Object {
        $cutTime = $null
        if ($_.PSObject.Properties.Name -contains 'remapped_cut_time' -and $_.remapped_cut_time -ne $null) {
            $cutTime = [double]$_.remapped_cut_time
        } elseif ($_.PSObject.Properties.Name -contains 'absolute_cut_time' -and $_.absolute_cut_time -ne $null) {
            $cutTime = [double]$_.absolute_cut_time
        }
        if ($cutTime -eq $null) { return }

        $target = $null
        if ($_.PSObject.Properties.Name -contains 'target_angle_name' -and $_.target_angle_name) {
            $target = $_.target_angle_name
        } else {
            $target = Get-TargetAngleName -angleChoice $_.angle_choice
        }
        if (-not $target) { $target = "Angle A" }

        [pscustomobject]@{
            cut_seconds       = $cutTime
            target_angle_name = $target
        }
    } |
    Sort-Object { [double]$_.cut_seconds }

if (-not $normalized) {
    Write-Error "FATAL: No usable cuts after normalization."
    exit 1
}

# --- Apply changes ---
foreach ($chg in $normalized) {
    $cut_seconds = [double]$chg.cut_seconds
    $target_angle = $chg.target_angle_name

    Write-Host "  - Applying cut at $cut_seconds s → $target_angle"

    & python 103-reflow-and-more.py `
        --fcpxml $fcpxml_current `
        --out $fcpxml_temp_out `
        apply-angle-change `
        --cut-seconds $cut_seconds `
        --target-angle-name $target_angle

    if (Test-Path $fcpxml_temp_out) {
        Move-Item -Path $fcpxml_temp_out -Destination $fcpxml_current -Force
        Write-Host "    - Edit applied."
    } else {
        Write-Error "    - Python script failed. Aborting this plan."
        break
    }
}

Write-Host "`n[+] All angle changes applied successfully." -ForegroundColor Green
