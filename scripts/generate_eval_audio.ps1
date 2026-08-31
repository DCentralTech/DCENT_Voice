# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Generate 16 kHz mono 16-bit PCM fixtures with Windows SAPI. Offline. No cloud.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$outDir = Join-Path (Split-Path -Parent $PSScriptRoot) "tests\fixtures\audio\eval"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$items = @{
    "short-command.wav" = "Open settings."
    "dcentral-terms.wav" = "D-Central ships DCENT Voice for sovereign dictation."
    "developer-file.wav" = "Edit app.py and run pytest."
    "bitcoin.wav" = "Send 0.021 bitcoin to the hardware wallet."
    "numbers.wav" = "The port is 8765."
    "punctuation.wav" = "What time is the meeting?"
    "conversational.wav" = "Can you look at the previous work and draft a plan?"
    "filename.wav" = "Save it as config example toml."
    "shell.wav" = "Run git status then cargo test."
    "url-email.wav" = "Email ada at d-central.tech"
}

$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
)

foreach ($name in $items.Keys) {
    $path = Join-Path $outDir $name
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $synth.Rate = 0
        $synth.SetOutputToWaveFile($path, $format)
        $synth.Speak($items[$name])
    } finally {
        $synth.Dispose()
    }
    Write-Host "wrote $path"
}
Write-Host "SAPI eval fixtures ready in $outDir"
