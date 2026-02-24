#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/filament_datenbank}"
DOMAIN="${DOMAIN:-}"
REQUIRE_EXTERNAL="${REQUIRE_EXTERNAL:-1}"

ok() {
  echo "[ok] $*"
}

warn() {
  echo "[warn] $*"
}

fail() {
  echo "[fail] $*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "command missing: $1"
}

require_cmd docker
require_cmd curl
require_cmd grep

cd "$REPO_DIR" || fail "repo dir not found: $REPO_DIR"
[ -f "docker-compose.yml" ] || fail "docker-compose.yml missing in $REPO_DIR"
[ -f ".env" ] || fail ".env missing in $REPO_DIR"

ok "repo found: $REPO_DIR"

required_env_keys=(
  APP_ENV
  DATABASE_URL
  POSTGRES_USER
  POSTGRES_DB
  ENABLE_BASIC_AUTH
  ALLOWED_HOSTS
  COOKIE_SECURE
  CSRF_PROTECT
)

for key in "${required_env_keys[@]}"; do
  if ! grep -Eq "^${key}=" .env; then
    fail "missing env key in .env: $key"
  fi
done
ok "required .env keys are present"

get_env_value() {
  local key="$1"
  grep -E "^${key}=" .env | tail -n1 | cut -d'=' -f2- | tr -d '\r'
}

app_env="$(get_env_value APP_ENV || true)"
enable_basic_auth="$(get_env_value ENABLE_BASIC_AUTH || true)"
cookie_secure="$(get_env_value COOKIE_SECURE || true)"
csrf_protect="$(get_env_value CSRF_PROTECT || true)"

[ "$app_env" = "production" ] || fail "APP_ENV should be production (current: $app_env)"
[ "$enable_basic_auth" = "1" ] || warn "ENABLE_BASIC_AUTH is not 1"
[ "$cookie_secure" = "1" ] || warn "COOKIE_SECURE is not 1"
[ "$csrf_protect" = "1" ] || warn "CSRF_PROTECT is not 1"
ok "critical env defaults checked"

docker compose ps >/dev/null
ok "docker compose is reachable"

if ! docker compose ps --status running db >/dev/null 2>&1; then
  fail "db service is not running"
fi
if ! docker compose ps --status running web >/dev/null 2>&1; then
  fail "web service is not running"
fi
ok "web and db services are running"

if docker compose exec -T web alembic current >/tmp/filament_alembic_current.txt 2>/tmp/filament_alembic_err.txt; then
  if grep -q "head" /tmp/filament_alembic_current.txt; then
    ok "alembic revision is at head"
  else
    warn "could not confirm alembic head; output:"
    cat /tmp/filament_alembic_current.txt
  fi
else
  warn "alembic current check failed"
  cat /tmp/filament_alembic_err.txt || true
fi

internal_health="$(curl -sS -o /tmp/filament_health_internal.json -w "%{http_code}" http://127.0.0.1:8000/healthz || true)"
if [ "$internal_health" != "200" ]; then
  fail "internal healthz failed (http $internal_health)"
fi
if ! grep -q '"ok"[[:space:]]*:[[:space:]]*true' /tmp/filament_health_internal.json; then
  fail "internal healthz did not report ok=true"
fi
if ! grep -q '"database"[[:space:]]*:[[:space:]]*"ok"' /tmp/filament_health_internal.json; then
  fail "internal healthz did not report database=ok"
fi
ok "internal healthz is healthy"

if [ -z "$DOMAIN" ] && [ "$REQUIRE_EXTERNAL" = "1" ]; then
  fail "DOMAIN not set. Run with DOMAIN=deine-domain.tld"
fi

if [ -n "$DOMAIN" ]; then
  external_health="$(curl -sS -o /tmp/filament_health_external.json -w "%{http_code}" "https://${DOMAIN}/healthz" || true)"
  if [ "$external_health" != "200" ]; then
    fail "external healthz failed for https://${DOMAIN}/healthz (http $external_health)"
  fi
  if ! grep -q '"ok"[[:space:]]*:[[:space:]]*true' /tmp/filament_health_external.json; then
    fail "external healthz did not report ok=true"
  fi
  ok "external healthz is healthy (${DOMAIN})"
fi

ok "go-live automated checks passed"
