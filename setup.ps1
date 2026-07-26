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
$examplePath = Join-Path $here 'config.example.json'
if (Test-Path $configPath) {
    Good 'config.json already exists (left untouched)'
} elseif (Test-Path $examplePath) {
    # Copy the example rather than building a second starter here. It is
    # already sanitised (no name, town, token or playlist) and it carries the
    # things a hand-written starter kept losing: all eight verified feeds and
    # the presenter personality.
    $cfg = Get-Content $examplePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg.name = $env:USERNAME
    # No BOM: PowerShell 5.1's -Encoding utf8 writes one, and a BOM in front of
    # a JSON document is exactly what broke the first run before.
    [System.IO.File]::WriteAllText(
        $configPath,
        ($cfg | ConvertTo-Json -Depth 8),
        (New-Object System.Text.UTF8Encoding $false))
    Good 'wrote config.json from the example - set your town and playlist on the settings page'
} else {
    Bad 'config.example.json is missing, so there is nothing to start from.'
    $issues += 'config-example'
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
