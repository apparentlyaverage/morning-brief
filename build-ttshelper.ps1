# Rebuilds TtsHelper.dll from TtsHelper.cs.
#
# You should not need this - TtsHelper.dll is already built and committed next
# to it. It's here so the DLL isn't an unexplained binary, and so you can
# rebuild if the file is ever lost.
#
# Why the DLL exists at all: PowerShell 5.1 cannot call the modern Windows
# speech engine directly. Its async methods come back as raw COM objects with
# no usable properties, and Add-Type cannot consume the .winmd metadata that
# describes them. A tiny pre-compiled C# shim sidesteps both problems.
#
# Requires the Windows SDK (for the unified Windows.winmd). The DLL itself has
# no such requirement at runtime.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw "csc.exe not found at $csc" }

$facades = 'C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8\Facades'
if (-not (Test-Path $facades)) { throw "Missing .NET 4.8 reference facades: $facades" }

# Pick the newest unified Windows.winmd the SDK offers.
# Only version-numbered folders: UnionMetadata also contains a 'Facade'
# directory holding a type-forwarding stub, which sorts above the real
# versions alphabetically and produces CS1070 if picked by mistake.
$winmd = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\UnionMetadata' -Directory -ErrorAction SilentlyContinue |
         Where-Object { $_.Name -match '^\d+(\.\d+)+$' -and (Test-Path (Join-Path $_.FullName 'Windows.winmd')) } |
         Sort-Object { [version]$_.Name } -Descending |
         Select-Object -First 1
if (-not $winmd) {
    throw 'No Windows SDK UnionMetadata\Windows.winmd found. Install the Windows SDK, or keep using the existing TtsHelper.dll.'
}

$out = Join-Path $here 'TtsHelper.dll'
& $csc '/nologo' '/target:library' "/out:$out" `
    "/reference:$(Join-Path $winmd.FullName 'Windows.winmd')" `
    "/reference:$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll" `
    "/reference:$facades\System.Runtime.dll" `
    (Join-Path $here 'TtsHelper.cs') `
    (Join-Path $here 'SystemVolume.cs') `
    (Join-Path $here 'MediaSessions.cs')

if ($LASTEXITCODE -ne 0) { throw "csc failed with exit code $LASTEXITCODE" }
Write-Host "Built $out ($((Get-Item $out).Length) bytes) against $($winmd.Name)" -ForegroundColor Green
