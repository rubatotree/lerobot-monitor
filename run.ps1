# Prefer the workspace CUDA venv (d:\repos\lerobot\.venv), then the package venv.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
    (Join-Path $here "..\.venv\Scripts\python.exe"),
    (Join-Path $here "..\lerobot\.venv\Scripts\python.exe")
)
$lerobotPython = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$config = Join-Path $here "config.yaml"

if (-not $lerobotPython) {
    Write-Error "No lerobot venv found. Expected ..\.venv or ..\lerobot\.venv. Or: uv run lerobot-monitor"
}

uv pip install -e $here --python $lerobotPython | Out-Null
$env:LEROBOT_SRC = (Resolve-Path (Join-Path $here "..\lerobot\src")).Path
Write-Host "[run.ps1] python: $lerobotPython"
& $lerobotPython -m lerobot_monitor @args --config $config
