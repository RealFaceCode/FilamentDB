# Local Services (Endnutzer-PC)

Dieser Ordner enthält nur Dienste/Skripte, die auf dem lokalen Nutzer-PC laufen.

## Enthalten

- `slicer_auto_usage.py`  
  Slicer-Postprocessing: sendet 3MF/GCode-Datei an den Server (`/api/usage/auto-from-file`).
- `bambu_studio_auto_usage.py`  
  Kleiner Wrapper für Bambu Studio.
- `local_slot_bridge.py`  
  Liest AMS/Slot-Status lokal per Bambu MQTT und pusht ihn zum Server (`/api/slot-state/push`).

## Trennung PC vs. Server

- **PC lokal (`local_services/`)**: Zugriff auf Drucker im LAN, Sammeln von Live-Daten.
- **Server (`app/`, `docker-compose.yml`)**: Persistenz, Business-Logik, UI, Auswertungen.

## Beispiel: Local Slot Bridge starten (Windows)

```powershell
$env:BAMBU_PRINTERS_JSON='[{"name":"P1S-01","host":"192.168.1.50","serial":"01S00XXXXXXXX","access_code":"12345678"}]'
python .\local_services\local_slot_bridge.py --endpoint "https://dein-server/api/slot-state/push" --project private --source local-slot-bridge --auth-user "admin" --auth-password "secret"
```

Hinweise:

- `--endpoint` (oder `SLOT_PUSH_ENDPOINT`) ist Pflicht und sollte auf deine laufende App zeigen.
- `paho-mqtt` muss installiert sein (z. B. über Projekt-`requirements.txt`).
- Für mehrere Drucker einfach mehrere Objekte in `BAMBU_PRINTERS_JSON` hinterlegen.
- Der Server-Endpoint akzeptiert Format:

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
