param(
    [int]$StaleMinutes = 10
)

$ErrorActionPreference = 'Stop'

function Get-SlotPollerRunning {
    $name = (& docker compose ps --services --filter "status=running") | Where-Object { $_ -eq 'slot-poller' }
    return $null -ne $name
}

function Get-LastObservedAtUtc {
    $py = @'
from app.db import SessionLocal
from app.models import DeviceSlotState
from sqlalchemy import func

session = SessionLocal()
try:
    latest = session.query(func.max(DeviceSlotState.observed_at)).scalar()
    print(latest.isoformat() if latest else '')
finally:
    session.close()
'@
    $out = & docker compose exec -T web python -c $py
    return [string]($out | Select-Object -First 1)
}

$pollerRunning = Get-SlotPollerRunning
$latestRaw = Get-LastObservedAtUtc

if (-not $latestRaw) {
    Write-Host "slot-poller running: $pollerRunning"
    Write-Host "last slot update: none"
    Write-Host "status: NO_DATA" -ForegroundColor Yellow
    exit 2
}

$latest = [datetime]::Parse($latestRaw)
$nowUtc = [datetime]::UtcNow
$age = $nowUtc - $latest
$ageMinutes = [math]::Floor($age.TotalMinutes)

$status = if ($age.TotalMinutes -gt $StaleMinutes) { 'STALE' } else { 'FRESH' }
$color = if ($status -eq 'FRESH') { 'Green' } else { 'Yellow' }

Write-Host "slot-poller running: $pollerRunning"
Write-Host "last slot update: $($latest.ToString('u'))"
Write-Host "age minutes: $ageMinutes"
Write-Host "threshold minutes: $StaleMinutes"
Write-Host "status: $status" -ForegroundColor $color

if (-not $pollerRunning) {
    Write-Host "hint: slot-poller is not running" -ForegroundColor Yellow
    exit 3
}

if ($status -eq 'STALE') {
    exit 1
}

exit 0
