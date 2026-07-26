# Opens the settings page for the morning briefing.
#
#   .\settings.ps1
#
# Starts a small local web server and opens it in your browser. It only
# listens on 127.0.0.1, so nothing outside this machine can reach it.
# Close the window (or press Ctrl+C) when you're done - the briefing itself
# does not need this running.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Warning 'python not found on PATH.'; exit 1 }

# Already running? Just open the tab again rather than failing on a bound port.
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host 'Settings server is already running.' -ForegroundColor DarkGray
    Start-Process 'http://127.0.0.1:8765'
    exit 0
}

$env:PYTHONIOENCODING = 'utf-8'
& $python (Join-Path $here 'ui_server.py')
