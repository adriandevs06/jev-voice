# tts.ps1 - the single speech path. ElevenLabs when a key is available, Windows SAPI otherwise.
#
#   .\tts.ps1 "something to say"
#   .\tts.ps1 "something to say" -Force sapi      # skip ElevenLabs
#
# Config, all optional, read from user env vars:
#   ELEVENLABS_API_KEY    enables the good voice
#   ELEVENLABS_VOICE_ID   which voice; if unset, the first voice on the account is
#                         resolved once and cached in .voice_id
#   ELEVENLABS_MODEL      default eleven_flash_v2_5 (fastest + cheapest per char)
#
# Raises a .speaking sentinel for the whole duration so listen.py mutes the mic and
# the wake word can't trigger on Claude's own voice.

param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Text,
    [ValidateSet('auto', 'sapi', 'eleven')][string]$Force = 'auto'
)

$ErrorActionPreference = 'SilentlyContinue'
$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$lock      = Join-Path $here '.speaking'
$voiceFile = Join-Path $here '.voice_id'

function Get-Env($name) {
    $v = [Environment]::GetEnvironmentVariable($name, 'User')
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($name, 'Machine') }
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($name, 'Process') }
    return $v
}

function Speak-Sapi($t) {
    Add-Type -AssemblyName System.Speech
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try { $s.SelectVoice('Microsoft Zira Desktop') } catch { }
    $s.Rate = 1
    $s.Speak($t)
    $s.Dispose()
}

function Resolve-VoiceId($key) {
    $v = Get-Env 'ELEVENLABS_VOICE_ID'
    if ($v) { return $v }
    if (Test-Path $voiceFile) {
        $cached = (Get-Content $voiceFile -Raw).Trim()
        if ($cached) { return $cached }
    }
    $r = Invoke-RestMethod -Uri 'https://api.elevenlabs.io/v2/voices?page_size=1' `
                           -Headers @{ 'xi-api-key' = $key } -Method Get -TimeoutSec 15
    $id = $r.voices[0].voice_id
    if ($id) { Set-Content -Path $voiceFile -Value $id -Encoding ascii }
    return $id
}

function Speak-Eleven($t, $key) {
    $voiceId = Resolve-VoiceId $key
    if (-not $voiceId) { throw 'no voice id' }

    $model = Get-Env 'ELEVENLABS_MODEL'
    if (-not $model) { $model = 'eleven_flash_v2_5' }

    $mp3 = Join-Path $env:TEMP ("claude_tts_" + [guid]::NewGuid().ToString('N') + ".mp3")
    $body = @{
        text     = $t
        model_id = $model
        voice_settings = @{ stability = 0.4; similarity_boost = 0.75; style = 0.35; use_speaker_boost = $true }
    } | ConvertTo-Json -Depth 5

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-RestMethod -Uri "https://api.elevenlabs.io/v1/text-to-speech/$voiceId`?output_format=mp3_44100_128" `
        -Method Post -TimeoutSec 45 `
        -Headers @{ 'xi-api-key' = $key } `
        -ContentType 'application/json' `
        -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
        -OutFile $mp3

    if (-not (Test-Path $mp3) -or (Get-Item $mp3).Length -lt 512) { throw 'empty audio' }

    $ffplay = Join-Path (Split-Path (Get-Command ffmpeg).Source) 'ffplay.exe'
    if (Test-Path $ffplay) {
        & $ffplay -nodisp -autoexit -loglevel quiet $mp3 | Out-Null
    } else {
        Add-Type -AssemblyName presentationCore
        $p = New-Object System.Windows.Media.MediaPlayer
        $p.Open([uri]$mp3); Start-Sleep -Milliseconds 400; $p.Play()
        while ($p.NaturalDuration.HasTimeSpan -and $p.Position -lt $p.NaturalDuration.TimeSpan) {
            Start-Sleep -Milliseconds 150
        }
        $p.Close()
    }
    Remove-Item $mp3 -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------

if (-not $Text.Trim()) { exit 0 }

try {
    New-Item -ItemType File -Path $lock -Force | Out-Null

    $key = Get-Env 'ELEVENLABS_API_KEY'
    $useEleven = ($Force -eq 'eleven') -or ($Force -eq 'auto' -and $key)

    if ($useEleven) {
        if (-not $key) { throw 'ELEVENLABS_API_KEY not set' }
        try {
            Speak-Eleven $Text $key
        } catch {
            # Never go mute because the API is down, rate limited, or out of credits.
            Write-Warning "ElevenLabs failed ($($_.Exception.Message)); falling back to SAPI."
            Speak-Sapi $Text
        }
    } else {
        Speak-Sapi $Text
    }
}
finally {
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}

exit 0
