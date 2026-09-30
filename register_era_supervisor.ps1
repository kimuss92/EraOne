<#
register_era_supervisor.ps1 - install/remove the Windows Scheduled Task that keeps
Era One alive by running era_supervisor.py at logon.

PLAIN ENGLISH
  era_supervisor.py is a watchdog that starts main.py and restarts it if it dies.
  But something has to start the WATCHDOG. This script registers a Scheduled Task
  so Windows launches the watchdog automatically every time you log in - and
  restarts the watchdog itself if IT ever dies. That closes the loop:

      Windows Task  --starts-->  era_supervisor.py  --starts-->  main.py (Era)
      (restarts the supervisor if it exits)         (restarts Era if it crashes)

  Since 2026-09-26 `install` registers the ERA ONE launcher instead
  (pythonw -m app.hud.launcher --background): no console window at logon. The
  launcher starts the supervisor windowless and exits, so Task Scheduler no
  longer babysits the supervisor process itself; the supervisor still restarts
  Era, and the ERA ONE shortcut restarts everything if needed. A task registered
  before that date still works: the supervisor detaches from its console.

USAGE (PowerShell, in this folder)
  # Install (or update) the task so Era starts at logon and runs now:
  .\register_era_supervisor.ps1 -Action install

  # See whether it's registered and running:
  .\register_era_supervisor.ps1 -Action status

  # Start / stop the task by hand (does NOT unregister it):
  .\register_era_supervisor.ps1 -Action start
  .\register_era_supervisor.ps1 -Action stop

  # Remove the task entirely (Era will no longer auto-start at logon):
  .\register_era_supervisor.ps1 -Action uninstall

  # Resume after an intentional shutdown (clears the stop file + restarts Era):
  .\register_era_supervisor.ps1 -Action wake

STOPPING ERA ON PURPOSE (so the watchdog does NOT revive it)
  Either stop the task (-Action stop) OR create the stop file the supervisor
  checks each cycle:   %USERPROFILE%\.era\era.stop   (delete it to allow revival).

NOTES
  - No admin rights needed: the task runs as the current user, at that user's logon.
  - -Python lets you pin a specific interpreter (e.g. a venv). Default: whatever
    `python` resolves to on PATH. The supervisor then reuses that same interpreter
    for main.py, so there is no interpreter mismatch.
  - Re-running `install` overwrites the existing task cleanly (idempotent).
#>

[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'status', 'wake')]
    [string]$Action = 'status',

    # Task name as it appears in Task Scheduler.
    [string]$TaskName = 'EraOneSupervisor',

    # Interpreter to run the supervisor with. Default: `python` from PATH.
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'

# --- Resolve paths relative to THIS script, so it works from any CWD ------------
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$SupervisorPy  = Join-Path $ScriptDir 'era_supervisor.py'

function Assert-Admin {
    # Registering/removing a task in the root Task Scheduler folder needs elevation.
    # Fail early with a clear instruction instead of a cryptic "Access is denied".
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        throw ("This action needs an ELEVATED PowerShell. Right-click PowerShell -> " +
               "'Run as administrator', then re-run:  .\register_era_supervisor.ps1 -Action $Action")
    }
}

function Resolve-PythonPath {
    param([string]$Candidate)
    # Absolute/relative path that exists -> use as-is (full path).
    if (Test-Path $Candidate) { return (Resolve-Path $Candidate).Path }
    # Otherwise look it up on PATH.
    $cmd = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python interpreter '$Candidate' not found (not a file and not on PATH). Pass -Python <full path>."
}

