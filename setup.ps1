# Morning Brief - one-time setup.
#
#   Right-click this file and choose "Run with PowerShell", or:
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Checks what's needed, installs the one optional package, writes a starter
# config if there isn't one, registers the morning alarm, and opens settings.
# Safe to run again - it never overwrites an existing config.

param(
    [string]$Time = '06:30',
    [switch]$SkipSchedule
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$issues = @()

function Say($text, $colour = 'Gray') { Write-Host $text -ForegroundColor $colour }
function Good($text) { Write-Host "  [ok]   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [note] $text" -ForegroundColor Yellow }
function Bad($text)  { Write-Host "  [!]    $text" -ForegroundColor Red }

Say ''
Say '  Morning Brief - setup' Cyan
Say '  ---------------------' Cyan
Say ''

# ---------------------------------------------------------------- Python
Say 'Checking Python...'
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Bad 'Python is not on your PATH.'
    Say '         Install it from https://python.org/downloads (tick "Add python.exe to PATH"),'
    Say '         then run this script again.'
    $issues += 'python'
} else {
    $version = (& $python --version 2>&1) -replace 'Python\s*', ''
    $major, $minor = ($version -split '\.')[0, 1]
    if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 9)) {
        Bad "Python $version is too old - 3.9 or newer is needed."
        $issues += 'python-version'
    } else {
        Good "Python $version"
    }
}

# ---------------------------------------------------------------- Cider
Say ''
Say 'Checking Cider (required for the music)...'
$ciderInstalled = $null -ne (Get-AppxPackage -Name 'CiderCollective.Cider' -ErrorAction SilentlyContinue)
if (-not $ciderInstalled) {
    Bad 'Cider is not installed.'
    Say '         Morning Brief uses Cider to play your Apple Music playlist.'
    Say '         Get it from https://cider.sh - then run this script again.'
    $issues += 'cider'
} else {
    Good 'Cider is installed'
    if (Get-NetTCPConnection -LocalPort 10767 -State Listen -ErrorAction SilentlyContinue) {
        Good 'Cider''s external API is switched on'
    } else {
        Warn 'Cider is installed but its API is not answering.'
        Say '         Open Cider > Settings > Connectivity and turn on the External API,'
        Say '         then copy the token into the settings page when it opens.'
    }
}

# ---------------------------------------------------------------- packages
if ($python) {
    Say ''
    Say 'Checking Python packages...'
    foreach ($pkg in @(@{ import = 'edge_tts'; install = 'edge-tts'; why = 'natural voices' },
                       @{ import = 'pypdf';    install = 'pypdf';    why = 'reading PDFs aloud' })) {
        & $python -c "import $($pkg.import)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Good "$($pkg.install) - $($pkg.why)"
        } else {
            Say "  ...installing $($pkg.install) ($($pkg.why))"
            & $python -m pip install --quiet --disable-pip-version-check $pkg.install 2>&1 | Out-Null
            & $python -c "import $($pkg.import)" 2>$null
            if ($LASTEXITCODE -eq 0) { Good "$($pkg.install) installed" }
            else { Warn "$($pkg.install) would not install - the app still runs, without $($pkg.why)." }
        }
    }
}

# ---------------------------------------------------------------- config
Say ''
Say 'Checking your settings file...'
$configPath = Join-Path $here 'config.json'
if (Test-Path $configPath) {
    Good 'config.json already exists (left untouched)'
} else {
    $starter = [ordered]@{
        name = $env:USERNAME
        city = ''
        country = 'ZA'
        timezone = 'Africa/Johannesburg'
        latitude = $null
        longitude = $null
        units = 'metric'
        news_limit_per_feed = 4
        total_headlines = 12
        speech = [ordered]@{ enabled = $true; engine = 'online'
                             online_voice = 'en-ZA-LeahNeural'; voice = 'Mark'
                             rate = 0; headlines = 5 }
        cider  = [ordered]@{ enabled = $true; token = ''; playlist = ''
                             intro_seconds = 180; play_volume = 0.6; duck_volume = 0.1
                             outro_volume = 1; system_volume = 0.3; fade_ms = 800 }
        verse  = [ordered]@{ enabled = $true; faith = 'christianity' }
        stocks = [ordered]@{ enabled = $true; spoken = 3; symbols = @('^GSPC', 'AAPL') }
        calendar_opts = [ordered]@{ days_ahead = 7; max_events = 3 }
        llm    = [ordered]@{ enabled = $false; personality = '' }
        feeds  = @(
            @{ name = 'News24';         url = 'https://feeds.capi24.com/v1/Search/articles/news24/TopStories/rss' },
            @{ name = 'Daily Maverick'; url = 'https://www.dailymaverick.co.za/dmrss/' },
            @{ name = 'BBC World';      url = 'https://feeds.bbci.co.uk/news/world/rss.xml' }
        )
    }
    $starter | ConvertTo-Json -Depth 6 | Set-Content $configPath -Encoding utf8
    Good 'wrote a starter config.json - set your town and playlist on the settings page'
}

# ---------------------------------------------------------------- schedule
if (-not $SkipSchedule -and $python -and $issues.Count -eq 0) {
    Say ''
    Say "Setting the morning alarm for $Time..."
    try {
        & (Join-Path $here 'install-task.ps1') -Time $Time | Out-Null
        Good "scheduled daily at $Time"
    } catch {
        Warn "could not register the scheduled task: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- finish
Say ''
if ($issues.Count -gt 0) {
    Say '  Setup stopped - fix the items marked [!] above and run this again.' Yellow
    Say ''
    exit 1
}

Say '  Done.' Green
Say ''
Say '  Opening the settings page. Fill in:'
Say '    - your town (for the weather)'
Say '    - your Cider API token and playlist name'
Say '    - anything else you fancy: voice, verse, tickers'
Say ''
Say '  After that, the dashboard is the "Dashboard" link in the top corner.'
Say '  To install it as an app, open it in Edge or Chrome and use'
Say '  the install icon in the address bar.'
Say ''

& (Join-Path $here 'settings.ps1')
