from __future__ import annotations

import json
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.request

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.models import DeviceSlotState

try:
    import paho.mqtt.client as mqtt  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    mqtt = None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_text(value: Any, max_len: int) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized[:max_len]


def _normalize_slot(value: Any) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = int(float(raw))
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _load_payload() -> Any:
    feed_file = str(os.getenv("SLOT_STATE_FEED_FILE", "")).strip()
    if feed_file:
        path = Path(feed_file)
        if not path.exists() or not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    feed_url = str(os.getenv("SLOT_STATE_FEED_URL", "")).strip()
    if not feed_url:
        return None

    headers = {"Accept": "application/json"}
    token = str(os.getenv("SLOT_STATE_FEED_TOKEN", "")).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(feed_url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read().decode("utf-8", errors="replace")
        return json.loads(payload) if payload else None


def _iter_ams_objects(obj: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).strip().lower() == "ams":
                found.append(value)
            found.extend(_iter_ams_objects(value))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_iter_ams_objects(value))
    return found


def _extract_trays(value: Any) -> list[dict[str, Any]]:
    trays: list[dict[str, Any]] = []
    if isinstance(value, dict):
        tray_value = value.get("tray")
        if isinstance(tray_value, list):
            trays.extend([row for row in tray_value if isinstance(row, dict)])
        for nested in value.values():
            trays.extend(_extract_trays(nested))
    elif isinstance(value, list):
        for item in value:
            trays.extend(_extract_trays(item))
    return trays


def _normalize_slot_id_from_tray(tray: dict[str, Any]) -> int | None:
    slot = _normalize_slot(tray.get("slot") or tray.get("slot_id") or tray.get("id") or tray.get("tray_id"))
    if slot is None:
        return None
    return slot if slot >= 1 else (slot + 1)


def _extract_bambu_slots(payload: Any) -> list[dict[str, Any]]:
    ams_objects = _iter_ams_objects(payload)
    trays: list[dict[str, Any]] = []
    for ams in ams_objects:
        trays.extend(_extract_trays(ams))

    by_slot: dict[int, dict[str, Any]] = {}
    for tray in trays:
        slot = _normalize_slot_id_from_tray(tray)
        if slot is None:
            continue

        brand = _normalize_text(tray.get("brand") or tray.get("tray_sub_brands"), 120)
        material = _normalize_text(tray.get("material") or tray.get("filament_type") or tray.get("tray_type"), 80)
        color = _normalize_text(tray.get("color") or tray.get("tray_color"), 80)

        by_slot[slot] = {
            "slot": slot,
            "brand": brand,
            "material": material,
            "color": color,
        }

    return [by_slot[key] for key in sorted(by_slot.keys())]


def _parse_bambu_printers_env() -> list[dict[str, Any]]:
    raw = str(os.getenv("BAMBU_PRINTERS_JSON", "[]")).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = _normalize_text(item.get("name"), 120)
        host = _normalize_text(item.get("host"), 255)
        serial = _normalize_text(item.get("serial"), 120)
        access_code = _normalize_text(item.get("access_code"), 120)
        if not name or not host or not serial or not access_code:
            continue
        result.append(
            {
                "name": name,
                "host": host,
                "serial": serial,
                "access_code": access_code,
                "port": int(item.get("port") or 8883),
            }
        )
    return result


