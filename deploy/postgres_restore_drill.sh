#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/filament_datenbank}"
BACKUP_DIR="${BACKUP_DIR:-/opt/filament_backups}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
BACKUP_FILE="${1:-}"
DRILL_DB="filament_restore_drill_$(date -u +%Y%m%d%H%M%S)"

cd "$REPO_DIR"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[error] compose file not found: $REPO_DIR/$COMPOSE_FILE"
  exit 1
fi

if [ -z "$BACKUP_FILE" ]; then
  BACKUP_FILE="$(ls -1t "$BACKUP_DIR"/filament_pg_*.dump 2>/dev/null | head -n 1 || true)"
fi

if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
  echo "[error] no backup file found"
  exit 1
fi

echo "[step] restore drill with backup: $BACKUP_FILE"

docker compose exec -T db sh -lc "createdb -U \"\$POSTGRES_USER\" '$DRILL_DB'"

cleanup() {
  docker compose exec -T db sh -lc "dropdb -U \"\$POSTGRES_USER\" --if-exists '$DRILL_DB'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat "$BACKUP_FILE" | docker compose exec -T db sh -lc "pg_restore -U \"\$POSTGRES_USER\" -d '$DRILL_DB' --clean --if-exists --no-owner --no-privileges"

TABLE_COUNT="$(docker compose exec -T db sh -lc "psql -U \"\$POSTGRES_USER\" -d '$DRILL_DB' -tAc \"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';\"" | tr -d '[:space:]')"

if [ -z "$TABLE_COUNT" ] || [ "$TABLE_COUNT" = "0" ]; then
  echo "[error] restore drill failed: no public tables found"
  exit 1
fi

echo "[ok] restore drill passed (public tables: $TABLE_COUNT)"
