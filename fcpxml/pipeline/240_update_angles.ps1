param(
    [string]$ProjectDir,
    [string]$ProjectsRoot,
    [string]$ProjectsFolder
)

# --- Configuration ---
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectsRoot)   { $ProjectsRoot = "E:\ghostedit\fcpxml\workspace\projects" }
if (-not $ProjectsFolder) { $ProjectsFolder = "E:\ghostedit\fcpxml\pipeline\projects" }
$projectsFolder    = $ProjectsFolder
$activeProjectFile = Join-Path $projectsFolder "active_project.txt"
$pythonScriptPath  = Join-Path $scriptRoot "103-reflow-and-more.py"

if (-not (Test-Path $pythonScriptPath)) {
    Write-Error "FATAL: Helper script not found at '$pythonScriptPath'."
    exit 1
}

# --- Resolve active project ---
if ($ProjectDir) {
    try {
        $projectRoot = (Resolve-Path -Path $ProjectDir -ErrorAction Stop).Path
    } catch {
        Write-Error "FATAL: Unable to resolve ProjectDir '$ProjectDir'. Error: $_"
        exit 1
    }
    $active_project = Split-Path $projectRoot -Leaf
} else {
    try {
        $active_project = (Get-Content -Path $activeProjectFile -ErrorAction Stop).Trim()
    } catch {
        Write-Error "FATAL: Could not read active project file '$activeProjectFile'. Error: $_"
        exit 1
    }
    if (-not $active_project) {
        Write-Error "FATAL: Active project file '$activeProjectFile' is empty."
        exit 1
    }
    $projectRoot = Join-Path $ProjectsRoot $active_project
}

if (-not (Test-Path $projectRoot)) {
    Write-Error "FATAL: Project root '$projectRoot' does not exist."
    exit 1
}

$workspace = Join-Path $projectRoot "ai_working_files"
$fcpxml_current = Join-Path $projectRoot "Info.fcpxml"
$fcpxml_temp_out = Join-Path $projectRoot "Info.temp_angles.fcpxml"

if (-not (Test-Path $fcpxml_current)) {
    Write-Error "FATAL: Required Info.fcpxml not found at '$fcpxml_current'."
    exit 1
}

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

    Write-Host "  - Applying cut at $cut_seconds s -> $target_angle"

    & python $pythonScriptPath `
        --fcpxml $fcpxml_current `
        --out $fcpxml_temp_out `
        apply-angle-change `
        --cut-seconds $cut_seconds `
        --target-angle-name $target_angle

    if ($LASTEXITCODE -ne 0) {
        Write-Error "    - Python script failed (exit $LASTEXITCODE). Halting."
        if (Test-Path $fcpxml_temp_out) { Remove-Item $fcpxml_temp_out -Force }
        exit 1
    }

    if (Test-Path $fcpxml_temp_out) {
        $timestamp = Get-Date -Format "yyyyMMddHHmmss"
        $rand = Get-Random -Minimum 1000 -Maximum 9999
        $backupPath = Join-Path (Split-Path $fcpxml_current) "Info.bak.$timestamp_$rand.fcpxml"
        Move-Item -Path $fcpxml_current -Destination $backupPath -Force
        Move-Item -Path $fcpxml_temp_out -Destination $fcpxml_current -Force
        Write-Host "    - Edit applied. Backup: $backupPath"
    } else {
        Write-Error "    - Expected temp file '$fcpxml_temp_out' was not created. Halting."
        exit 1
    }
}

Write-Host "`n[+] All angle changes applied successfully."
