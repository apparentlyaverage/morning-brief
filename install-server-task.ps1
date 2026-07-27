# Keeps the local web server running, so the dashboard is there whenever you
# want it - not only just after the morning routine has fired.
#
#   .\install-server-task.ps1
#   .\install-server-task.ps1 -Uninstall
#
# Without this, ui_server.py only runs while settings.ps1 is open or shortly
# after morning-routine.ps1 has started it. Opening the installed app at any
# other time gives "connection refused", which for something you installed as
# an app is simply broken.
#
# No admin rights needed - registered for the current user only.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$taskName = 'MorningBriefServer'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$server = Join-Path $here 'ui_server.py'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed '$taskName'. The dashboard will only be up when settings.ps1 or the morning routine starts it." -ForegroundColor Green
    } else {
        Write-Host "No task named '$taskName' to remove." -ForegroundColor DarkGray
    }
    return
}

if (-not (Test-Path $server)) { throw "Cannot find $server" }

$python = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw 'python is not on your PATH.' }

# pythonw over python where available: it runs without a console window, so
# nothing flashes up at login and no black box sits in the taskbar all day.
$action = New-ScheduledTaskAction -Execute $python `
    -Argument ('"{0}" --no-open' -f $server) -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# A short delay keeps it out of the way of everything else starting at login.
$trigger.Delay = 'PT20S'

# No execution time limit: this is a server, it is meant to stay up. IgnoreNew
# means logging in twice cannot leave two of them fighting over port 8765.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Runs the Morning Brief dashboard server so the app is always reachable.' `
    -Force | Out-Null

Write-Host "Installed '$taskName' - the dashboard server now starts when you log in." -ForegroundColor Green
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $taskName" -ForegroundColor DarkGray
