# Starts every stage worker in its own titled window, plus video_analysis.
# Run from the repo root. Each worker gets a distinct --metrics-port since
# they share the host (see README's "Getting started").
#
# Close a window (or Ctrl+C in it) to stop that one worker; the rest keep
# running. This script only launches them -- it doesn't manage them.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$workers = @(
    "video_analysis",
    "audio", "burn_subtitles", "color", "crop",
    "detect_filler_words", "detect_scenes", "flip", "merge",
    "pad", "remove_segment", "render", "resize", "rotate",
    "transcribe", "trim"
)

$port = 9100
foreach ($w in $workers) {
    $title = "worker: $w"
    $cmd = "`$Host.UI.RawUI.WindowTitle = '$title'; uv run python -m backend.workers.cli $w --worker $w --metrics-port $port"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    $port++
    Start-Sleep -Milliseconds 300
}

Write-Host "Launched $($workers.Count) worker windows (metrics ports 9100-$($port-1))."
