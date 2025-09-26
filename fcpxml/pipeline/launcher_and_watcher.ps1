# launch_and_monitor.ps1
#
# A PowerShell wrapper to launch all Ghostedit Assembly Line watchers
# and provide a unified, live view of all relevant log files.
#
# USAGE:
# 1. Edit the CONFIGURATION variables below to match your environment.
# 2. Open a PowerShell terminal in this script's directory.
# 3. Run the script: .\launch_and_monitor.ps1
# 4. Press Ctrl+C to stop monitoring and shut down all watcher processes.
#

#----------------------------------------------------------------------
# ## CONFIGURATION ##
# Adjust these paths to match your folder structure.
#----------------------------------------------------------------------
$watchDir = "E:\ghostedit\assembly_line\TODO"
$inProgressDir = "E:\ghostedit\assembly_line\IN_PROGRESS"
$projectsBaseDir = "E:\ghostedit\assembly_line\projects"
$ffmpegDir = "C:\path\to\your\ffmpeg\bin" # Example: C:\ffmpeg\bin
$sharedScriptsDir = "E:\ghostedit\fcpxml\shared"

# Use pythonw.exe to run scripts in the background without a new console window.
$pythonExecutable = "pythonw.exe"

#----------------------------------------------------------------------
# ## SCRIPT BODY ##
# No need to edit below this line.
#----------------------------------------------------------------------
$ErrorActionPreference = "Stop"
$scriptRoot = $PSScriptRoot

# Array to hold the background processes so we can clean them up later
$watcherProcesses = @()

# Define the watchers and their arguments
$watchers = @(
    @{
        Name = "001_intake_watcher.py"
        Args = @(
            "--watch-dir", "'$watchDir'",
            "--in-progress-dir", "'$inProgressDir'",
            "--projects-base-dir", "'$projectsBaseDir'",
            "--ffmpeg-dir", "'$ffmpegDir'"
        )
    },
    @{
        Name = "003_aroll_watcher.py"
        Args = @() # This watcher is self-configuring
    },
    @{
        Name = "005_aroll_compile_watcher.py"
        Args = @() # This watcher is self-configuring
    },
    @{
        Name = "007_broll_watcher.py"
        Args = @() # This watcher is self-configuring
    }
)

# Define all log files to monitor (watchers and doers)
# --- THIS SECTION IS CORRECTED ---
$logFilesToMonitor = @(
    (Join-Path $scriptRoot "001_watcher.log"),
    (Join-Path $scriptRoot "002_setup_project.log"),
    (Join-Path $scriptRoot "003_aroll_watcher.log"),
    (Join-Path $scriptRoot "004_aroll_doer.log"),
    (Join-Path $scriptRoot "005_aroll_compile_watcher.log"),
    (Join-Path $scriptRoot "006_compile_and_drop_aroll.log"),
    (Join-Path $scriptRoot "007_broll_watcher.log"),
    (Join-Path $scriptRoot "008_broll_doer.log")
)

# Optional: Clear old log files before starting
# Uncomment the line below if you want to start with fresh logs each time.
# $logFilesToMonitor | ForEach-Object { if (Test-Path $_) { Clear-Content $_ } }

# Main logic block with cleanup
try {
    Write-Host "--- Starting Ghostedit Assembly Line Watchers ---" -ForegroundColor Green

    # Start each watcher as a background process
    foreach ($watcher in $watchers) {
        $scriptPath = Join-Path $scriptRoot $watcher.Name
        if (-not (Test-Path $scriptPath)) {
            Write-Warning "Watcher script not found: $scriptPath. Skipping."
            continue
        }
        
        Write-Host "Starting $($watcher.Name)..."
        $process = Start-Process -FilePath $pythonExecutable -ArgumentList "$scriptPath $($watcher.Args -join ' ')" -PassThru -NoNewWindow
        $watcherProcesses += $process
    }
    
    Write-Host "`n--- All watchers are running in the background. ---`n" -ForegroundColor Green
    Write-Host "--- Now monitoring all log files. Press Ctrl+C to exit. ---`n" -ForegroundColor Cyan

    # Wait a moment for log files to be created if they don't exist
    Start-Sleep -Seconds 2

    # Tail all log files simultaneously
    Get-Content -Path $logFilesToMonitor -Wait -Tail 10

}
catch {
    Write-Error "An unexpected error occurred: $_"
}
finally {
    # This block runs when you press Ctrl+C
    Write-Host "`n--- Shutting down watcher processes... ---" -ForegroundColor Yellow
    foreach ($proc in $watcherProcesses) {
        # Check if the process is still running before trying to stop it
        if ($proc -and -not $proc.HasExited) {
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction Stop
                Write-Host "Stopped process $($proc.Id) ($($proc.ProcessName))"
            }
            catch {
                # Handle cases where the process might have already terminated
                Write-Warning "Could not stop process $($proc.Id). It may have already exited."
            }
        }
    }
    Write-Host "--- Cleanup complete. ---" -ForegroundColor Green
}