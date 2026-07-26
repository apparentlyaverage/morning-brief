# Wrapper the scheduled task calls. Renders the briefing in a window and
# leaves a plain-text copy next to the script.
#
# Manual use:  .\run-briefing.ps1
#              .\run-briefing.ps1 -Quiet      (write the file, show nothing)

param(
    [switch]$Quiet,
    [switch]$NoSpeak,       # render it, but stay silent
    [int]$HoldSeconds = 0   # 0 = wait for a keypress before closing
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'briefing.py'
$outFile = Join-Path $here 'briefing.txt'
$spokenFile = Join-Path $here 'briefing-spoken.txt'
$speaker = Join-Path $here 'speak.ps1'

# Emoji and box-drawing characters need a UTF-8 console.
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Warning 'python not found on PATH.'
    exit 1
}

if ($Quiet) {
    & $python $script --no-color --out $outFile --speak-out $spokenFile | Out-Null
    exit $LASTEXITCODE
}

$Host.UI.RawUI.WindowTitle = 'Morning Briefing'
Clear-Host
& $python $script --out $outFile --speak-out $spokenFile

if ($LASTEXITCODE -ne 0) {
    Write-Warning "briefing.py exited with code $LASTEXITCODE"
}

# Read it aloud. Speech settings live in config.json so you can change the
# voice without touching this script.
if (-not $NoSpeak -and (Test-Path $spokenFile)) {
    try {
        $speech = (Get-Content (Join-Path $here 'config.json') -Raw -Encoding UTF8 |
                   ConvertFrom-Json).speech
        if ($null -eq $speech -or $speech.enabled) {
            $voice = if ($speech) { [string]$speech.voice } else { '' }
            $onlineVoice = if ($speech) { [string]$speech.online_voice } else { '' }
            $rate = if ($speech -and $null -ne $speech.rate) { [int]$speech.rate } else { 0 }
            $forceOffline = ($speech -and $speech.engine -and $speech.engine -ne 'online')
            & $speaker -File $spokenFile -Voice $voice -OnlineVoice $onlineVoice `
                -Rate $rate -Offline:$forceOffline
        }
    } catch {
        Write-Warning "Couldn't read the briefing aloud: $($_.Exception.Message)"
    }
}

if ($HoldSeconds -gt 0) {
    Write-Host ("  Closing in {0}s..." -f $HoldSeconds) -ForegroundColor DarkGray
    Start-Sleep -Seconds $HoldSeconds
} else {
    Write-Host '  Press any key to close.' -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { Start-Sleep -Seconds 30 }
}
