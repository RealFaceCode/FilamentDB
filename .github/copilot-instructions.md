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

## Verbindliche Externer-Server-Regel

- Design- und Architekturentscheidungen sind immer so zu treffen, dass der Betrieb auf einem externen Server zuverlässig funktioniert (z. B. Hostinger VPS).
- Lokale Dienste dürfen als Datenquelle dienen, aber die zentrale Laufzeit, Persistenz und Business-Logik liegt auf dem externen Server.
- Neue Features müssen ohne lokalen Sonderbetrieb auf dem Server deploybar und betreibbar sein.
- Infrastruktur- und Netzwerkannahmen müssen extern-server-tauglich sein (Reverse Proxy, TLS, öffentliche Erreichbarkeit der App, keine Abhängigkeit von lokalem LAN am Server).
- Bei Zielkonflikten gilt: Externer-Server-Betrieb hat Vorrang vor lokaler Bequemlichkeit.
