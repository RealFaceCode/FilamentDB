# FilamentDB

FilamentDB is a Docker-first web application for managing 3D-print filament inventory, usage booking, and printer slot state.
It combines a FastAPI backend, server-rendered UI, PostgreSQL persistence, and optional local integrations (Slicer hooks and LAN slot bridge).

## Table of Contents

- [FilamentDB](#filamentdb)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Key Features](#key-features)
  - [Architecture](#architecture)
  - [Operating Model (Mandatory)](#operating-model-mandatory)
  - [Quickstart](#quickstart)
    - [1) Prerequisites](#1-prerequisites)
    - [2) Clone repository](#2-clone-repository)
    - [3) Create environment file](#3-create-environment-file)
    - [4) Configure minimum required values in `.env`](#4-configure-minimum-required-values-in-env)
    - [5) Start services](#5-start-services)
    - [6) Run migrations](#6-run-migrations)
    - [7) Verify health](#7-verify-health)
  - [Configuration](#configuration)
  - [Compose Profiles](#compose-profiles)
  - [Development Workflows](#development-workflows)
  - [Deployment and Operations](#deployment-and-operations)
  - [Backup and Restore](#backup-and-restore)
  - [Integrations](#integrations)
    - [Use Slot Poller](#use-slot-poller)
    - [Use Slicer Auto Booking](#use-slicer-auto-booking)
  - [Security Baseline](#security-baseline)
  - [Troubleshooting](#troubleshooting)
    - [Containers are not healthy](#containers-are-not-healthy)
    - [`/healthz` returns 503](#healthz-returns-503)
    - [Migration fails](#migration-fails)
    - [Auth / host / CSRF issues](#auth--host--csrf-issues)
    - [Slot-state data not updating](#slot-state-data-not-updating)
    - [Backup/restore issues](#backuprestore-issues)
  - [References](#references)

## Overview

FilamentDB supports end-to-end filament operations:

- Spool inventory and lifecycle handling
- Manual and automatic usage booking
- Printer slot status ingestion and expected-vs-observed comparison
- Import/export and backup/restore workflows
- UI and API endpoints for daily operations

## Key Features

- Spool management (brand, material, color, weight, remaining amount, location)
- Usage tracking (manual and file-driven auto booking)
- Slot-state ingestion via API and optional poller
- Import/export for CSV and Excel workflows
- Backup/restore UI and script-driven operational flows
- Project segmentation (`private` / `business`)

## Architecture

- `web`: FastAPI app + server-rendered UI (`app/main.py`)
- `db`: PostgreSQL 16 (`docker-compose.yml` service `db`)
- Optional Caddy reverse proxy profiles:
  - `https` (public domain)
  - `https-local` (localhost TLS)
  - `https-lan` (LAN TLS)
- Optional `slot-poller` profile for periodic slot-state ingestion

## Operating Model (Mandatory)

- Docker Compose only for runtime, migrations, tests, and operations.
- PostgreSQL only for regular operation.
- `DATABASE_URL` must use PostgreSQL and host `db` (Compose service).
- After project changes, rebuild/restart with:

```bash
docker compose up -d --build
```

## Quickstart

### 1) Prerequisites

- Docker Desktop / Docker Engine with Compose plugin
- Git

### 2) Clone repository

```bash
git clone <your-repo-url>
cd Filament_Datenbank
```

### 3) Create environment file

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

### 4) Configure minimum required values in `.env`

- `POSTGRES_PASSWORD`
- `DATABASE_URL` (must point to `@db:5432`)
- `ENABLE_BASIC_AUTH`, `BASIC_AUTH_USERNAME`, `BASIC_AUTH_PASSWORD`
- `ALLOWED_HOSTS`, `TRUSTED_ORIGINS`

### 5) Start services

```bash
docker compose up -d --build
```

### 6) Run migrations

```bash
docker compose exec web alembic upgrade head
```

### 7) Verify health

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

Expected result includes `"ok": true` and `"database": "ok"`.

## Configuration

Primary settings are in `.env` (template: `.env.example`).

Important groups:

- App runtime: `APP_ENV`, `HOST`, `PORT`, `LOG_LEVEL`
- Security: `ENABLE_BASIC_AUTH`, `CSRF_PROTECT`, `COOKIE_SECURE`, `FORCE_HTTPS_REDIRECT`
- Access control: `ALLOWED_HOSTS`, `TRUSTED_ORIGINS`
- Database: `POSTGRES_*`, `DATABASE_URL`
- Slot ingestion/poller: `SLOT_STATE_*`, `BAMBU_PRINTERS_JSON`

## Compose Profiles

Public HTTPS:

```bash
docker compose --profile https up -d --build
```

Localhost TLS:

```bash
docker compose --profile https-local up -d --build
```

LAN TLS:

```bash
docker compose --profile https-lan up -d --build
```

Slot poller:

```bash
docker compose --profile slot-poller up -d --build
```

## Development Workflows

Recommended preflight (PowerShell):

```powershell
.\scripts\dev_preflight.ps1
```

Skip tests:

```powershell
.\scripts\dev_preflight.ps1 -SkipTests
```

Targeted tests in Compose:

```bash
docker compose exec -e PYTHONPATH=/app web pytest -q tests/test_supplies_page.py
```

```bash
docker compose exec web python -m unittest tests/test_api_auto_usage.py -v
```

## Deployment and Operations

Primary runbooks:

- `deploy/GO_LIVE_CHECKLIST.md`
- `deploy/ROLLBACK_RUNBOOK.md`

Typical rollout:

```bash
git pull
docker compose pull
docker compose --profile https up -d --build
docker compose exec web alembic upgrade head
```

Runtime checks:

```bash
docker compose ps
docker compose logs --tail=100 web
docker compose logs --tail=100 db
curl -fsS http://127.0.0.1:8000/healthz
```

## Backup and Restore

Scripted backup:

```bash
REPO_DIR=/opt/filament_datenbank BACKUP_DIR=/opt/filament_backups RETENTION_DAYS=14 /opt/filament_datenbank/deploy/postgres_backup.sh
```

Restore drill:

```bash
REPO_DIR=/opt/filament_datenbank BACKUP_DIR=/opt/filament_backups /opt/filament_datenbank/deploy/postgres_restore_drill.sh
```

In-app backup routes:

- `GET /backup`
- `POST /backup/create`
- `GET /backup/download/{filename}`
- `POST /backup/restore-file`
- `POST /backup/delete-file`
- `POST /backup/auto-settings`
- `POST /backup/reset-all`
- `GET /backup/export`
- `POST /backup/import`

## Integrations

Slicer auto-usage hooks:

- Setup guide: `slicer_hooks/README.md`
- Main API endpoint: `POST /api/usage/auto-from-file`

Local slot-state bridge:

- Setup guide: `local_services/README.md`
- Main API endpoint: `POST /api/slot-state/push`

### Use Slot Poller

The slot poller runs as an optional Compose profile and updates slot-state data periodically.

1) Configure `.env` (minimum):

- `SLOT_STATE_POLL_INTERVAL_SEC=45`
- `SLOT_STATE_PROVIDER=feed` or provider required by your setup
- `SLOT_STATE_PROJECT=private` (or `business`)
- `SLOT_STATE_STALE_MINUTES=10`
- Optional Bambu direct polling: `BAMBU_PRINTERS_JSON=[{"name":"P1S-01","host":"192.168.1.50","serial":"...","access_code":"..."}]`

2) Start profile:

```bash
docker compose --profile slot-poller up -d --build
```

3) Verify logs:

```bash
docker compose logs --tail=200 slot-poller
```

4) Validate app view/API:

- Open `/slot-status` in the app.
- Optional health-level validation via app logs and DB-backed UI updates.

For local LAN ingestion from a user PC instead of server-side polling, see `local_services/README.md` (`local_slot_bridge.py` pushing to `POST /api/slot-state/push`).

### Use Slicer Auto Booking

Automatic booking sends generated print files to the API endpoint `POST /api/usage/auto-from-file`.

1) Ensure app is reachable and auth is configured:

- Server running via Compose
- If Basic Auth is enabled, provide credentials in the hook script configuration

2) Configure hook script in your slicer (Windows):

- Bambu Studio: `slicer_hooks/send_filament_usage_bambu.cmd`
- PrusaSlicer / OrcaSlicer / SuperSlicer: `slicer_hooks/send_filament_usage_prusa_orca_superslicer.cmd`
- Cura / Creality Print: `slicer_hooks/send_filament_usage_cura_creality.cmd`

3) Adjust script variables as needed in `slicer_hooks/send_filament_usage.cmd`:

- `URL` (your app endpoint)
- `PROJECT` (`private` or `business`)
- `DRYRUN` (`1` for test, `0` for real booking)
- `AUTH` (`user:password` when Basic Auth is enabled)

4) Run a test print/export and verify booking:

- Check app views `/usage` and `/booking/tracking`
- For troubleshooting, inspect hook terminal output and app logs

Full slicer-specific setup details are documented in `slicer_hooks/README.md`.

## Security Baseline

For public deployment, keep these enabled:

- `ENABLE_BASIC_AUTH=1`
- `CSRF_PROTECT=1`
- `COOKIE_SECURE=1`
- `FORCE_HTTPS_REDIRECT=1`
- `ALLOWED_HOSTS` set to valid domain(s)
- `TRUSTED_ORIGINS` set to valid HTTPS origin(s)

Replace all placeholder credentials before go-live.

## Troubleshooting

### Containers are not healthy

```bash
docker compose ps
docker compose logs --tail=200 db
docker compose logs --tail=200 web
```

Verify DB health and connectivity from `web` to PostgreSQL.

### `/healthz` returns 503

Verify `.env` and database settings:

- `DATABASE_URL` is present
- Driver is PostgreSQL (`postgresql...`)
- Host is `db`

Then rebuild/restart:

```bash
docker compose up -d --build
```

### Migration fails

```bash
docker compose exec web alembic upgrade head
docker compose exec web alembic current
docker compose exec web alembic history --verbose
```

### Auth / host / CSRF issues

Review:

- `ENABLE_BASIC_AUTH`
- `ALLOWED_HOSTS`
- `TRUSTED_ORIGINS`
- `CSRF_PROTECT`, `STRICT_CSRF_CHECK`

Then restart with rebuild:

```bash
docker compose up -d --build
```

### Slot-state data not updating

If using poller profile:

```bash
docker compose --profile slot-poller up -d --build
docker compose logs --tail=200 slot-poller
```

Validate `BAMBU_PRINTERS_JSON` and related `SLOT_STATE_*` values.

If using local bridge, validate endpoint/auth/reachability via `local_services/README.md`.

### Backup/restore issues

- Run script-based backup and restore drill first.
- Validate backup files (`*.dump`, `*.sha256`) and directory permissions.
- Use rollback procedures from `deploy/ROLLBACK_RUNBOOK.md` for production incidents.

## References

- `DB_FUNKTIONSHANDBUCH.md`
- `deploy/GO_LIVE_CHECKLIST.md`
- `deploy/ROLLBACK_RUNBOOK.md`
- `local_services/README.md`
- `slicer_hooks/README.md`
