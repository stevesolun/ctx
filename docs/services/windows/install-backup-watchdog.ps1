# install-backup-watchdog.ps1
#
# Registers the ctx backup watchdog as a Windows Scheduled Task.
# The task runs under the current user, starts at logon, and restarts
# on failure. Nothing elevated — a standard user can install, run, and
# remove the task without administrator rights.
#
# Usage:
#   # From a PowerShell prompt inside this repo:
#   pwsh -File docs/services/windows/install-backup-watchdog.ps1
#
# Flags:
#   -RepoPath    Absolute path to this ctx checkout. Defaults to the
#                repo the script lives in.
#   -Python      Absolute path to the Python interpreter. Auto-detected
#                via `where python` when omitted.
#   -Interval    Seconds between polls. Default 60.
#   -Uninstall   Remove the task and exit.
#
# Inspect afterwards:
#   Get-ScheduledTask -TaskName 'ClaudeBackupWatchdog'
#   Get-ScheduledTaskInfo -TaskName 'ClaudeBackupWatchdog'
#
# Remove:
#   pwsh -File docs/services/windows/install-backup-watchdog.ps1 -Uninstall

[CmdletBinding()]
param(
    [string]$RepoPath = (Resolve-Path "$PSScriptRoot\..\..\..").Path,
    [string]$Python = $null,
    [int]$Interval = 60,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ClaudeBackupWatchdog'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[uninstall] removed scheduled task $TaskName"
    } else {
        Write-Host "[uninstall] task $TaskName is not registered; nothing to do"
    }
    return
}

# --- Validate inputs ----------------------------------------------------------

$MirrorScript = Join-Path $RepoPath 'src\backup_mirror.py'
if (-not (Test-Path $MirrorScript)) {
    throw "backup_mirror.py not found under $RepoPath. Pass -RepoPath correctly."
}

if (-not $Python) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $Python) {
        throw "Python interpreter not found on PATH. Pass -Python <path>."
    }
}
if (-not (Test-Path $Python)) {
    throw "Python path does not exist: $Python"
}

if ($Interval -lt 5 -or $Interval -gt 3600) {
    throw "Interval must be between 5 and 3600 seconds (got $Interval)."
}

# --- Build the task ----------------------------------------------------------

$Arguments = "`"$MirrorScript`" watchdog --interval $Interval"

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Arguments `
    -WorkingDirectory $RepoPath

# Run at user logon. The watchdog itself sleeps between polls, so we
# don't need a repetition trigger on top.
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings: allow on battery, restart on failure, no time limit.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$Description = "Snapshots ~/.claude/ on change. Source: $RepoPath"

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description $Description

# Replace any previous registration.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -InputObject $Task | Out-Null

# Kick it off now so the user sees a snapshot folder appear.
Start-ScheduledTask -TaskName $TaskName

Write-Host "[install] registered scheduled task $TaskName"
Write-Host "          python:   $Python"
Write-Host "          script:   $MirrorScript"
Write-Host "          interval: ${Interval}s"
Write-Host ""
Write-Host "Inspect:  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "Remove:   pwsh -File '$PSCommandPath' -Uninstall"
