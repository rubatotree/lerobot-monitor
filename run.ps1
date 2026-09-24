$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$localConfig = Join-Path $projectRoot "config.yaml"
$exampleConfig = Join-Path $projectRoot "config.example.yaml"
$configPath = if (Test-Path -LiteralPath $localConfig) { $localConfig } else { $exampleConfig }

# Use an explicit hardware/CUDA environment when supplied. The sibling LeRobot
# checkout remains convenient for development but is not required to start.
$pythonPath = $env:LEROBOT_MONITOR_PYTHON
$siblingSource = Join-Path $projectRoot "..\lerobot\src"
$siblingPython = Join-Path $projectRoot "..\.venv\Scripts\python.exe"
if (-not $pythonPath -and (Test-Path -LiteralPath $siblingSource) -and (Test-Path -LiteralPath $siblingPython)) {
    $pythonPath = $siblingPython
}

if ($pythonPath) {
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "LEROBOT_MONITOR_PYTHON does not exist: $pythonPath"
    }
    uv pip install -e $projectRoot --python $pythonPath | Out-Null
    if (-not $env:LEROBOT_SRC -and (Test-Path -LiteralPath $siblingSource)) {
        $env:LEROBOT_SRC = (Resolve-Path -LiteralPath $siblingSource).Path
    }
    & $pythonPath -m lerobot_monitor --config $configPath @args
    exit $LASTEXITCODE
}

uv run --project $projectRoot lerobot-monitor --config $configPath @args
exit $LASTEXITCODE