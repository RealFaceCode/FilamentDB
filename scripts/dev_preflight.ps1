param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    Copy-Item (Join-Path $repoRoot ".env.example") (Join-Path $repoRoot ".env")
    Write-Host "[info] .env created from .env.example"
}

$envPath = Join-Path $repoRoot ".env"
$databaseUrl = ""
foreach ($line in (Get-Content $envPath)) {
    if ($line -match '^\s*DATABASE_URL\s*=\s*(.+)\s*$') {
        $databaseUrl = $Matches[1].Trim()
        break
    }
}

if (-not $databaseUrl) {
    throw "DATABASE_URL missing in .env"
}

if ($databaseUrl -notmatch '^postgresql') {
    throw "DATABASE_URL must use PostgreSQL in Docker-only mode."
}

if ($databaseUrl -notmatch '@db:') {
    throw "DATABASE_URL must point to Compose PostgreSQL service 'db'."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not available. Install/start Docker Desktop and retry."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker daemon is not reachable. Start Docker Desktop and wait until 'Engine running' is shown, then retry."
}

Write-Host "[step] Starting Docker stack (web + postgres + slot-poller)..."
docker compose --profile slot-poller up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "docker compose failed (exit code: $LASTEXITCODE)"
}

Write-Host "[step] Waiting for PostgreSQL health..."
$maxRetries = 30
$healthy = $false
for ($i = 1; $i -le $maxRetries; $i++) {
    $status = docker inspect --format='{{json .State.Health.Status}}' filament-db-postgres 2>$null
    if ($status -eq '"healthy"') {
        $healthy = $true
        break
    }
    Start-Sleep -Seconds 2
}

if (-not $healthy) {
    throw "PostgreSQL container did not become healthy in time."
}

Write-Host "[step] Checking PostgreSQL ID sequence drift..."
docker compose exec -T web python scripts/check_postgres_sequences.py
if ($LASTEXITCODE -ne 0) {
    throw "Sequence drift check failed (exit code: $LASTEXITCODE)"
}

if (-not $SkipTests) {
    Write-Host "[step] Running regression tests..."
    docker compose exec -e ENABLE_BASIC_AUTH=0 -e CSRF_PROTECT=1 -e STRICT_CSRF_CHECK=0 -e ALLOWED_HOSTS=localhost,127.0.0.1,testserver web python -m unittest tests/test_labels_custom_layout.py tests/test_usage_undo_capacity.py tests/test_api_auto_usage.py -v
    if ($LASTEXITCODE -ne 0) {
        throw "Regression tests failed (exit code: $LASTEXITCODE)"
    }
}

Write-Host "[ok] Preflight completed successfully."
