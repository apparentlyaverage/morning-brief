# Talks to the Cider music player's local API (http://localhost:10767).
#
#   .\cider.ps1 -Discover              probe which endpoints this Cider build exposes
#   .\cider.ps1 -ListPlaylists         show your library playlists
#   .\cider.ps1 -Play "background chilling"
#   .\cider.ps1 -Volume 0.2            0.0 .. 1.0
#   .\cider.ps1 -Launch                start Cider if it isn't running
#
# Needs an API token: Cider > Settings > Connectivity > External API.
# Put it in config.json under cider.token - this script reads it from there.

param(
    [switch]$Discover,
    [switch]$ListPlaylists,
    [string]$Play,
    [double]$Volume = -1,
    [switch]$Launch,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# 127.0.0.1 rather than localhost: localhost resolves to ::1 first and Cider
# listens on IPv4 only, which costs ~2s per request failing over.
$BASE = 'http://127.0.0.1:10767'
$APPID = 'CiderCollective.Cider_a6qxe093bx5xj!App'

$cfg = (Get-Content (Join-Path $here 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json).cider
$TOKEN = if ($cfg) { [string]$cfg.token } else { '' }

function Test-CiderUp {
    try { $null = Test-NetConnection -ComputerName 127.0.0.1 -Port 10767 -WarningAction SilentlyContinue -InformationLevel Quiet -ErrorAction Stop } catch {}
    return (Get-NetTCPConnection -LocalPort 10767 -State Listen -ErrorAction SilentlyContinue) -ne $null
}

function Start-Cider {
    if (Get-Process -Name 'Cider' -ErrorAction SilentlyContinue) {
        if (-not $Quiet) { Write-Host 'Cider already running.' -ForegroundColor DarkGray }
    } else {
        if (-not $Quiet) { Write-Host 'Starting Cider...' -ForegroundColor Cyan }
        Start-Process "shell:AppsFolder\$APPID"
    }
    # The API comes up a few seconds after the window does.
    $deadline = (Get-Date).AddSeconds(45)
    while (-not (Test-CiderUp)) {
        if ((Get-Date) -gt $deadline) { throw 'Cider did not open its API on port 10767 within 45s.' }
        Start-Sleep -Milliseconds 500
    }
    if (-not $Quiet) { Write-Host 'Cider API is up.' -ForegroundColor Green }
}

function Invoke-Cider {
    param([string]$Method = 'GET', [Parameter(Mandatory)][string]$Path, $Body)
    if (-not $TOKEN) { throw 'No Cider API token in config.json (cider.token).' }
    $p = @{
        Uri             = "$BASE$Path"
        Method          = $Method
        Headers         = @{ 'apptoken' = $TOKEN }
        TimeoutSec      = 10
        UseBasicParsing = $true
    }
    # Cider rejects a POST with no content type (415), even when it needs no
    # body - so every POST gets at least an empty JSON object.
    if ($Method -ne 'GET') {
        $p.Body = if ($null -ne $Body) { $Body | ConvertTo-Json -Depth 6 -Compress } else { '{}' }
        $p.ContentType = 'application/json'
    }
    Invoke-RestMethod @p
}

function Get-AllPlaylists {
    # A page is capped at 100 - follow the cursor or a large library gets cut off.
    $items = @()
    $offset = 0
    while ($offset -lt 5000) {
        $page = Invoke-Cider -Method POST -Path '/api/v1/amapi/run-v3' `
            -Body @{ path = "/v1/me/library/playlists?limit=100&offset=$offset" }
        $batch = $page.data.data
        if (-not $batch) { break }
        $items += $batch
        if (-not $page.data.next) { break }
        $offset += 100
    }
    return $items
}

# ---------------------------------------------------------------- discovery
# Endpoint names have moved between Cider versions, so rather than guessing at
# runtime we probe once and report what this build actually answers.
if ($Discover) {
    if (-not (Test-CiderUp)) { throw 'Cider API is not listening on port 10767. Start Cider first.' }
    if (-not $TOKEN) { Write-Warning 'No token in config.json - everything will come back 403.' }

    $candidates = @(
        @{ M = 'GET';  P = '/api/v1/playback/now-playing' }
        @{ M = 'GET';  P = '/api/v1/playback/is-playing' }
        @{ M = 'GET';  P = '/api/v1/playback/volume' }
        @{ M = 'GET';  P = '/api/v1/playback/queue' }
        @{ M = 'POST'; P = '/api/v1/amapi/run-v3'; B = @{ path = '/v1/me/library/playlists' } }
    )

    foreach ($c in $candidates) {
        try {
            $r = Invoke-Cider -Method $c.M -Path $c.P -Body $c.B
            $s = ($r | ConvertTo-Json -Depth 3 -Compress)
            if ($s.Length -gt 200) { $s = $s.Substring(0, 200) + ' ...' }
            Write-Host ("OK    {0,-5} {1}" -f $c.M, $c.P) -ForegroundColor Green
            Write-Host "      $s" -ForegroundColor DarkGray
        } catch {
            $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { '?' }
            Write-Host ("{0,-5} {1,-5} {2}" -f $code, $c.M, $c.P) -ForegroundColor DarkYellow
        }
    }
    return
}

# ---------------------------------------------------------------- actions
if ($Launch) { Start-Cider; return }

if ($ListPlaylists) {
    if (-not (Test-CiderUp)) { Start-Cider }
    $items = Get-AllPlaylists
    if (-not $items) { Write-Warning 'No playlists came back - run -Discover to check the endpoint.'; return }
    $items | ForEach-Object {
        [pscustomobject]@{ Name = $_.attributes.name; Id = $_.id }
    } | Format-Table -AutoSize
    return
}

if ($Play) {
    if (-not (Test-CiderUp)) { Start-Cider }
    $items = Get-AllPlaylists
    $match = $items | Where-Object { $_.attributes.name -and $_.attributes.name.Trim() -ieq $Play.Trim() } | Select-Object -First 1
    if (-not $match) {
        $match = $items | Where-Object { $_.attributes.name -like "*$Play*" } | Select-Object -First 1
    }
    if (-not $match) {
        Write-Warning "No playlist matching '$Play'. Your playlists:"
        $items | ForEach-Object { Write-Host "  $($_.attributes.name)" }
        return
    }
    if (-not $Quiet) { Write-Host "Playing: $($match.attributes.name)" -ForegroundColor Cyan }
    Invoke-Cider -Method POST -Path '/api/v1/playback/play-item-href' -Body @{ href = "/v1/me/library/playlists/$($match.id)" } | Out-Null
    return
}

if ($Volume -ge 0) {
    $v = [Math]::Max(0.0, [Math]::Min(1.0, $Volume))
    Invoke-Cider -Method POST -Path '/api/v1/playback/volume' -Body @{ volume = $v } | Out-Null
    if (-not $Quiet) { Write-Host "Volume -> $v" -ForegroundColor DarkGray }
    return
}

Write-Host 'Nothing to do. Try -Discover, -ListPlaylists, -Play <name>, -Volume <0..1>, or -Launch.'
