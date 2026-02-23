#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/filament_datenbank}"
BACKUP_DIR="${BACKUP_DIR:-/opt/filament_backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/filament_pg_${TIMESTAMP}.dump"

mkdir -p "$BACKUP_DIR"
cd "$REPO_DIR"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[error] compose file not found: $REPO_DIR/$COMPOSE_FILE"
  exit 1
fi

if ! docker compose ps --status running db >/dev/null 2>&1; then
  echo "[error] db service is not running"
  exit 1
fi

echo "[step] creating backup: $BACKUP_FILE"
docker compose exec -T db sh -lc 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP_FILE"

if [ ! -s "$BACKUP_FILE" ]; then
  echo "[error] backup file is empty"
  rm -f "$BACKUP_FILE"
  exit 1
fi

sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"

find "$BACKUP_DIR" -type f -name 'filament_pg_*.dump' -mtime +"$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -type f -name 'filament_pg_*.dump.sha256' -mtime +"$RETENTION_DAYS" -delete

echo "[ok] backup created: $BACKUP_FILE"
