# Reads text aloud through the Windows speech engine.
#
#   .\speak.ps1 -List                       show every usable voice
#   .\speak.ps1 -File briefing-spoken.txt   read that file
#   .\speak.ps1 -Text "hello" -Voice Ava    read a string in a chosen voice
#
# -Voice matches on a substring, so "Ava" finds "Microsoft Ava (Natural)".
#
# Windows splits its voices across two engines and they do NOT see each other:
#
#   modern (Windows.Media.SpeechSynthesis) - the OneCore voices, which is where
#          every "Natural" voice you install from Settings ends up.
#   legacy (System.Speech / SAPI)          - only ever sees the old "* Desktop"
#          voices. On this machine that is just David and Zira.
#
# PowerShell 5.1 cannot call the modern engine directly (its async calls come
# back as unprojected COM objects), so TtsHelper.dll does that part. Rebuild it
# from TtsHelper.cs only if you ever need to - see build-ttshelper.ps1.

param(
    [string]$File,
    [string]$Text,
    [string]$Voice = '',        # offline voice name, e.g. Mark
    [string]$OnlineVoice = '',  # neural voice, e.g. en-ZA-LeahNeural
    [int]$Rate = 0,             # -10 (slow) .. 10 (fast)
    [switch]$Offline,           # skip the online engine entirely
    [switch]$List
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$helperDll = Join-Path $here 'TtsHelper.dll'

function Import-Helper {
    if (-not (Test-Path $helperDll)) { return $false }
    try { Add-Type -Path $helperDll -ErrorAction Stop; return $true } catch { return $false }
}

function Get-ModernVoices {
    if (-not (Import-Helper)) { return @() }
    try { return @([TtsHelper]::Voices()) } catch { return @() }
}

function Get-LegacyVoices {
    try {
        Add-Type -AssemblyName System.Speech
        $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
        try { return @($s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }) }
        finally { $s.Dispose() }
    } catch { return @() }
}

if ($List) {
    $modern = Get-ModernVoices
    $legacy = Get-LegacyVoices

    if ($modern.Count) {
        Write-Host 'Voices (modern engine - preferred):' -ForegroundColor Cyan
        $modern | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Warning 'Modern engine unavailable - falling back to the legacy voices below.'
    }
    if ($legacy.Count) {
        Write-Host ''
        Write-Host 'Voices (legacy engine - fallback only):' -ForegroundColor DarkCyan
        $legacy | ForEach-Object { Write-Host "  $_" }
    }
    if (-not $modern.Count -and -not $legacy.Count) { Write-Warning 'No speech voices found.' }

    Write-Host ''
    Write-Host 'Add more: Settings > Accessibility > Narrator > Add natural voices' -ForegroundColor DarkGray
    Write-Host 'Then put the name in config.json under speech.voice' -ForegroundColor DarkGray
    return
}

if ($File) {
    if (-not (Test-Path $File)) { throw "No such file: $File" }
    $Text = Get-Content $File -Raw -Encoding UTF8
}
if (-not $Text) { throw 'Give me -File or -Text.' }

$Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))

function Speak-Modern {
    $voices = Get-ModernVoices
    if (-not $voices.Count) { return $false }
    if ($Voice -and -not ($voices | Where-Object { $_ -like "*$Voice*" })) {
        return $false          # not here - let the legacy engine try the name
    }

    # The modern engine wants a rate multiplier (1.0 = normal), not -10..10.
    $speakingRate = if ($Rate -ge 0) { 1.0 + ($Rate * 0.1) } else { 1.0 + ($Rate * 0.05) }
    $wav = Join-Path $env:TEMP ("briefing-speech-{0}.wav" -f $PID)
    try {
        [TtsHelper]::ToWav($Text, $Voice, $speakingRate, $wav)
        $player = New-Object System.Media.SoundPlayer $wav
        try { $player.PlaySync() } finally { $player.Dispose() }
        return $true
    } finally {
        if (Test-Path $wav) { Remove-Item $wav -Force -ErrorAction SilentlyContinue }
    }
}

