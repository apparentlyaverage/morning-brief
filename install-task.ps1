# Registers (or updates) the Windows scheduled task that shows the briefing
# every morning.
#
#   .\install-task.ps1 -Time 06:30
#   .\install-task.ps1 -Time 07:00 -WeekdaysOnly
#   .\install-task.ps1 -Uninstall
#
# No admin rights needed - the task is registered for the current user only.

param(
    [string]$Time = '06:30',
    [switch]$WeekdaysOnly,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$taskName = 'MorningBriefing'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# The full sequence: music -> intro -> duck -> briefing -> restore.
# For the briefing on its own, point this at run-briefing.ps1 instead.
$runner = Join-Path $here 'morning-routine.ps1'

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed scheduled task '$taskName'." -ForegroundColor Green
    } else {
        Write-Host "No task named '$taskName' to remove." -ForegroundColor DarkGray
    }
    return
}

if (-not (Test-Path $runner)) { throw "Cannot find $runner" }

try { $parsed = [datetime]::ParseExact($Time, 'HH:mm', $null) }
catch { throw "-Time must look like 06:30 (24-hour). Got '$Time'." }

# -HoldSeconds matters: without it the routine ends on "press any key", which
# nobody does at 06:30, so Task Scheduler kills it at the time limit and
# reports failure every day. 30s is long enough to glance at, then it closes.
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -HoldSeconds 30' -f $runner) `
    -WorkingDirectory $here

if ($WeekdaysOnly) {
    $trigger = New-ScheduledTaskTrigger -Weekly -At $parsed `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday
} else {
    $trigger = New-ScheduledTaskTrigger -Daily -At $parsed
}

# StartWhenAvailable: if the machine was off at the scheduled time, run on wake.
# The battery settings stop Windows skipping the task on an unplugged laptop.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

# Interactive so the window is actually visible on the desktop.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Shows weather, SA news and today classes each morning.' `
    -Force | Out-Null

$when = if ($WeekdaysOnly) { "$Time, Monday to Friday" } else { "$Time, every day" }
Write-Host "Scheduled task '$taskName' installed - runs at $when." -ForegroundColor Green
Write-Host "Test it now with:  Start-ScheduledTask -TaskName $taskName" -ForegroundColor DarkGray
