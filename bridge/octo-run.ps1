# OctoDash — команда запуска (Windows PowerShell).
# Идемпотентно поднимает мост detached, затем запускает claude.
# Повторные вызовы безвредны: лишний инстанс упрётся в занятый порт и молча выйдет.
#
# Использование: .\octo-run.ps1 [аргументы claude...]

$ErrorActionPreference = "SilentlyContinue"

$bridgePath = $env:OCTO_BRIDGE_PATH
if (-not $bridgePath) { $bridgePath = Join-Path $PSScriptRoot "bridge.py" }

# Предпочитаем интерпретатор из .venv, иначе системный python
$python = $env:OCTO_PYTHON
if (-not $python) {
    $venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $python = $venvPy } else { $python = "python" }
}

# Detached-запуск моста, чтобы он пережил закрытие терминала.
Start-Process -FilePath $python -ArgumentList $bridgePath -WindowStyle Hidden | Out-Null

& claude @args
exit $LASTEXITCODE
