#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/filament_datenbank}"
RESET_PASSWORD="${RESET_PASSWORD:-0}"

cd "$REPO_DIR"

if [ ! -f "docker-compose.yml" ]; then
  echo "[error] docker-compose.yml not found in $REPO_DIR"
  exit 1
fi

if ! docker compose ps --status running web >/dev/null 2>&1; then
  echo "[error] web service is not running"
  exit 1
fi

docker compose exec -T web env RESET_PASSWORD="$RESET_PASSWORD" python - <<'PY'
import os
from app.main import SessionLocal, _hash_password
from app.models import User

email = str(os.getenv("DEFAULT_ADMIN_EMAIL", "")).strip().lower()
password = str(os.getenv("DEFAULT_ADMIN_PASSWORD", "")).strip()
reset_password = str(os.getenv("RESET_PASSWORD", "0")).strip().lower() in {"1", "true", "yes", "on"}

if not email or not password:
    raise SystemExit("[error] DEFAULT_ADMIN_EMAIL or DEFAULT_ADMIN_PASSWORD is empty")

db = SessionLocal()
try:
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            display_name="Administrator",
            password_hash=_hash_password(password),
            is_active=True,
        )
        db.add(user)
        db.commit()
        print(f"[ok] admin user created: {email}")
    else:
        changed = False
        if not bool(user.is_active):
            user.is_active = True
            changed = True
        if reset_password:
            user.password_hash = _hash_password(password)
            changed = True
        if changed:
            db.commit()
            action = "reactivated + password reset" if reset_password else "reactivated"
            print(f"[ok] admin user updated: {email} ({action})")
        else:
            print(f"[ok] admin user already present: {email}")
finally:
    db.close()
PY
