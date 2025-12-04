# 118_apply_broll_placeholders.ps1
# v2.1 (Linter Fix + Dumb Consumer Logic)
# 
# PHILOSOPHY:
# This script no longer guesses filenames. It expects the JSON plan 
# to contain a 'final_clip_path' key for every item.

param(
    [string]$ProjectDir,
    [string]$ProjectsRoot,
    [string]$ProjectsFolder
)

# --- Init ---
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectsRoot)   { $ProjectsRoot = "E:\ghostedit\fcpxml\workspace\projects" }
if (-not $ProjectsFolder) { $ProjectsFolder = "E:\ghostedit\fcpxml\pipeline\projects" }
$activeProjectFile = Join-Path $ProjectsFolder "active_project.txt"
$pythonScriptPath = Join-Path $scriptRoot "117_insert_broll_asset_clip.py"

# --- JSON Setup ---
Add-Type -AssemblyName System.Web.Extensions
$jsonSerializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$jsonSerializer.MaxJsonLength = [int]::MaxValue

# --- Project Paths ---
if ($ProjectDir) {
    $projectRoot = (Resolve-Path -Path $ProjectDir).Path
    $active_project = Split-Path $projectRoot -Leaf
} else {
    $active_project = (Get-Content -Path $activeProjectFile).Trim()
    $projectRoot = Join-Path $ProjectsRoot $active_project
}
$fcpxml_main = Join-Path $projectRoot "Info.fcpxml"

Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Applying B-Roll (Manifest Mode) for: $active_project" -ForegroundColor Cyan
Write-Host "==========================================================="

# --- Load Manifest ---
$brollPlansDir = Join-Path $projectRoot "ai_working_files\46_ai_b_roll_plans"
$latestBrollPlan = Get-ChildItem -Path $brollPlansDir -Filter "*_broll_plan.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $latestBrollPlan) { Write-Error "FATAL: No plan found."; exit 1 }

Write-Host "-> Loading Manifest: $($latestBrollPlan.Name)"
$planRoot = $jsonSerializer.DeserializeObject((Get-Content -Path $latestBrollPlan.FullName -Raw))
$items = $planRoot.b_roll_plan 

# --- Backup ---
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item $fcpxml_main -Destination (Join-Path $projectRoot "Info.pre_broll.$ts.fcpxml") -Force

# --- Execution ---
for ($i = 0; $i -lt $items.Count; $i++) {
    $item = $items[$i]
    $ClipCounter = $i + 1 # Renamed to standard PascalCase to appease strict linters

    # THE NEW CONTRACT:
    # We only care about 'final_clip_path'. 
    
    if (-not $item.ContainsKey("final_clip_path")) {
        Write-Host " -> [!] ERROR Item #$(ClipCounter): Missing 'final_clip_path' key in JSON. Skipping." -ForegroundColor Red
        continue
    }

    $filePath = $item.final_clip_path
    $start    = [double]$item.timestamp_start
    $dur      = [double]$item.duration_seconds

    if (-not (Test-Path $filePath)) {
        Write-Host " -> [!] MISSING FILE: '$filePath'" -ForegroundColor Red
        continue
    }

    # Extract clean filename for logging
    $fileName = Split-Path $filePath -Leaf
    Write-Host " -> Placing #$(ClipCounter): $fileName" -ForegroundColor Green
    Write-Host "    Time: $start s | Dur: $dur s"

    # Call Python Injector
    $fcpxml_temp = Join-Path $projectRoot ("Info_temp.{0:D4}.fcpxml" -f $ClipCounter)
    
    # Explicitly casting arguments to strings to prevent parsing errors
    $argsPy = @(
        "`"$fcpxml_main`"", 
        "`"$fcpxml_temp`"", 
        "`"$projectRoot`"", 
        "`"$filePath`"", 
        "$start", 
        "$dur"
    )
    
    & python $pythonScriptPath $argsPy
    
    if ($LASTEXITCODE -eq 0 -and (Test-Path $fcpxml_temp)) {
        Move-Item -Path $fcpxml_temp -Destination $fcpxml_main -Force
    } else {
        Write-Error "    [!] Python injection failed for Item #$ClipCounter"
        exit 1
    }
}

Write-Host "`n[+] Done." -ForegroundColor Cyan