# Go-Live Checklist

Diese Checkliste ist als **sequentielle Runbook-Liste** gedacht. Jeder Schritt hat ein klares Erfolgskriterium.

## 0) Voraussetzungen

- Optional: Domain zeigt auf den Zielhost
- Ports `22`, `80`, `443` offen
- Repository unter `/opt/filament_datenbank` geklont
- `.env` mit produktiven Werten vorhanden

## 1) Pre-Flight (vor dem Release)

```bash
cd /opt/filament_datenbank
```

```bash
git status
```

Erwartung:

- Keine ungewollten lokalen Änderungen im Deploy-Ordner

```bash
cat .env | egrep '^(APP_ENV|DATABASE_URL|POSTGRES_USER|POSTGRES_DB|ENABLE_BASIC_AUTH|ALLOWED_HOSTS|COOKIE_SECURE|CSRF_PROTECT)='
```

Erwartung:

- `APP_ENV=production`
- `ENABLE_BASIC_AUTH=1`
- `COOKIE_SECURE=1`
- `CSRF_PROTECT=1`
- `ALLOWED_HOSTS` enthält deine Domain

## 2) Release ausrollen

```bash
cd /opt/filament_datenbank
git pull
docker compose pull
docker compose --profile https --profile slot-poller up -d --build
```

Erwartung:

- `docker compose` ohne Fehler abgeschlossen

```bash
docker compose exec web alembic upgrade head
```

Erwartung:

- Alembic auf `head`

## 3) Runtime-Checks (intern)

```bash
docker compose ps
```

Erwartung:

- `web` und `db` laufen, Health ist `healthy`

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

Erwartung:

- JSON mit `"ok": true` und `"database": "ok"`

```bash
docker compose logs --tail=100 web
docker compose logs --tail=100 db
```

Erwartung:

- Keine Crash-Loops, keine dauerhaften DB-Fehler

## 4) Runtime-Checks (extern)

```bash
curl -i https://DEINE_DOMAIN/healthz
```

Erwartung:

- HTTP 200
- Health JSON wie intern

Manueller Funktionscheck im Browser:

- Login via Basic Auth funktioniert
- Dashboard lädt
- Import/Booking/Labels öffnen ohne 5xx

## 5) Backup & Restore-Drill (Go-Live Pflicht)

```bash
REPO_DIR=/opt/filament_datenbank BACKUP_DIR=/opt/filament_backups RETENTION_DAYS=14 /opt/filament_datenbank/deploy/postgres_backup.sh
```

Erwartung:

- Neues `.dump` und `.sha256` unter `/opt/filament_backups`

```bash
REPO_DIR=/opt/filament_datenbank BACKUP_DIR=/opt/filament_backups /opt/filament_datenbank/deploy/postgres_restore_drill.sh
```

Erwartung:

- Ausgabe endet mit `restore drill passed`

## 6) Monitoring-Start (direkt nach Go-Live)

```bash
crontab -l
```

Erwartung:

- Backup-Cron aktiv (siehe Deploy-Doku)

```bash
tail -n 50 /var/log/filament_backup.log
```

Erwartung:

- Letzter Backup-Lauf ohne Fehler

## 7) Rollback-Kriterien (harte Trigger)

Rollback starten, wenn eines davon zutrifft:

- `healthz` > 5 Minuten nicht stabil (`503`/Timeout)
- Wiederholte Fehler in `web`/`db` Logs trotz Re-Deploy
- Dateninkonsistenz nach Deploy (z. B. fehlerhafte Buchungen)

Dann ausführen:

- [ROLLBACK_RUNBOOK.md](deploy/ROLLBACK_RUNBOOK.md)

## 8) Abschluss

- Go-Live-Zeitpunkt dokumentiert
- Commit-ID dokumentiert
- Ergebnis des Restore-Drills dokumentiert
- Erstes 24h-Follow-up im Kalender eingeplant
