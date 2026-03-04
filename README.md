# Filament Datenbank (Web-App)

Docker-Compose Web-App zum Verwalten von Filamentspulen mit modernem UI, 3MF-Usage-Tracking, QR-Codes, Suche, Statistiken sowie CSV/Excel Import/Export.

## Features

- Spulenverwaltung (Marke, Material, Farbe, Gewicht, Restmenge, Preis, Lagerort)
- Hierarchische Lagerorte (Bereich/Fach, z. B. R1/A1) mit eigener Verwaltungsseite
- Multi-Profil/Projekt-Modus (Privat/Business) mit getrennten Beständen
- Etikettendruck mit QR + Materialdaten (A4/Labelbogen)
- 3MF-Upload für Verbrauchs-Tracking (optional manuelle Grammangabe)
- QR-Codes pro Spule
- QR-Codes für Lagerorte (Regal/Fach) mit Scan-Filter auf Spulenliste
- Suche & Statistiken
- CSV/Excel Import & Export
- Zweisprachige Oberfläche (DE/EN)
- Mehrseitige Hilfeseiten mit Funktionskapiteln unter `/help`

## Hilfeseiten

- Einstieg: `/help`
- Kapitel:
  - `/help/inventory`
  - `/help/booking`
  - `/help/printers-ams`
  - `/help/slot-status`
  - `/help/labels-qr`
  - `/help/storage-qr`
  - `/help/analysis-audit`
  - `/help/presets`
  - `/help/backup`

Die Seiten enthalten Ablaufbeschreibungen, praxisnahe Schritte und Bildmaterial (`app/static/help/*`).

### Demo-Daten für Hilfescreenshots (nur Staging)

Demo-Daten nie in der produktiven Instanz erzeugen. Nutze ausschließlich eine separate Staging-Umgebung.

```bash
docker compose exec web python scripts/help_demo_seed.py --project private
# Screenshots aufnehmen
docker compose exec web python scripts/help_demo_cleanup.py --project private --confirm
```

## Setup

1. `.env.example` nach `.env` kopieren und bei Bedarf Werte anpassen
2. Starten: `docker compose up -d --build`
3. App öffnen: `http://127.0.0.1:8000`

### Lokales HTTPS (ohne Domain)

Für lokales HTTPS-Testing gibt es ein separates Compose-Profil mit internem Zertifikat:

```bash
docker compose --profile https-local up -d --build
```

Aufruf lokal:

- `https://localhost:8443`

Hinweis: Der Browser zeigt beim ersten Aufruf eine Zertifikatswarnung (lokales internes Zertifikat). Für lokalen Testbetrieb ist das erwartetes Verhalten.

### Lokales HTTPS vom Handy (gleiches WLAN)

1. In `.env` setzen: `LAN_HOST=<DEINE_PC_LAN_IP>` (z. B. `192.168.178.50`)
2. Profil starten:

```bash
docker compose --profile https-lan up -d --build
```

3. Am Handy aufrufen:

- `https://<DEINE_PC_LAN_IP>:8443`

Hinweise:

- Handy und PC müssen im selben Netzwerk sein.
- Windows-Firewall muss eingehend `8443` (und optional `8080`) erlauben.
- Das lokale Zertifikat ist nicht öffentlich vertrauenswürdig; für produktive Handy-Nutzung ohne Warnung nutze die Domain-Variante mit `--profile https`.

### HTTPS für externe Nutzung (Handy/Kamera/QR)