def _load_payload_bambu_mqtt() -> Any:
    if mqtt is None:
        raise RuntimeError("paho-mqtt not installed")

    printers = _parse_bambu_printers_env()
    if not printers:
        return None

    timeout_raw = str(os.getenv("SLOT_STATE_BAMBU_TIMEOUT_SEC", "10")).strip()
    try:
        timeout_sec = max(3, int(float(timeout_raw)))
    except ValueError:
        timeout_sec = 10

    printer_rows: list[dict[str, Any]] = []

    for printer in printers:
        message_holder: dict[str, Any] = {"payload": None}

        def on_connect(client, _userdata, _flags, rc):
            if rc == 0:
                topic = f"device/{printer['serial']}/report"
                client.subscribe(topic)

        def on_message(_client, _userdata, msg):
            try:
                raw_text = msg.payload.decode("utf-8", errors="replace")
                message_holder["payload"] = json.loads(raw_text)
            except Exception:
                message_holder["payload"] = None

        client = mqtt.Client(client_id=f"slot-poller-{printer['serial']}")
        client.username_pw_set("bblp", printer["access_code"])
        client.on_connect = on_connect
        client.on_message = on_message
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)

        try:
            client.connect(printer["host"], printer["port"], keepalive=20)
            client.loop_start()
            started = time.time()
            while time.time() - started < timeout_sec:
                payload = message_holder.get("payload")
                if payload is not None:
                    slots = _extract_bambu_slots(payload)
                    if slots:
                        printer_rows.append({"printer": printer["name"], "slots": slots})
                        break
                time.sleep(0.2)
        finally:
            try:
                client.loop_stop()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass

    if not printer_rows:
        return None

    return {"printers": printer_rows}


def _extract_entries(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []

    blocks: list[Any]
    if isinstance(payload, dict) and isinstance(payload.get("printers"), list):
        blocks = payload.get("printers", [])
    elif isinstance(payload, list):
        blocks = payload
    elif isinstance(payload, dict):
        blocks = [payload]
    else:
        return []

    entries: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue

        printer_name = _normalize_text(block.get("printer") or block.get("printer_name"), 120)
        slots = block.get("slots")
        if not printer_name or not isinstance(slots, list):
            continue

        for row in slots:
            if not isinstance(row, dict):
                continue
            slot = _normalize_slot(row.get("slot") or row.get("slot_id"))
            if slot is None:
                continue
            entries.append(
                {
                    "printer_name": printer_name,
                    "slot": slot,
                    "observed_brand": _normalize_text(row.get("brand"), 120),
                    "observed_material": _normalize_text(row.get("material"), 80),
                    "observed_color": _normalize_text(row.get("color"), 80),
                }
            )

    return entries


def _upsert_entries(project: str, source: str | None, entries: list[dict[str, Any]]) -> int:
    if not entries:
        return 0

    now = _utcnow_naive()
    count = 0
    with SessionLocal() as db:
        for entry in entries:
            state = (
                db.query(DeviceSlotState)
                .filter(
                    DeviceSlotState.project == project,
                    DeviceSlotState.printer_name == entry["printer_name"],
                    DeviceSlotState.slot == entry["slot"],
                )
                .first()
            )
            if state is None:
                state = DeviceSlotState(
                    project=project,
                    printer_name=entry["printer_name"],
                    slot=entry["slot"],
                )
                db.add(state)

            state.observed_brand = entry.get("observed_brand")
            state.observed_material = entry.get("observed_material")
            state.observed_color = entry.get("observed_color")
            state.source = source
            state.observed_at = now
            state.updated_at = now
            count += 1

        db.commit()
    return count


def main() -> int:
    interval_raw = str(os.getenv("SLOT_STATE_POLL_INTERVAL_SEC", "45")).strip()
    try:
        interval = max(5, int(float(interval_raw)))
    except ValueError:
        interval = 45

    project = _normalize_text(os.getenv("SLOT_STATE_PROJECT") or "private", 40) or "private"
    source = _normalize_text(os.getenv("SLOT_STATE_SOURCE") or "slot-poller", 120)
    provider = _normalize_text(os.getenv("SLOT_STATE_PROVIDER") or "feed", 40) or "feed"

    print(f"[slot-poller] started project={project} interval={interval}s provider={provider}")

    while True:
        try:
            if provider == "bambu_mqtt":
                payload = _load_payload_bambu_mqtt()
            else:
                payload = _load_payload()
            entries = _extract_entries(payload)
            updated = _upsert_entries(project=project, source=source, entries=entries)
            if entries:
                print(f"[slot-poller] upserted {updated} slot states")
            else:
                print("[slot-poller] no slot data available")
        except urllib.error.URLError as error:
            print(f"[slot-poller] fetch failed: {error}")
        except Exception as error:
            print(f"[slot-poller] unexpected error: {error}")

        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
