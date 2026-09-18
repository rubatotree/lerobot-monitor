# Launch with the sibling lerobot venv so SO-101 / policy imports work.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$lerobotPython = Join-Path $here "..\lerobot\.venv\Scripts\python.exe"
$config = Join-Path $here "config.yaml"

if (-not (Test-Path $lerobotPython)) {
    Write-Error "Sibling lerobot venv not found at $lerobotPython. Install lerobot first, or: uv run lerobot-monitor"
}

uv pip install -e $here --python $lerobotPython | Out-Null
$env:LEROBOT_SRC = (Resolve-Path (Join-Path $here "..\lerobot\src")).Path
& $lerobotPython -m lerobot_monitor @args --config $config