Für produktive/externe Nutzung läuft HTTPS vollständig im Compose-Stack über Caddy (Let's Encrypt automatisch):

1. In `.env` setzen:
  - `DOMAIN=deine-domain.tld`
  - `TLS_EMAIL=admin@deine-domain.tld`
  - `FORCE_HTTPS_REDIRECT=1`
  - optional `PUBLIC_BASE_URL=https://deine-domain.tld` (Header-QR nutzt sonst den aktuellen Request-Host)
2. DNS A/AAAA Record der Domain auf den Zielhost zeigen lassen
3. Ports `80` und `443` in der Host-Firewall freigeben
4. Stack mit HTTPS-Profil starten:

```bash
docker compose --profile https up -d --build
```

Danach ist die App unter `https://deine-domain.tld` erreichbar.

Stoppen:

```bash
docker compose down
```

## Produktion / Betrieb

Der Betrieb erfolgt über Docker Compose mit PostgreSQL (Compose-Service `db`).

Das Projekt ist dafür vorbereitet:

- Datenbank per Umgebungsvariable `DATABASE_URL` (Compose-PostgreSQL `db`)
- Produktionsserver mit Gunicorn + Uvicorn Worker
- Health-Endpoint: `GET /healthz` (inkl. DB-Readiness, bei DB-Fehler HTTP `503`)
- Versionierte Datenbankmigrationen mit Alembic
- Beispiel-Dateien:
  - `.env.example`
  - `Dockerfile`
  - `docker-compose.yml`
  - Externe Deploy-Dokumentation im `deploy/` Bereich

### Docker-Start

1. `.env.example` nach `.env` kopieren und anpassen (`POSTGRES_PASSWORD` stark setzen)
2. Starten mit:

```bash
docker compose up -d --build
```

Für HTTPS-Betrieb auf externer Domain:

```bash
docker compose --profile https up -d --build
```

Wichtig:

- `.env` wird nicht versioniert und darf nie committed werden
- PostgreSQL ist im Compose-Setup nicht öffentlich exponiert
- Für öffentliche Deployments `ENABLE_BASIC_AUTH=1` und starke `BASIC_AUTH_*` Werte setzen
- Nur Docker-Compose-Betrieb ist unterstützt (kein lokaler Python-/venv-/pip-Runpath)

### Schema-Migrationen (Alembic)

Bei PostgreSQL-Deployments müssen Schema-Änderungen über Alembic ausgerollt werden:

```bash
docker compose exec web alembic upgrade head
```

### CI / Release-Gate

GitHub Actions Workflow unter `.github/workflows/ci.yml` prüft bei Push/PR:

- Syntax (`py_compile`)
- Alembic Migration (`alembic upgrade head`)
- Regressionstests (`tests/test_labels_custom_layout.py`, `tests/test_usage_undo_capacity.py`, `tests/test_api_auto_usage.py`)

### Ops / Betrieb

Für den regulären Betrieb:

- In-App Backup-Management unter `/backup` (manuell erstellen, gespeicherte Backups wiederherstellen/löschen, Auto-Backup-Intervalle)
- Persistenter Backup-Speicher über Bind-Mount `./artifacts/db-backups` nach `/home/appuser/backups`
- Backup-Retention Script: `deploy/postgres_backup.sh`
- Restore-Drill Script: `deploy/postgres_restore_drill.sh`
- Rollback-Runbook: `deploy/ROLLBACK_RUNBOOK.md`
- Go-Live-Runbook: `deploy/GO_LIVE_CHECKLIST.md`
- One-Command Go-Live-Check: `deploy/go_live_check.sh`

## Lokaler Preflight (Docker-only, empfohlen)

### One-Command (Windows / PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev_preflight.ps1
```

Der Preflight prüft zusätzlich PostgreSQL-ID-Sequenzen (`spools`, `usage_history`) und korrigiert Sequence-Drift automatisch, um `duplicate key` Fehler nach Import/Restore zu vermeiden.

Optional:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev_preflight.ps1 -SkipTests
```

Hinweis: Der Preflight akzeptiert bewusst kein alternatives DB-Ziel; geprüft wird immer die Compose-DB (`@db`).

### 1) Docker + PostgreSQL lokal starten

1. `.env` so setzen, dass PostgreSQL aus Compose genutzt wird:

```env
DATABASE_URL=postgresql+psycopg://filament:filament@db:5432/filament_db
```

1. Services starten:

```bash
docker compose up -d --build
```

### 2) Schema-Migrationen anwenden

```bash
docker compose exec web alembic upgrade head
```

### 3) Smoke-Test

- App öffnen: `http://127.0.0.1:8000`
- Healthcheck: `http://127.0.0.1:8000/healthz`
- Kernfunktionen prüfen: Spulenliste, Buchung, Labeldruck, Import/Export

Wenn das lokal sauber läuft, ist dein Docker-Compose-Setup einsatzbereit.

## Slicer Auto-Abbuchung (Bambu/Prusa/Orca)

Der Slicer kann nach dem Slicen die erzeugte Datei (`.3mf`, `.gcode`, `.gco`, `.bgcode`) an die App senden und den Verbrauch abbuchen.

1. App starten (`docker compose up -d --build`)
2. Im Slicer ein Post-Processing-Kommando hinterlegen, das den Endpoint direkt aufruft (z. B. `https://DEINE_DOMAIN/api/usage/auto-from-file`).
3. Bei aktivierter App-Basic-Auth müssen Auth-Credentials mitgesendet werden.
4. Gleiches Prinzip funktioniert in Bambu Studio, PrusaSlicer und OrcaSlicer.

### API-Endpoint

- `POST /api/usage/auto-from-file` (empfohlen)
- `POST /api/usage/auto-from-3mf` (abwärtskompatibel)
- `POST /api/slot-state/push` (für lokale Slot-Bridge vom Nutzer-PC)
- Form-Fields:
  - `file` (3MF/GCode-Datei, Pflicht)
  - `project` (`private` oder `business`, optional)
  - `slicer` (z. B. `Bambu Studio`, `PrusaSlicer`, `OrcaSlicer`, optional)
  - `printer` (Druckername für Tracking, optional)
  - `ams_slots` (genutzte AMS-Slots, z. B. `1,2,4`, optional; falls leer, wird aus 3MF-Metadaten erkannt)
  - `job_id` (optional, verhindert Doppelbuchungen)
  - `dry_run` (`1` oder `0`, optional)

### Post-Processing Beispiele (Windows)

Nutze das Script `local_services/slicer_auto_usage.py` als Post-Processing-Kommando. Der Dateipfad der exportierten Datei wird als letztes Argument übergeben.

#### Bambu Studio

```powershell
python .\local_services\slicer_auto_usage.py "$env:SLICER_FILE" --endpoint "https://DEINE_DOMAIN/api/usage/auto-from-file" --project private --slicer "Bambu Studio" --printer "P1S-01"
```

Wenn du den AMS-Slot fix übergeben willst:

```powershell
python .\local_services\slicer_auto_usage.py "$env:SLICER_FILE" --endpoint "https://DEINE_DOMAIN/api/usage/auto-from-file" --project private --slicer "Bambu Studio" --printer "P1S-01" --ams-slots "1,3"
```

#### OrcaSlicer

```powershell
python .\local_services\slicer_auto_usage.py "$env:SLICER_FILE" --endpoint "https://DEINE_DOMAIN/api/usage/auto-from-file" --project private --slicer "OrcaSlicer" --printer "X1C-01"
```

#### PrusaSlicer

```powershell
python .\local_services\slicer_auto_usage.py "$env:SLICER_FILE" --endpoint "https://DEINE_DOMAIN/api/usage/auto-from-file" --project private --slicer "PrusaSlicer" --printer "MK4-01"
```

Hinweise:

- Der Platzhalter für den exportierten Dateipfad ist je nach Slicer unterschiedlich; in der Slicer-Doku den passenden Placeholder einsetzen.
- Endpoint immer explizit setzen (`--endpoint`) oder per Umgebungsvariable `FILAMENT_DB_ENDPOINT` vorbelegen.
- Mit `--job-id` kannst du eine externe Job-ID vorgeben. Ohne Angabe erzeugt das Script automatisch eine stabile ID aus Dateipfad + Dateistat.
- Bei aktivierter Basic-Auth zusätzlich `--auth-user` und `--auth-password` setzen.

### So funktioniert es genau

1. Der Slicer ruft das Script auf und übergibt die erzeugte Datei (`.3mf`, `.gcode`, `.gco`, `.bgcode`).
2. Das Script sendet Multipart-Formdaten an `/api/usage/auto-from-file` (`file`, `project`, `slicer`, optional `printer`, optional `ams_slots`, `job_id`, `dry_run`).
3. Die API parst den Verbrauch aus der Datei:
  - 3MF: inklusive Material-Infos und (wenn vorhanden) Slot-Daten.
  - GCode: über Metadaten-Kommentare.
4. Spulen-Auswahl erfolgt intelligent: zuerst „in Nutzung“, dann passende Materialien/Farben/Marken, danach Kapazitätsaufteilung.
5. Wenn `job_id` bereits verarbeitet wurde, wird nichts doppelt abgebucht (idempotent).
6. Bei echtem Lauf (`dry_run=0`) wird Restmenge je Spule reduziert und `usage_history` geschrieben.
7. Zusätzlich wird pro Batch (`batch_id`) ein Kontext-Eintrag gespeichert (`printer_name`, `ams_slots`) in `usage_batch_context`.
8. Im Tracking unter `/booking/tracking` siehst du dann pro Eintrag: Wer, Datei, Spulen-Aufteilung, Gesamtverbrauch plus Drucker und AMS-Slots.

### Feste Slot→Spule Zuordnung (wichtig bei gleichen Materialien)

Damit bei zwei gleichen Spulen (z. B. beide PLA Schwarz) korrekt abgebucht wird, kannst du jede Spule fest einem Slot zuordnen:

1. Spule öffnen unter `/spools/{id}/edit`
2. Felder setzen:
  - `AMS Drucker` (z. B. `P1S-01`)
  - `AMS Slot` (z. B. `4`)
3. Speichern

Hinweis: Eine doppelte Belegung desselben `AMS Drucker + AMS Slot` im gleichen Projekt wird vom Backend blockiert (Konfliktmeldung im Formular).

Auto-Abzug mit Slot-Infos arbeitet dann wie folgt:

- Wenn die Datei `slot`-Informationen liefert (oder `ams_slots` gesendet wird), wird zuerst die Spule mit passender `AMS Drucker + AMS Slot` Zuordnung gesucht.
- Wenn dafür keine Spule gemappt ist, wird die Buchung für diese Position nicht auf eine „falsche“ ähnliche Spule umgelegt.
- Nur ohne Slot-Info greift der bisherige Material/Farbe/Marke-Fallback.

Kurz: Mit gepflegter Slot-Zuordnung wird nicht mehr „irgendeine“ passende PLA-Spule gewählt, sondern die physisch im Slot hinterlegte Spule.

### AMS-Slot Herkunft

- Priorität 1: explizit übergebenes Feld `ams_slots` (z. B. `1,2`).
- Priorität 2: automatisch aus 3MF-Usage-Breakdown (`slot` je Materialzeile).
- Wenn beides fehlt, bleibt das Feld leer.

## Live Slot-Status (Soll/Ist)

Die Seite `/slot-status` vergleicht:

- **Soll**: Spulen-Mapping aus `spools` (`ams_printer` + `ams_slot`)
- **Ist**: letzte Live-Daten aus `device_slot_state`

## Druckerverwaltung (mehrere Drucker)

Über die Seite `/printers` können mehrere Drucker im Web-Interface verwaltet werden (Name, Seriennummer, Host, Port, Access Code, Aktiv-Flag).

- `serial` ist die technische Identität im Projekt.
- `name` ist der Anzeigename im UI.
- Live-Telemetrie (Status, letzter Kontakt, Job, Temperaturen, Firmware) wird automatisch aus Push/Poller-Daten aktualisiert.

### Demo-Daten für UI-Checks

Für reproduzierbare UI-Tests (inkl. AMS-Live-Slots im Drucker-Popup) kann jederzeit derselbe Demo-Drucker neu befüllt werden:

```bash
docker compose exec web python scripts/demo_printer_seed.py --project private
```

Der Seed aktualisiert/erstellt:

- Demo-Drucker `DEMO-LIVE-001` mit kompletter Live-Telemetrie
- AMS-Live-Slots 1-4 in `device_slot_state`
- Demo-`usage_batch_context` mit AMS-Slotliste

### Empfohlen: lokal abgreifen und an Server senden

Der Endnutzer-PC liest die Drucker lokal im LAN und pusht die Daten zum Server:

```powershell
$env:BAMBU_PRINTERS_JSON='[{"name":"P1S-01","host":"192.168.1.50","serial":"01S00XXXXXXXX","access_code":"12345678"}]'
python .\local_services\local_slot_bridge.py --endpoint "https://dein-server/api/slot-state/push" --project private --source local-slot-bridge
```

Optional bei aktivierter Basic-Auth:

```powershell
python .\local_services\local_slot_bridge.py --endpoint "https://dein-server/api/slot-state/push" --project private --source local-slot-bridge --auth-user "admin" --auth-password "secret"
```

Format für den Push-Endpoint:

```json
{
  "project": "private",
  "source": "local-slot-bridge",
  "printers": [
    {
      "printer": "P1S-01",
      "serial": "01S00XXXXXXXX",
      "telemetry": {
        "status": "online",
        "job_name": "part.3mf",
        "job_status": "RUNNING",
        "progress": 42.3,
        "nozzle_temp": 220.1,
        "bed_temp": 59.7,
        "chamber_temp": 35.0,
        "firmware": "01.08.00.00"
      },
      "slots": [
        { "slot": 1, "brand": "Bambu", "material": "PLA", "color": "Black" }
      ]
    }
  ]
}
```

### Worker aktivieren

Im Compose-Stack läuft ein zusätzlicher Service `slot-poller`:

```bash
docker compose --profile slot-poller up -d --build
```

Standard-Start ohne Profil (`docker compose up -d --build`) startet **ohne** `slot-poller`.

### Poller-Umgebungsvariablen

- `SLOT_STATE_PROVIDER` (`feed`, `bambu_mqtt` oder `multi_brand_http`, Default `feed`)
- `SLOT_STATE_POLL_INTERVAL_SEC` (Default `45`)
- `SLOT_STATE_FEED_URL` (optional, JSON-Endpoint)
- `SLOT_STATE_FEED_TOKEN` (optional Bearer Token)
- `SLOT_STATE_FEED_FILE` (optional lokaler JSON-Pfad im Container)
- `SLOT_STATE_SOURCE` (Kennung in UI/DB, Default `slot-poller`)
- `SLOT_STATE_PROJECT` (Default `private`)
- `SLOT_STATE_STALE_MINUTES` (UI-Stale-Grenze, Default `10`)

Hinweis:

- Die Verbrauchsbuchung erfolgt über Slicer-Postprocessing (`scripts/slicer_auto_usage.py` / `scripts/bambu_studio_auto_usage.py`) und **nicht** über Polling.
- Für sehr häufiges Polling kann `SLOT_STATE_POLL_INTERVAL_SEC=1` gesetzt werden.

Bei `multi_brand_http` zusätzlich:

- `MULTIBRAND_PRINTERS_JSON` (Array mit Drucker-Definitionen und Adapter-Typ)

### Direkt vom Bambu Drucker/AMS (ohne Zwischen-Feed)

Setze in `.env`:

```env
SLOT_STATE_PROVIDER=bambu_mqtt
BAMBU_PRINTERS_JSON=[{"name":"P1S-01","host":"192.168.1.50","serial":"01S00XXXXXXXX","access_code":"12345678"}]
SLOT_STATE_BAMBU_TIMEOUT_SEC=10
```

Hinweise:

- `access_code` ist der LAN-Access-Code vom Drucker.
- Der Poller verbindet sich per MQTT/TLS (`port` standardmäßig `8883`).
- Mehrere Drucker sind über mehrere Einträge in `BAMBU_PRINTERS_JSON` möglich.

### Multi-Brand Adapter (Creality, Prusa, OctoPrint, Klipper)

Setze in `.env`:

```env
SLOT_STATE_PROVIDER=multi_brand_http
MULTIBRAND_PRINTERS_JSON=[
  {"name":"K1-Max","serial":"CREALITY-K1-001","brand":"creality","adapter":"moonraker","base_url":"http://192.168.1.70"},
  {"name":"MK4","serial":"PRUSA-MK4-001","brand":"prusa","adapter":"prusalink","base_url":"http://192.168.1.80","api_key":"<PRUSA_API_KEY>"},
  {"name":"Ender-3","serial":"ENDER3-001","brand":"creality","adapter":"octoprint","base_url":"http://192.168.1.90","api_key":"<OCTOPRINT_API_KEY>"}
]
```

Unterstützte `adapter`-Werte:

- `octoprint` (u. a. für viele Creality/Anycubic/Prusa Setups mit OctoPrint)
- `moonraker` / `klipper` (z. B. Creality K1/K1C, Voron, QIDI, Elegoo mit Klipper)
- `prusalink` / `prusa` (Prusa Link API)
- `generic_http` (eigener Endpoint liefert bereits `telemetry` + `slots` im Standardformat)

Hinweise:

- AMS/MMU-Slots werden nur dann geschrieben, wenn der jeweilige Adapter Slotdaten liefert.
- Telemetrie (Status/Job/Fortschritt/Temperaturen) wird für alle Adapter in die Druckeransicht übernommen.

### Erwartetes JSON-Format

```json
{
  "printers": [
    {
      "printer": "P1S-01",
      "serial": "01S00XXXXXXXX",
      "telemetry": {
        "status": "online",
        "job_status": "RUNNING",
        "progress": 42.3
      },
      "slots": [
        { "slot": 1, "brand": "Bambu", "material": "PLA", "color": "Black" },
        { "slot": 2, "brand": "Bambu", "material": "PETG", "color": "White" }
      ]
    }
  ]
}
```

Alternativ wird auch ein einzelner Printer-Block ohne `printers`-Array akzeptiert.

### Smoke-Test: Mehrere Drucker E2E

Die folgenden Schritte prüfen den kompletten Weg (UI-Verwaltung + API-Ingestion + Anzeige):

1. Stack starten/aktualisieren:

```bash
docker compose up -d --build
docker compose exec web alembic upgrade head
```

2. Drucker im UI anlegen:

- Seite öffnen: `/printers`
- Zwei Drucker anlegen (z. B. `P1S-01` und `X1C-01`) mit unterschiedlicher `serial`.

3. Test-Payload für beide Drucker senden (PowerShell):

```powershell
$body = @{
  project = "private"
  source = "smoke-test"
  printers = @(
    @{
      printer = "P1S-01"
      serial = "SERIAL-P1S-01"
      telemetry = @{
        status = "online"
        job_name = "benchy.3mf"
        job_status = "RUNNING"
        progress = 37.5
        nozzle_temp = 220.0
        bed_temp = 60.0
        firmware = "01.08.00.00"
      }
      slots = @(
        @{ slot = 1; brand = "Bambu"; material = "PLA"; color = "Black" }
      )
    },
    @{
      printer = "X1C-01"
      serial = "SERIAL-X1C-01"
      telemetry = @{
        status = "online"
        job_name = "gear.3mf"
        job_status = "PAUSE"
        progress = 82.0
        nozzle_temp = 0
        bed_temp = 0
        firmware = "01.08.01.00"
      }
      slots = @(
        @{ slot = 2; brand = "Bambu"; material = "PETG"; color = "White" }
      )
    }
  )
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/slot-state/push?project=private&source=smoke-test" -ContentType "application/json" -Body $body
```

4. Erwartete Ergebnisse prüfen:

- `/printers`: beide Drucker sichtbar, `Last seen` gesetzt, Telemetrie/Status gefüllt.
- `/slot-status`: je Slot ein aktueller Ist-Eintrag vorhanden.
- Antwort von `/api/slot-state/push`: `ok=true`, `entries>=2`, `updated>=2`.

5. Optional: Bridge-Weg testen:

```powershell
$env:BAMBU_PRINTERS_JSON='[{"name":"P1S-01","host":"192.168.1.50","serial":"01S00XXXXXXXX","access_code":"12345678"}]'
python .\local_services\local_slot_bridge.py --endpoint "http://127.0.0.1:8000/api/slot-state/push" --project private --source local-slot-bridge
```
