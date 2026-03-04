# DB Funktionshandbuch (Filament_Datenbank)

Stand: 2026-02-21

## 1) Laufzeit- und DB-Basis

- **Runtime**: Docker Compose only
- **Datenbank**: PostgreSQL (Compose-Service `db`)
- **Verbindungsquelle**: `DATABASE_URL`
- **Schema-Management**: Alembic Migrationen
- **Zielbetrieb**: Docker-Compose-Stack mit PostgreSQL; lokale Dienste optional als Datenzulieferer

Wichtige Dateien:
- `app/db.py`
- `app/models.py`
- `alembic/versions/20260220_000001_initial_schema.py`
- `alembic/versions/20260221_000002_usage_batch_context.py`
- `alembic/versions/20260221_000003_spool_slot_mapping.py`
- `alembic/versions/20260221_000004_device_slot_state.py`

---

## 2) Tabellen und Zweck

## `spools`
Inventar der Filamentspulen.

Felder (Kern):
- `id`
- `project` (`private`/`business`)
- `brand`, `material`, `color`
- `weight_g`, `remaining_g`
- `low_stock_threshold_g`
- `price`, `location`
- `in_use`
- `ams_printer`, `ams_slot` (feste Slot-Zuordnung)
- `created_at`, `updated_at`

## `usage_history`
Transaktionshistorie aller Abbuchungen (manuell/automatisch).

Felder (Kern):
- `id`, `created_at`, `project`
- `mode`, `actor`, `source_app`, `source_file`
- `batch_id` (Job-Gruppierung / Idempotenz)
- Snapshot-Felder: `spool_id`, `spool_brand`, `spool_material`, `spool_color`
- `deducted_g`, `remaining_before_g`, `remaining_after_g`
- `undone`, `undone_at`

## `usage_batch_context`
Kontext je Batch/Job (ein Eintrag pro `project + batch_id`).

Felder (Kern):
- `project`, `batch_id` (unique)
- `printer_name`
- `ams_slots` (CSV-ähnlich, z. B. `1,3`)
- `created_at`

## `device_slot_state`
Ist-Zustand je Drucker/Slot aus dem Polling-Worker.

Felder (Kern):
- `project`, `printer_name`, `slot` (unique je Kombination)
- `observed_brand`, `observed_material`, `observed_color`
- `source`, `observed_at`, `updated_at`

## `app_settings`
Persistente App-Settings (z. B. Sprache/Theme/Layouts).

---

## 3) Feature-Matrix (Funktion → DB-Effekt)

## Spulenverwaltung
- **Neu anlegen**: schreibt in `spools`
- **Bulk anlegen**: mehrere Inserts in `spools`
- **Bearbeiten**: Update in `spools`
- **Löschen**: Delete in `spools`
- **In Nutzung toggeln**: Update `spools.in_use`
- **AMS-Konfliktschutz**: gleiche Kombination `project + ams_printer + ams_slot` wird im Backend blockiert

Endpoints (Auszug):
- `GET /spools`, `GET /spools/new`, `POST /spools/new`
- `GET /spools/bulk`, `POST /spools/bulk`
- `GET /spools/{spool_id}/edit`, `POST /spools/{spool_id}/edit`
- `POST /spools/{spool_id}/delete`, `POST /spools/{spool_id}/toggle-use`

## Verbrauch/Buchung
- **Manuell/halbautomatisch**: reduziert `spools.remaining_g`, schreibt `usage_history`
- **Undo letzte Buchung**: markiert Historie als `undone`, schreibt Mengen zurück in `spools`
- **Tracking-Ansicht**: liest gruppiert aus `usage_history` + `usage_batch_context`

Endpoints:
- `GET /booking`, `POST /booking`
- `GET /booking/tracking`, `POST /booking/tracking`

## Auto-Abzug API (Slicer)
- Dateiupload (`.3mf/.gcode/.gco/.bgcode`)
- erkennt Verbrauch und Material-Hinweise
- **slot-sicheres Matching** bei vorhandener Slot-Info
- idempotent über `job_id`/`batch_id`
- schreibt `usage_history` (+ optional `usage_batch_context`)

Endpoints:
- `POST /api/usage/auto-from-file`
- `POST /api/usage/auto-from-3mf`

Form-Felder:
- `file` (Pflicht)
- `project`, `slicer`
- `printer`, `ams_slots`
- `job_id`, `dry_run`

## Presets / Schwellwerte
- Marken/Material/Farben/Color-Map
- Low-Stock per Spule und Material-defaults
- Material-Gesamtschwellen

Endpoints (Auszug):
- `GET /presets`
- `POST /presets/brand|material|color|color-map|color-map/import|low-stock-threshold`
- `GET /thresholds`
- `POST /thresholds/spool|spool/delete|material-default|material-default/delete|material-total|material-total/delete`

## Import/Export
- **Import CSV/XLSX**: Inserts in `spools`
- **Export CSV/XLSX**: Read-only aus `spools`

