# Slicer Hooks

Dieser Ordner enthält lokale Hook-Skripte für Slicer-Postprocessing.

## Enthaltene Skripte

- `send_filament_usage.cmd` = Hauptskript (universell, robuste Argument-Erkennung)
- `send_filament_usage_bambu.cmd` = Wrapper für Bambu Studio
- `send_filament_usage_prusa_orca_superslicer.cmd` = Wrapper für PrusaSlicer / OrcaSlicer / SuperSlicer
- `send_filament_usage_cura_creality.cmd` = Wrapper für Cura / Creality Print

Alle Wrapper rufen intern das Hauptskript auf.

## Hersteller / Slicer Zuordnung (gängig)

- **Bambu Lab** → Bambu Studio: `C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_bambu.cmd`
- **Prusa** → PrusaSlicer: `C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_prusa_orca_superslicer.cmd`
- **OrcaSlicer-Nutzer** (z. B. Anycubic, Elegoo, QIDI, Voron, Custom) → OrcaSlicer: `C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_prusa_orca_superslicer.cmd`
- **Creality** → Creality Print (bzw. Cura-basiert): `C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_cura_creality.cmd`
- **Ultimaker / viele Drittanbieter** → Cura: `C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_cura_creality.cmd`

Wenn dein Hersteller nicht explizit aufgeführt ist, nimm den Slicer-Pfad, den du tatsächlich verwendest.

## Quick Setup (Copy/Paste)

### Bambu Studio

1. **Process** öffnen
2. **Others** öffnen
3. **Post-processing scripts**
4. Eintragen:

`C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_bambu.cmd`

### PrusaSlicer / OrcaSlicer / SuperSlicer

1. **Print Settings** öffnen
2. **Output options** öffnen
3. **Post-processing scripts**
4. Eintragen:

`C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_prusa_orca_superslicer.cmd`

### Cura / Creality Print

1. Post-Processing-Plugin/Funktion öffnen
2. Script/Command-Feld öffnen
3. Eintragen:

`C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_cura_creality.cmd`

Hinweis: `send_filament_usage.cmd` bleibt das zentrale Hauptskript. Alle anderen `.cmd`-Dateien sind nur Wrapper mit slicerfreundlichen Namen.

## `send_filament_usage.cmd`

Windows-Postprocessing-Hook für Bambu Studio / PrusaSlicer / OrcaSlicer.
Er sendet die erzeugte Druckdatei (`.3mf`, `.gcode`, `.gco`, `.bgcode`) an:

- `POST /api/usage/auto-from-file`

### Nutzung in Bambu Studio

Im Feld **Post-processing scripts** den absoluten Pfad eintragen, z. B.:

`C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_bambu.cmd`

Bambu Studio übergibt die erzeugte Datei automatisch als erstes Argument.

### Nutzung in PrusaSlicer / OrcaSlicer / SuperSlicer

Im Feld **Post-processing scripts** den absoluten Pfad eintragen, z. B.:

`C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_prusa_orca_superslicer.cmd`

### Nutzung in Cura / Creality Print

Trage den Scriptpfad im jeweiligen Post-Processing-Workflow ein:

`C:\dev\Filament_Datenbank\slicer_hooks\send_filament_usage_cura_creality.cmd`

### Wichtige Variablen im Script

- `URL`: Server-Endpoint (aktuell LAN-HTTPS)
- `PROJECT`: `private` oder `business`
- `DRYRUN`: `1` = Testlauf ohne Abbuchung, `0` = echte Abbuchung
- `AUTH`: leer lassen oder `user:password` setzen (bei Basic Auth)
- `KEEP_OPEN`: `0` = Fenster schließt sofort, `1` = Fenster bleibt am Ende offen

### Hinweise

- Das Script nutzt `curl.exe` (in Windows enthalten).
- `-k` ist aktiv, damit interne LAN-Zertifikate akzeptiert werden.
  Wenn das Zertifikat im Truststore sauber installiert ist, kann `-k` entfernt werden.
- Wenn Bambu Studio eine Datei aus `...\Metadata\...` übergibt, versucht das Script automatisch die passende Hauptdatei (`.3mf/.bgcode/.gcode/.gco`) im Job-Ordner zu finden und zu verwenden.
- Wenn die API mit `error=no_grams` antwortet, versucht das Script automatisch einen Retry mit einer nahegelegenen `.3mf`-Datei.
- Das Hauptskript akzeptiert mehrere Argumente und nimmt automatisch das erste existierende Dateiargument. Das hilft bei unterschiedlichen Slicer-Argumentmustern.
