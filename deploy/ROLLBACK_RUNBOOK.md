# Rollback Runbook

## Voraussetzungen

- Projekt liegt unter `/opt/filament_datenbank`
- Backups liegen unter `/opt/filament_backups`
- Aktueller Stack läuft via `docker compose`

## 1) Lagebild erfassen

```bash
cd /opt/filament_datenbank
docker compose ps
docker compose logs --tail=200 web
docker compose logs --tail=200 db
curl -i http://127.0.0.1:8000/healthz
```

Wenn `healthz` dauerhaft `503` oder Fehler in den Logs zeigt, Rollback starten.

## 2) App-Rollback (Code/Container)

```bash
cd /opt/filament_datenbank
git log --oneline -n 10
```

Auf letzten stabilen Commit zurück:

```bash
git checkout <STABILER_COMMIT>
docker compose --profile slot-poller up -d --build
docker compose exec web alembic upgrade head
curl -fsS http://127.0.0.1:8000/healthz
```

## 3) DB-Rollback (nur wenn Daten inkonsistent)

Nutzt den zuletzt validierten Dump:

```bash
ls -1t /opt/filament_backups/filament_pg_*.dump | head -n 5
```

Wiederherstellung auf produktive DB:

```bash
cat /opt/filament_backups/<DATEI>.dump \
  | docker compose exec -T db sh -lc 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges'
```

Danach Health prüfen:

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

## 4) Nacharbeiten

- Incident-Zeitpunkt, Ursache, Commit-ID dokumentieren
- Betroffene Nutzer/Zeitraum notieren
- Folge-Maßnahme als Issue erfassen (Regression-Test, Monitoring, Guardrail)
