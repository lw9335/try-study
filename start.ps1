param([switch]$Check)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & py -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Please install Python 3.11+ and retry.' }
    & $pythonPath -m pip install -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if ($Check) {
    & $pythonPath -m ruff check gateway tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonPath -m pytest -q
} else {
    & $pythonPath -m gateway.main
}
