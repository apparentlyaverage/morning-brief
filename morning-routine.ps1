# The full morning sequence:
#   open Cider -> play the playlist -> let it run -> fade the music down ->
#   Mark reads the briefing -> fade the music back up.
#
#   .\morning-routine.ps1                 the real thing (3 minute intro)
#   .\morning-routine.ps1 -IntroSeconds 5 quick end-to-end test
#   .\morning-routine.ps1 -NoMusic        briefing only
#
# This is what the 06:30 scheduled task runs.

param(
    [int]$IntroSeconds = -1,   # -1 = use config
    [switch]$NoMusic,
    [switch]$NoSpeak,
    [switch]$NoDashboard,      # skip the pop-up dashboard
    [int]$HoldSeconds = 0      # 0 = wait for a keypress at the end
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg = Get-Content (Join-Path $here 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$cider = $cfg.cider
$speech = $cfg.speech

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Warning 'python not found on PATH.'; exit 1 }

$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$outFile = Join-Path $here 'briefing.txt'
$spokenFile = Join-Path $here 'briefing-spoken.txt'
$dataFile = Join-Path $here 'briefing_data.json'
$intro = if ($IntroSeconds -ge 0) { $IntroSeconds } elseif ($cider) { [int]$cider.intro_seconds } else { 0 }

# ------------------------------------------------------------------ Cider
# 127.0.0.1, not localhost - localhost tries IPv6 (::1) first and Cider binds
# IPv4 only, costing about two seconds on every single call.
$BASE = 'http://127.0.0.1:10767'
$APPID = 'CiderCollective.Cider_a6qxe093bx5xj!App'
$musicOn = $false

function Cider-Api {
    param([string]$Method = 'GET', [Parameter(Mandatory)][string]$Path, $Body)
    $p = @{
        Uri = "$BASE$Path"; Method = $Method
        Headers = @{ 'apptoken' = [string]$cider.token }
        TimeoutSec = 10; UseBasicParsing = $true
    }
    if ($Method -ne 'GET') {
        $p.Body = if ($null -ne $Body) { $Body | ConvertTo-Json -Depth 6 -Compress } else { '{}' }
        $p.ContentType = 'application/json'
    }
    Invoke-RestMethod @p
}

function Cider-Listening {
    (Get-NetTCPConnection -LocalPort 10767 -State Listen -ErrorAction SilentlyContinue) -ne $null
}

function Start-Music {
    if (-not (Get-Process -Name 'Cider' -ErrorAction SilentlyContinue)) {
        Write-Host 'Opening Cider...' -ForegroundColor Cyan
        Start-Process "shell:AppsFolder\$APPID"
    }
    $deadline = (Get-Date).AddSeconds(45)
    while (-not (Cider-Listening)) {
        if ((Get-Date) -gt $deadline) { throw 'Cider API never came up on port 10767.' }
        Start-Sleep -Milliseconds 500
    }

    # Set the volume before pressing play, so it never starts at full blast.
    Cider-Api POST '/api/v1/playback/volume' @{ volume = [double]$cider.play_volume } | Out-Null

    # Page through the whole library. The API caps a page at 100, and a bigger
    # library would otherwise hide playlists and fail to find the right one.
    $items = @()
    $offset = 0
    while ($offset -lt 5000) {
        $page = Cider-Api POST '/api/v1/amapi/run-v3' @{ path = "/v1/me/library/playlists?limit=100&offset=$offset" }
        $batch = $page.data.data
        if (-not $batch) { break }
        $items += $batch
        if (-not $page.data.next) { break }
        $offset += 100
    }

    $want = [string]$cider.playlist
    $match = $items | Where-Object { $_.attributes.name -and $_.attributes.name.Trim() -ieq $want.Trim() } | Select-Object -First 1
    if (-not $match) { $match = $items | Where-Object { $_.attributes.name -like "*$want*" } | Select-Object -First 1 }
    if (-not $match) { throw "No playlist matching '$want' among $($items.Count) playlists." }

    Cider-Api POST '/api/v1/playback/play-item-href' @{ href = "/v1/me/library/playlists/$($match.id)" } | Out-Null
    Start-Sleep -Seconds 3
    $np = Cider-Api GET '/api/v1/playback/now-playing'
    Write-Host ("Playing '{0}' - {1}" -f $match.attributes.name, $np.info.name) -ForegroundColor Green
    return $true
}

function Fade-Volume([double]$From, [double]$To, [int]$Ms) {
    $steps = 12
    for ($i = 1; $i -le $steps; $i++) {
        $v = $From + (($To - $From) * $i / $steps)
        try { Cider-Api POST '/api/v1/playback/volume' @{ volume = [Math]::Round($v, 3) } | Out-Null } catch {}
        Start-Sleep -Milliseconds ([Math]::Max(10, $Ms / $steps))
    }
}

# ------------------------------------------------------------------ run
$Host.UI.RawUI.WindowTitle = 'Morning Briefing'

# Windows master volume. Whatever it was left at last night, the briefing
# should start from a known level - and unmuted, or none of this is audible.
if ($cider -and $null -ne $cider.system_volume) {
    try {
        Add-Type -Path (Join-Path $here 'TtsHelper.dll')
        [SystemVolume]::Set([float]$cider.system_volume)
        Write-Host ("System volume -> {0:N0}%" -f ([double]$cider.system_volume * 100)) -ForegroundColor DarkGray
    } catch {
        Write-Warning "Couldn't set system volume: $($_.Exception.Message)"
    }
}

if (-not $NoMusic -and $cider -and $cider.enabled -and $cider.token) {
    try { $musicOn = Start-Music }
    catch { Write-Warning "Music didn't start: $($_.Exception.Message)" }
}

# Build the briefing while the music plays, so there's no pause afterwards.
$started = Get-Date

# Run the briefing with ErrorActionPreference relaxed. Under 'Stop', any line
# Python writes to stderr - a warning, a traceback - is promoted to a
# terminating error and kills the whole routine before the checks below run.
# Judge success by the exit code instead.
$briefingOk = $false
$previousEAP = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $python (Join-Path $here 'briefing.py') --out $outFile --speak-out $spokenFile --data-out $dataFile 2>&1 |
        ForEach-Object { Write-Host $_ }
    $briefingOk = ($LASTEXITCODE -eq 0)
} catch {
    Write-Warning "briefing.py could not be run: $($_.Exception.Message)"
} finally {
    $ErrorActionPreference = $previousEAP
}
if (-not $briefingOk) { Write-Warning "briefing.py failed (exit $LASTEXITCODE)" }

