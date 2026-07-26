# Bridge to Windows' global media transport controls.
#
#   .\media.ps1 -Action list
#   .\media.ps1 -Action state [-App <appId>]
#   .\media.ps1 -Action play|pause|playpause|next|previous [-App <appId>]
#   .\media.ps1 -Action seek -Position 42.5 [-App <appId>]
#
# Always prints one line of JSON. Python calls this rather than talking to
# WinRT itself - the .NET shim in TtsHelper.dll does the actual work.

param(
    [Parameter(Mandatory)][string]$Action,
    [string]$App = '',
    [double]$Position = 0
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    Add-Type -Path (Join-Path $here 'TtsHelper.dll') -ErrorAction Stop
} catch {
    '{"ok":false,"error":"TtsHelper.dll missing or unloadable"}'
    exit 0
}

try {
    switch ($Action.ToLower()) {
        'list'  { [MediaSessions]::List() }
        'state' { [MediaSessions]::State($App) }
        'seek'  { [MediaSessions]::Control($App, "seek:$([string]::Format([cultureinfo]::InvariantCulture,'{0}',$Position))") }
        default { [MediaSessions]::Control($App, $Action.ToLower()) }
    }
} catch {
    '{"ok":false,"error":"' + ($_.Exception.Message -replace '"', "'") + '"}'
}