function Speak-Legacy {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $synth.Rate = $Rate
        if ($Voice) {
            $match = @(Get-LegacyVoices) | Where-Object { $_ -like "*$Voice*" } | Select-Object -First 1
            if ($match) {
                $synth.SelectVoice($match)
            } else {
                Write-Warning "Voice '$Voice' not found on either engine - using the default. Run: .\speak.ps1 -List"
            }
        }
        $synth.Speak($Text)
    } finally { $synth.Dispose() }
}

function Speak-Online {
    if ($Offline -or -not $OnlineVoice) { return $false }
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    $script = Join-Path $here 'say_online.py'
    if (-not $py -or -not (Test-Path $script)) { return $false }

    $mp3 = Join-Path $env:TEMP ("briefing-online-{0}.mp3" -f $PID)
    $errFile = Join-Path $env:TEMP ("briefing-online-{0}.err" -f $PID)
    $tmpText = $null
    try {
        # edge-tts wants a signed percentage; -10..10 maps to -50%..+50%.
        $pct = '{0}{1}%' -f $(if ($Rate -ge 0) { '+' } else { '-' }), [Math]::Abs($Rate * 5)

        # Always hand the text over as a file. Passing it as an argument breaks
        # on headlines containing quotes, ampersands or newlines.
        $srcFile = $File
        if (-not $srcFile) {
            $tmpText = Join-Path $env:TEMP ("briefing-online-{0}.txt" -f $PID)
            [System.IO.File]::WriteAllText($tmpText, $Text, (New-Object System.Text.UTF8Encoding $true))
            $srcFile = $tmpText
        }

        # Start-Process rather than the call operator: a native command writing
        # to stderr under ErrorActionPreference='Stop' raises NativeCommandError,
        # which would turn an ordinary "service unreachable" into a thrown error.
        $argList = @(
            "`"$script`"", '--voice', $OnlineVoice, '--out', "`"$mp3`"",
            '--rate', $pct, '--file', "`"$srcFile`""
        )
        $proc = Start-Process -FilePath $py -ArgumentList $argList -NoNewWindow -Wait -PassThru `
            -RedirectStandardError $errFile
        if ($proc.ExitCode -ne 0 -or -not (Test-Path $mp3) -or (Get-Item $mp3).Length -eq 0) { return $false }

        Add-Type -AssemblyName presentationCore
        $player = New-Object System.Windows.Media.MediaPlayer
        try {
            $player.Open([uri]$mp3)
            # Open() is async - wait for the duration metadata before playing.
            $deadline = (Get-Date).AddSeconds(10)
            while (-not $player.NaturalDuration.HasTimeSpan -and (Get-Date) -lt $deadline) {
                Start-Sleep -Milliseconds 100
            }
            if (-not $player.NaturalDuration.HasTimeSpan) { return $false }
            $secs = $player.NaturalDuration.TimeSpan.TotalSeconds
            $player.Play()
            Start-Sleep -Milliseconds ([int](($secs + 0.6) * 1000))
            return $true
        } finally { $player.Stop(); $player.Close() }
    } finally {
        foreach ($f in @($mp3, $errFile, $tmpText)) {
            if ($f -and (Test-Path $f)) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
        }
    }
}

# Best voice first, degrade quietly: online neural -> offline OneCore -> SAPI.
# The briefing must never go silent just because the network is down.
$spoken = $false
$onlineFailure = $null
if ($OnlineVoice -and -not $Offline) {
    try { $spoken = Speak-Online }
    catch { $onlineFailure = $_.Exception.Message.Split("`n")[0] }
    if (-not $spoken) {
        $why = if ($onlineFailure) { " ($onlineFailure)" } else { '' }
        Write-Warning "Online voice '$OnlineVoice' unavailable$why - falling back to '$Voice'."
    }
}
if (-not $spoken) {
    try { $spoken = Speak-Modern } catch { Write-Warning "Modern speech engine failed: $($_.Exception.Message)" }
}
if (-not $spoken) { Speak-Legacy }

exit 0
