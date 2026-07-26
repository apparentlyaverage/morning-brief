# Builds the zip you send to someone else.
#
#   .\make-release.ps1                 -> morning-brief.zip next to the repo
#   .\make-release.ps1 -To D:\share    -> somewhere else
#
# It exports from git rather than zipping the working folder, so anything
# untracked or git-ignored - your config, timetable, tasks, diary, uploaded
# documents, caches, run log - cannot be included by accident. That is the
# whole point: the working folder is full of your personal data.

param(
    [string]$To = '',
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $here
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'git is needed to build a release (it is what guarantees no personal files get in).'
    }

    $dirty = git status --porcelain
    if ($dirty -and -not $AllowDirty) {
        Write-Warning 'You have uncommitted changes. They will NOT be in the zip:'
        $dirty | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Write-Host '  Commit them first, or pass -AllowDirty to build anyway.' -ForegroundColor Yellow
        exit 1
    }

    $target = if ($To) { $To } else { Split-Path -Parent $here }
    if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
    $zip = Join-Path $target 'morning-brief.zip'

    # git archive gives exactly the tracked files at HEAD - nothing else.
    $staging = Join-Path $env:TEMP ('mb-release-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        $inner = Join-Path $staging 'morning-brief'
        New-Item -ItemType Directory -Path $inner | Out-Null
        $tar = Join-Path $staging 'export.tar'
        git archive --format=tar -o $tar HEAD
        tar -xf $tar -C $inner
        Remove-Item $tar -Force

        # Sanity check: personal files must not be in there.
        $leaks = Get-ChildItem -Path $inner -Recurse -File | Where-Object {
            $_.Name -in @('config.json', 'timetable.json', 'todos.json', 'events.json',
                          'calendar.json', 'briefing.txt', 'briefing-spoken.txt',
                          'briefing_data.json', 'run-log.txt', 'stock_cache.json',
                          'verse_cache.json') -or $_.FullName -match '\\library\\'
        }
        if ($leaks) {
            $leaks | ForEach-Object { Write-Host "  LEAK: $($_.Name)" -ForegroundColor Red }
            throw 'Personal files reached the staging folder - not building a zip.'
        }

        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path $inner -DestinationPath $zip -CompressionLevel Optimal

        $size = (Get-Item $zip).Length / 1MB
        $count = (Get-ChildItem $inner -Recurse -File).Count
        Write-Host ''
        Write-Host ('  Built {0}' -f $zip) -ForegroundColor Green
        Write-Host ('  {0} files, {1:N1} MB, from commit {2}' -f $count, $size, (git rev-parse --short HEAD)) -ForegroundColor DarkGray
        Write-Host ''
        Write-Host '  Tell them: unzip it, then double-click "START HERE.cmd".' -ForegroundColor Cyan
        Write-Host ''
    } finally {
        Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
} finally {
    Pop-Location
}
