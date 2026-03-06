# Projekt-Instruktionen

## Allgemeine Arbeitsweise

- Kommunikation kurz, klar und umsetzungsorientiert halten.
- Änderungen minimal-invasiv und entlang des bestehenden Stils umsetzen.
- Dokumentation bei relevanten Verhaltensänderungen immer mitziehen.

## Verbindliche Docker-only Regel

- Dieses Projekt wird ausschließlich über Docker Compose betrieben.
- App-Start, Migrationen, Tests und Betriebschecks erfolgen nur per docker compose.
- Lokale Python-/venv-/pip-Workflows sind kein unterstützter Betriebsweg.
- DATABASE_URL muss auf den Compose-PostgreSQL-Service db zeigen.
- SQLite und MySQL sind für den regulären Betrieb nicht erlaubt.
- Neue Doku, Automationen und Skripte müssen diese Regel einhalten.

## Verbindliche Rebuild/Restart-Regel nach Änderungen

- Nach jeder inhaltlichen Änderung am Projekt muss der Docker-Stack neu gebaut und gestartet werden.
- Standardablauf: `docker compose up -d --build` (mit Build-Cache, damit unveränderte Layer wie Dependencies nicht neu geladen werden).
- `--no-cache` nur gezielt verwenden, wenn ein Cache-Problem vermutet wird oder Abhängigkeiten bewusst vollständig neu aufgebaut werden sollen.
- Diese Pflicht gilt für Code-, Template-, Migrations-, Skript- und Konfigurationsänderungen.

## Verbindliche Soft-Refresh-Regel bei Nutzer-Speicheraktionen

- Beim Speichern von vom Nutzer eingegebenen Daten darf keine harte Seiten-Neuladung erfolgen.
- Speichern/Ändern in Formularen muss per Soft-Refresh/AJAX erfolgen, damit Layout und Kontext stabil bleiben.
- Voll-Reload ist nur erlaubt, wenn technisch unvermeidbar (z. B. Datei-Download/Browser-Navigation außerhalb des Daten-Speicherns).

## Verbindliche Refresh-Init-Regel für Seiten-Skripte

- Jede Seite mit eigenen JavaScript-Initialisierungen muss nach Soft-Refresh erneut korrekt aufgebaut werden.
- Dafür ist die Initialisierung in eine wiederverwendbare Funktion zu kapseln und zusätzlich auf `ui:after-soft-refresh` zu binden.
- Event-Handler müssen idempotent registriert werden (keine Mehrfach-Bindings bei wiederholten Refreshes).
- Diese Regel gilt für alle Templates mit `<script>`-Blöcken.

## Verbindliche Tabellen-Schema-Regel

- Tabellen müssen im einheitlichen UI-Schema aufgebaut sein: Wrapper mit `overflow-auto`/`overflow-x-auto`, Tabelle mit `ui-table`, Header-Zeile mit `ui-thead-row`, Header-Zellen mit `ui-th-sticky`, Datenzeilen mit `ui-row`, Datenzellen mit `ui-td`.
- Aktionsspalten müssen `ui-th-actions` und `ui-td-actions` verwenden.
- Abweichende Einzelklassen für Tabellenkopf/-zeilen sind nicht erlaubt, sofern keine technisch zwingende Ausnahme dokumentiert ist.

## Verbindliche Vollbreiten-Regel für Tabellen-Seiten

- Seiten/Container mit Datentabellen müssen die verfügbare Inhaltsbreite vollständig nutzen (`w-full`).
- Breitenbegrenzungen wie `max-w-*` auf Haupt-Containern von Tabellen-Seiten sind zu vermeiden.
- Tabellen selbst müssen über das bestehende `ui-table`-Schema auf volle Breite laufen; horizontales Scrollen wird nur über den Wrapper gelöst.

## Verbindliche Ausrichtungs-Regel für Tabellenwerte

- Tabellenwerte müssen innerhalb ihrer Spalte immer am Spaltenanfang stehen (links/start ausgerichtet).
- Rechts-/Endausrichtung (`text-right`, `justify-end`, `items-end`) ist in Tabellenköpfen und Tabellenzellen nicht erlaubt.
- Ausnahme Aktionsspalten: Spaltenkopf `ui-th-actions` und Zellen `ui-td-actions` sind rechtsbündig auszurichten, sodass Aktions-Buttons immer am rechten Tabellenende sitzen.


