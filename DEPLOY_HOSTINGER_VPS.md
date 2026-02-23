# Deploy auf Hostinger VPS (Docker Compose + Nginx)

Diese Anleitung ist der **verbindliche Produktionspfad** für dieses Projekt:

- App + PostgreSQL laufen in Docker Compose
- Nginx auf dem Host terminiert TLS und proxyt auf `127.0.0.1:8000`
- PostgreSQL ist **nicht** öffentlich erreichbar
- Live-AMS-Daten werden bevorzugt **lokal beim Endnutzer** erfasst und an den externen VPS gepusht

## Zielarchitektur (Hostinger extern)

- **Extern (Hostinger VPS):** FastAPI, PostgreSQL, UI, Business-Logik
- **Lokal (Nutzer-PC):** `local_services/local_slot_bridge.py` liest Drucker/AMS im LAN
- **Datenfluss:** LAN-Drucker -> lokaler Bridge-Dienst -> `https://DEINE_DOMAIN/api/slot-state/push`

Damit bleibt der Server internet-tauglich, ohne direkten LAN-Zugriff auf Kundendrucker zu benötigen.

## 1) Server vorbereiten

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-plugin nginx certbot python3-certbot-nginx
sudo systemctl enable --now docker
```

## 2) Projekt ausrollen

```bash
cd /opt
sudo git clone <DEIN_REPO_URL> filament_datenbank
cd filament_datenbank
sudo cp .env.example .env
sudo chown $USER:$USER .env
```

Danach `.env` bearbeiten und mindestens diese Werte setzen:

- `POSTGRES_PASSWORD` auf starkes Passwort
- `DATABASE_URL` konsistent zu `POSTGRES_*` lassen
- optional `PORT` (Standard `8000`)
- `ENABLE_BASIC_AUTH=1`, `BASIC_AUTH_USERNAME`, `BASIC_AUTH_PASSWORD`
- `ALLOWED_HOSTS` auf deine Domain setzen (z. B. `filament.example.com`)
- optional `TRUSTED_ORIGINS` mit `https://DEINE_DOMAIN`
- `DEFAULT_ADMIN_EMAIL` und `DEFAULT_ADMIN_PASSWORD` für den initialen Admin-Zugang

## 3) Stack starten

```bash
docker compose pull
docker compose up -d --build
docker compose ps
```

Hinweis: `slot-poller` ist absichtlich als optionales Compose-Profil konfiguriert und startet **nicht** standardmäßig auf dem VPS.

Schema-Migrationen anwenden:

```bash
docker compose exec web alembic upgrade head
```

Initialen Admin sicherstellen (auch bei bereits vorhandenen Usern):

```bash
chmod +x deploy/ensure_default_admin.sh
REPO_DIR=/opt/filament_datenbank ./deploy/ensure_default_admin.sh
```

Optional Passwort forcieren (Rotation / Recovery):

```bash
REPO_DIR=/opt/filament_datenbank RESET_PASSWORD=1 ./deploy/ensure_default_admin.sh
```

Schnellcheck:

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

Der Healthcheck liefert DB-Readiness (`database: ok|error`) und gibt bei DB-Problemen HTTP `503` zurück.

## 4) Nginx Reverse Proxy

Datei `/etc/nginx/sites-available/filament-db`:

```nginx
server {
    listen 80;
    server_name DEINE_DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /healthz {
        proxy_pass http://127.0.0.1:8000/healthz;
    }
}
```

Aktivieren:

```bash
sudo ln -s /etc/nginx/sites-available/filament-db /etc/nginx/sites-enabled/filament-db
sudo nginx -t
sudo systemctl reload nginx
```

## 5) TLS (Let's Encrypt)

```bash
sudo certbot --nginx -d DEINE_DOMAIN
```

## 6) Update-Workflow

```bash
cd /opt/filament_datenbank
git pull
docker compose up -d --build
docker compose exec web alembic upgrade head
docker compose ps
```

Wenn du den serverseitigen Poller bewusst aktivieren willst (Sonderfall), nutze:

```bash
docker compose --profile slot-poller up -d --build
```

Vor jedem Release sollten die CI-Checks aus `.github/workflows/ci.yml` grün sein.

## 7) Security-Minimum vor Go-Live

- `.env` niemals committen
- Starke Passwörter und Secret-Rotation
- Firewall aktiv (nur 22/80/443 von außen)
- Healthcheck extern prüfen: `https://DEINE_DOMAIN/healthz`
- Basic Auth aktiv lassen, wenn die App öffentlich erreichbar ist

Runtime-Logs (Compose):

```bash
docker compose logs -f web
docker compose logs -f db
```

## 8) Backups (aktueller Stand)

Für PostgreSQL steht Backup/Restore in der App als `.dump` (Custom Format) zur Verfügung.

Hinweise:

- Der Web-Container benötigt `pg_dump` und `pg_restore` (im aktuellen `Dockerfile` enthalten).
- Für automatisierte Nacht-Backups trotzdem zusätzlich einen VPS-Cronjob mit `pg_dump` einrichten.

### 8.1 Automatisierte Backups + Retention

Scripts im Repository:

- `deploy/postgres_backup.sh`
- `deploy/postgres_restore_drill.sh`
- `deploy/ROLLBACK_RUNBOOK.md`

Auf dem VPS ausführbar machen:

```bash
cd /opt/filament_datenbank
chmod +x deploy/postgres_backup.sh deploy/postgres_restore_drill.sh
```

Cronjob (täglich 03:15 UTC, 14 Tage Retention):

```bash
crontab -e
```

Eintrag:

```cron
15 3 * * * REPO_DIR=/opt/filament_datenbank BACKUP_DIR=/opt/filament_backups RETENTION_DAYS=14 /opt/filament_datenbank/deploy/postgres_backup.sh >> /var/log/filament_backup.log 2>&1
```

### 8.2 Restore-Drill (mind. monatlich)

```bash
REPO_DIR=/opt/filament_datenbank BACKUP_DIR=/opt/filament_backups /opt/filament_datenbank/deploy/postgres_restore_drill.sh
```

Optional mit explizitem Backupfile:

```bash
/opt/filament_datenbank/deploy/postgres_restore_drill.sh /opt/filament_backups/filament_pg_YYYYMMDD_HHMMSS.dump
```

### 8.3 Rollback-Runbook

Siehe `deploy/ROLLBACK_RUNBOOK.md` für App- und DB-Rollback inkl. Health-Checks.

## 9) Go-Live Abarbeitung

Für den finalen Rollout nutze die sequenzielle Checkliste unter `deploy/GO_LIVE_CHECKLIST.md`.

Optional als One-Command Check auf dem VPS:

```bash
cd /opt/filament_datenbank
chmod +x deploy/go_live_check.sh
DOMAIN=DEINE_DOMAIN REPO_DIR=/opt/filament_datenbank ./deploy/go_live_check.sh
```

## 10) Lokale Bridge zum externen Server (empfohlen)

Auf dem Endnutzer-PC (gleiches LAN wie Drucker) läuft:

```powershell
$env:BAMBU_PRINTERS_JSON='[{"name":"P1S-01","host":"192.168.1.50","serial":"01S00XXXXXXXX","access_code":"12345678"}]'
python .\local_services\local_slot_bridge.py --endpoint "https://DEINE_DOMAIN/api/slot-state/push" --project private --source local-slot-bridge --auth-user "admin" --auth-password "DEIN_PASSWORT"
```

Damit landen Live-Slotdaten zuverlässig auf dem externen Hostinger-Server, auch wenn dieser keinen direkten Zugriff auf das lokale Drucker-LAN hat.
