param([switch]$Check)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker Desktop is required.' }
$taskDockerReady = $false
try {
    & docker info --format '{{.ServerVersion}}' *> $null
    $taskDockerReady = $LASTEXITCODE -eq 0
} catch { $taskDockerReady = $false }
if (-not $taskDockerReady) {
    & docker desktop start
    if ($LASTEXITCODE -ne 0) { throw 'Please start Docker Desktop and retry.' }
}
$taskEnvFile = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path -LiteralPath $taskEnvFile)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.env.example') -Destination $taskEnvFile
}
$taskEnvText = [System.IO.File]::ReadAllText($taskEnvFile)
if ($taskEnvText -notmatch '(?m)^POSTGRES_PASSWORD=(?!replace-with-local-random-password)[^\r\n]+') {
    $taskDatabasePassword = [guid]::NewGuid().ToString('N')
    if ($taskEnvText -match '(?m)^POSTGRES_PASSWORD=.*$') {
        $taskEnvText = $taskEnvText -replace '(?m)^POSTGRES_PASSWORD=.*$', "POSTGRES_PASSWORD=$taskDatabasePassword"
    } else { $taskEnvText = $taskEnvText.TrimEnd() + "`nPOSTGRES_PASSWORD=$taskDatabasePassword`n" }
    [System.IO.File]::WriteAllText($taskEnvFile, $taskEnvText, [System.Text.UTF8Encoding]::new($false))
}
& docker compose up -d --build --wait
if ($LASTEXITCODE -ne 0) { throw 'Docker startup failed. Check port 3000 and Docker logs.' }
if ($Check) {
    & docker compose --profile test run --build --rm tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Output 'Gateway: http://127.0.0.1:3000'
& docker compose ps
