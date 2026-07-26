# Runs the test suite. Nothing here touches the network, Cider, Ollama, or
# your real calendar/library files, so it's safe to run at any time.
#
#   .\run-tests.ps1                 all tests, one line per result
#   .\run-tests.ps1 -Detailed       name every test as it runs
#   .\run-tests.ps1 -Only stocks    just tests\test_stocks.py
#
# -Detailed rather than -Verbose: PowerShell reserves that name.

param(
    [switch]$Detailed,
    [string]$Only = ''
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Warning 'python not found on PATH.'
    exit 1
}

# -t . puts the project root on sys.path so the tests can import briefing,
# stocks, and the rest the same way the app does.
# Not $args - that's an automatic variable in PowerShell.
$argv = @('-m', 'unittest')
if ($Only) {
    $name = $Only -replace '^(tests[\\/])?(test_)?', '' -replace '\.py$', ''
    $argv += "tests.test_$name"
} else {
    $argv += @('discover', '-s', 'tests', '-t', '.')
}
if ($Detailed) { $argv += '-v' }

Push-Location $here
try {
    & $python @argv
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -eq 0) {
    Write-Host '  All good.' -ForegroundColor Green
} else {
    Write-Host '  Something is broken - see above.' -ForegroundColor Red
}
exit $code