# Guard against reading a stale briefing. If the build failed, the file on disk
# is yesterday's - and confidently reading yesterday's weather and headlines as
# though they were today's is worse than admitting the fetch failed.
$spokenFresh = $false
if (Test-Path $spokenFile) {
    $ageMinutes = ((Get-Date) - (Get-Item $spokenFile).LastWriteTime).TotalMinutes
    $spokenFresh = $briefingOk -and ($ageMinutes -lt 30)
    if (-not $spokenFresh) {
        Write-Warning ("Briefing is stale ({0:N0} min old) - it will not be read out." -f $ageMinutes)
        Set-Content $spokenFile -Encoding utf8 -Value (
            "Good morning. I could not fetch this morning's briefing - " +
            "the news, weather or both were unreachable. Everything else is still on the dashboard.")
        $spokenFresh = $true   # the apology is fresh, and is safe to read
    }
}

# Pop the dashboard up. Nothing is running at 06:30, so start the little web
# server first - it hosts both the dashboard and the settings page.
if (-not $NoDashboard) {
    try {
        if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath $python `
                -ArgumentList @("`"$(Join-Path $here 'ui_server.py')`"", '--no-open') `
                -WindowStyle Hidden
            $deadline = (Get-Date).AddSeconds(20)
            while (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) {
                if ((Get-Date) -gt $deadline) { throw 'dashboard server did not start' }
                Start-Sleep -Milliseconds 400
            }
        }
        Start-Process 'http://127.0.0.1:8765/dashboard'
        Write-Host 'Dashboard opened.' -ForegroundColor DarkGray
    } catch {
        Write-Warning "Couldn't open the dashboard: $($_.Exception.Message)"
    }
}

# Hold until the intro has had its full run.
$elapsed = ((Get-Date) - $started).TotalSeconds
$remaining = $intro - $elapsed
if ($remaining -gt 0) {
    Write-Host ("Music for another {0:N0}s..." -f $remaining) -ForegroundColor DarkGray
    Start-Sleep -Seconds $remaining
}

# Duck, speak, and always bring the volume back - even if speech throws.
$playVol = if ($cider) { [double]$cider.play_volume } else { 0.6 }
$duckVol = if ($cider) { [double]$cider.duck_volume } else { 0.1 }
$fadeMs = if ($cider) { [int]$cider.fade_ms } else { 800 }
# After the briefing the music comes back up louder than the intro - the point
# is to wake you up, not to settle back into background listening.
$outroVol = if ($cider -and $null -ne $cider.outro_volume) { [double]$cider.outro_volume } else { $playVol }

try {
    if ($musicOn) { Fade-Volume $playVol $duckVol $fadeMs }

    if (-not $NoSpeak -and $spokenFresh -and (Test-Path $spokenFile)) {
        $voice = if ($speech) { [string]$speech.voice } else { '' }
        $onlineVoice = if ($speech) { [string]$speech.online_voice } else { '' }
        $rate = if ($speech -and $null -ne $speech.rate) { [int]$speech.rate } else { 0 }
        $forceOffline = ($speech -and $speech.engine -and $speech.engine -ne 'online')
        if (-not $speech -or $speech.enabled) {
            & (Join-Path $here 'speak.ps1') -File $spokenFile -Voice $voice `
                -OnlineVoice $onlineVoice -Rate $rate -Offline:$forceOffline
        }
    }
} finally {
    # Runs even if speech failed - never leave the music stuck at duck level.
    if ($musicOn) { Fade-Volume $duckVol $outroVol $fadeMs }
}

# Leave a breadcrumb. The routine deliberately exits 0 even when a part failed
# (it handled it and said so out loud), which means Task Scheduler can't be the
# place you find out something broke - this log is.
$outcome = if ($briefingOk) { 'ok' } else { 'briefing failed - spoke the fallback message' }
$logLine = '{0}  {1}  music={2}  spoke={3}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm'),
           $outcome, $(if ($musicOn) { 'yes' } else { 'no' }), $(if ($spokenFresh -and -not $NoSpeak) { 'yes' } else { 'no' })
try {
    $logPath = Join-Path $here 'run-log.txt'
    Add-Content -Path $logPath -Value $logLine -Encoding utf8
    # Keep the last 60 mornings, not a log that grows forever.
    $lines = @(Get-Content $logPath -Encoding utf8)
    if ($lines.Count -gt 60) { Set-Content $logPath -Value $lines[-60..-1] -Encoding utf8 }
} catch { }

if ($HoldSeconds -gt 0) {
    Start-Sleep -Seconds $HoldSeconds
} elseif ($Host.Name -eq 'ConsoleHost') {
    Write-Host '  Press any key to close.' -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { Start-Sleep -Seconds 20 }
}

# Always report success to Task Scheduler: a handled failure is not a crash,
# and a task that "fails" every bad-network morning trains you to ignore it.
exit 0