function Install-Task {
    Assert-Admin
    if (-not (Test-Path $SupervisorPy)) {
        throw "Cannot find era_supervisor.py next to this script (looked at: $SupervisorPy)."
    }
    $pythonPath = Resolve-PythonPath -Candidate $Python
    # Silent start (localhost HUD, 2026-09-26): run the ERA ONE launcher under
    # pythonw.exe (no console at all) in --background mode. It starts the
    # supervisor windowless if Era is not already running and opens nothing
    # itself — the backend opens the HUD at logon per its own setting.
    $pythonwPath = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
    if (-not (Test-Path $pythonwPath)) { $pythonwPath = $pythonPath }
    Write-Host "Python interpreter : $pythonwPath"
    Write-Host "Launcher           : -m app.hud.launcher --background"
    Write-Host "Working directory  : $ScriptDir"

    $action = New-ScheduledTaskAction -Execute $pythonwPath `
        -Argument "-m app.hud.launcher --background" -WorkingDirectory $ScriptDir

    # Fire at logon of the current user.
    $trigger = New-ScheduledTaskTrigger -AtLogOn

    # Run as the current user, only when logged on (interactive) so Era can see the
    # screen / speakers / mic. No stored password, no admin elevation.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited

    # Keep the supervisor alive: if IT ever exits/crashes, restart it; never let
    # Windows time-limit or auto-kill the long-running watchdog.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = unlimited runtime
    $settings.MultipleInstances = 'IgnoreNew'  # never run two supervisors at once

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null

    # Verify it actually landed before claiming success (a non-terminating CIM error
    # must never be reported as "[OK]").
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        throw "Register-ScheduledTask reported no error but the task '$TaskName' is not present."
    }
    Write-Host "`n[OK] Registered scheduled task '$TaskName' (runs at your logon)." -ForegroundColor Green

    # Start it now so you don't have to log out/in to get Era running.
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Host "[OK] Started '$TaskName' now. Era should be coming up." -ForegroundColor Green
    Write-Host "     Watch: logs\era_crash.log   Stop for good: -Action stop  (or create ~/.era/era.stop)"
}

function Uninstall-Task {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "Task '$TaskName' is not registered - nothing to remove."; return }
    Assert-Admin
    Stop-ScheduledTask  -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[OK] Removed scheduled task '$TaskName'. Era will no longer auto-start at logon." -ForegroundColor Yellow
    Write-Host "     (Any Era already running was left alone - close it manually if you want it down now.)"
}

function Start-Task {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        throw "Task '$TaskName' is not registered. Run: .\register_era_supervisor.ps1 -Action install"
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "[OK] Started '$TaskName'." -ForegroundColor Green
}

function Stop-Task {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "Task '$TaskName' is not registered - nothing to stop."; return
    }
    Stop-ScheduledTask -TaskName $TaskName
    Write-Host "[OK] Stopped the supervisor task '$TaskName'." -ForegroundColor Yellow
    Write-Host "     NOTE: this stops the WATCHDOG. An Era (main.py) it already launched"
    Write-Host "     may keep running until it exits. Create ~/.era/era.stop to keep Era down."
}

function Wake-Task {
    # Resume after an INTENTIONAL shutdown: clear the stop flag so the watchdog is
    # allowed to run Era again, then (re)start the supervisor task. Era wrote the
    # stop flag on shutdown, which also made the supervisor exit - so we start it.
    # Runs unelevated (start/stop of an existing task needs no admin).
    $stopFlag = Join-Path $env:USERPROFILE '.era\era.stop'
    if (Test-Path $stopFlag) {
        Remove-Item $stopFlag -Force
        Write-Host "[OK] Cleared stop flag ($stopFlag) - watchdog may run Era again." -ForegroundColor Green
    } else {
        Write-Host "No stop flag present (Era was not in intentional-shutdown state)."
    }
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "Task '$TaskName' is not registered. Run: .\register_era_supervisor.ps1 -Action install" -ForegroundColor Yellow
        return
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "[OK] (Re)started supervisor '$TaskName'; Era should be coming back up." -ForegroundColor Green
}

function Show-Status {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Host "Task '$TaskName': NOT REGISTERED." -ForegroundColor Yellow
        Write-Host "Install it with:  .\register_era_supervisor.ps1 -Action install"
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task '$TaskName'"
    Write-Host "  State          : $($t.State)"
    Write-Host "  Last run time  : $($info.LastRunTime)"
    Write-Host "  Last result    : $($info.LastTaskResult)"
    Write-Host "  Next run time  : $($info.NextRunTime)"
    $stopFlag = Join-Path $env:USERPROFILE '.era\era.stop'
    if (Test-Path $stopFlag) {
        Write-Host "  Stop flag      : PRESENT ($stopFlag) - supervisor will NOT revive Era." -ForegroundColor Yellow
    }
    $haltFlag = Join-Path $env:USERPROFILE '.era\era_supervisor_halted.flag'
    if (Test-Path $haltFlag) {
        Write-Host "  Halt flag      : PRESENT ($haltFlag) - last stop was a crash-loop halt." -ForegroundColor Red
        Write-Host "                   Fix the bug, delete that file, then -Action start."
    }
}

switch ($Action) {
    'install'   { Install-Task }
    'uninstall' { Uninstall-Task }
    'start'     { Start-Task }
    'stop'      { Stop-Task }
    'wake'      { Wake-Task }
    'status'    { Show-Status }
}