Endpoints:
- `GET /import-export`, `POST /import-export`
- `GET /import` (Legacy-Redirect auf `/import-export`), `POST /import` (Legacy-kompatibel)
- `GET /export/csv`, `GET /export/excel`

## Backup/Restore
- PostgreSQL-Backup/Restore über `pg_dump`/`pg_restore` (im Container)
- UI-Endpunkte für Export/Import

Endpoints:
- `GET /backup`, `GET /backup/export`, `POST /backup/import`

---

## 4) Ops/Automation rund um DB

Skripte:
- `deploy/postgres_backup.sh` (Backup + Retention)
- `deploy/postgres_restore_drill.sh` (Restore-Drill in temporäre DB)
- `scripts/check_postgres_sequences.py` (ID-Sequence Drift Fix)
- `scripts/device_slot_poller.py` (Live Slot-State Polling)
- `scripts/dev_preflight.ps1` (Compose up, Sequence-Check, Tests)
- `deploy/go_live_check.sh` (Health/Config/Service-Checks)

---

## 5) Live-AMS-Status: ist das möglich?

Kurz: **Ja, als MVP implementiert**.

Aktueller Stand:
- Slot-Zuordnung wird im Backend gepflegt (`spools.ams_printer` + `spools.ams_slot`).
- Auto-Abzug nutzt diese Zuordnung strikt.
- Lokaler Bridge-Dienst kann Live-Slotdaten an den Server pushen (`POST /api/slot-state/push`).
- Optionaler Polling-Worker schreibt Live-Slotdaten in `device_slot_state`.
- UI `/slot-status` zeigt den Soll/Ist-Vergleich.

## Implementierter MVP-Ansatz

- Lokale Erfassung via `local_services/local_slot_bridge.py`
- Push zum Server via `POST /api/slot-state/push`
- Optional Polling via `scripts/device_slot_poller.py` (Compose-Profil `slot-poller`)
- Persistenz in `device_slot_state`
- Soll/Ist-Visualisierung auf `/slot-status`

Erweiterbar um:
- direkte Hersteller-API-Adapter
- pro Slot zusätzliche Seriennummer/Spulen-ID-Fingerprints
- harte Blockierlogik bei Abweichungen vor Auto-Abzug

---

## 6) Empfehlung für dieses Projekt

1. Kurzfristig: **Pre-Commit/Pre-Booking Validierung** einbauen
   - Vor Abbuchung prüfen: stimmt `printer + slot` mit gemappter Spule?
   - bei Abweichung: keine Abbuchung, klarer Konfliktfehler

2. Mittelfristig: **lokaler Agent + API-Push als Standardpfad**
   - lokaler Dienst im Nutzer-LAN
   - Push auf die laufende App (`/api/slot-state/push`)
   - optionaler Polling-Worker nur als Sonderfall

3. Langfristig: **UI-Ansicht „Live Slot State“**
   - Soll/Ist-Vergleich pro Drucker-Slot

---

## 7) Vollständige Route-Übersicht (aktuell)

- `GET /healthz`
- `POST /settings`, `GET /settings`
- `GET /`, `GET /spools`, `GET /analysis`, `GET /thresholds`
- `GET /slot-status`
- `POST /thresholds/spool`, `POST /thresholds/spool/delete`
- `POST /thresholds/material-default`, `POST /thresholds/material-default/delete`
- `GET /spools/new`, `GET /spools/bulk`, `POST /spools/new`, `POST /spools/bulk`
- `GET /spools/{spool_id}/edit`, `POST /spools/{spool_id}/edit`
- `POST /spools/{spool_id}/delete`, `POST /spools/{spool_id}/toggle-use`
- `GET /spools/{spool_id}/qr`
- `GET /qr-scan`, `POST /qr-scan`, `GET /qr-scan/manage/{spool_id}`, `POST /qr-scan/action`
- `GET /labels`, `POST /labels/layouts`, `POST /labels`
- `GET /usage`, `GET /booking`, `GET /booking/tracking`
- `POST /usage`, `POST /booking`, `POST /booking/tracking`
- `POST /api/usage/auto-from-file`, `POST /api/usage/auto-from-3mf`
- `POST /api/slot-state/push`
- `GET /import-export`, `POST /import-export`
- `GET /import` (Legacy-Redirect auf `/import-export`), `POST /import` (Legacy-kompatibel)
- `GET /backup`, `GET /backup/export`, `POST /backup/import`
- `GET /export/csv`, `GET /export/excel`

---

## 8) Schnellbefehle (Docker-only)

Migrationen:

```bash
docker compose exec web alembic upgrade head
```

Tests (fokussiert):

```bash
docker compose run --rm -e ENABLE_BASIC_AUTH=0 web python -m unittest tests.test_spool_list_hide_empty tests.test_api_auto_usage tests.test_slot_status_page
```

Backup-Script:

```bash
bash deploy/postgres_backup.sh
```
