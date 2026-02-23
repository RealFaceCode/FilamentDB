from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import time
from typing import Any
import urllib.error
import urllib.request

try:
    import paho.mqtt.client as mqtt  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    mqtt = None


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

        by_slot[slot] = {
            "slot": slot,
            "brand": _normalize_text(tray.get("brand") or tray.get("tray_sub_brands"), 120),
            "material": _normalize_text(tray.get("material") or tray.get("filament_type") or tray.get("tray_type"), 80),
            "color": _normalize_text(tray.get("color") or tray.get("tray_color"), 80),
        }

    return [by_slot[key] for key in sorted(by_slot.keys())]


def _parse_printers_json(raw: str) -> list[dict[str, Any]]:
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


def _read_single_printer(printer: dict[str, Any], timeout_sec: int) -> list[dict[str, Any]]:
    if mqtt is None:
        raise RuntimeError("paho-mqtt not installed")

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

    client = mqtt.Client(client_id=f"local-slot-bridge-{printer['serial']}")
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
                    return slots
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

    return []


def _collect_payload(printers: list[dict[str, Any]], timeout_sec: int) -> dict[str, Any]:
    printer_rows: list[dict[str, Any]] = []
    for printer in printers:
        slots = _read_single_printer(printer, timeout_sec)
        if slots:
            printer_rows.append({"printer": printer["name"], "slots": slots})
    return {"printers": printer_rows}


def _post_payload(
    endpoint: str,
    payload: dict[str, Any],
    timeout_sec: int,
    auth_user: str | None,
    auth_password: str | None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if auth_user and auth_password:
        token = base64.b64encode(f"{auth_user}:{auth_password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=max(1, int(timeout_sec))) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read AMS slots locally and push them to Filament_Datenbank server")
    parser.add_argument(
        "--endpoint",
        default=os.getenv("SLOT_PUSH_ENDPOINT", ""),
        help="Server endpoint for pushed slot state",
    )
    parser.add_argument("--project", default=os.getenv("SLOT_PUSH_PROJECT", "private"), help="Project key")
    parser.add_argument("--source", default=os.getenv("SLOT_PUSH_SOURCE", "local-slot-bridge"), help="Source label")
    parser.add_argument(
        "--printers-json",
        default=os.getenv("BAMBU_PRINTERS_JSON", "[]"),
        help="JSON array with printers: [{name,host,serial,access_code,port?}]",
    )
    parser.add_argument("--interval", type=int, default=int(float(os.getenv("SLOT_PUSH_INTERVAL_SEC", "45"))), help="Poll interval seconds")
    parser.add_argument(
        "--mqtt-timeout",
        type=int,
        default=int(float(os.getenv("SLOT_PUSH_MQTT_TIMEOUT_SEC", "10"))),
        help="Max seconds per printer to wait for MQTT data",
    )
    parser.add_argument("--http-timeout", type=int, default=15, help="HTTP timeout seconds")
    parser.add_argument("--auth-user", default=os.getenv("SLOT_PUSH_AUTH_USER"), help="Optional HTTP Basic auth username")
    parser.add_argument("--auth-password", default=os.getenv("SLOT_PUSH_AUTH_PASSWORD"), help="Optional HTTP Basic auth password")
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    endpoint = str(args.endpoint or "").strip()
    if not endpoint:
        print("[local-slot-bridge] missing endpoint; set --endpoint or SLOT_PUSH_ENDPOINT")
        return 2

    printers = _parse_printers_json(args.printers_json)
    if not printers:
        print("[local-slot-bridge] no valid printers configured via --printers-json / BAMBU_PRINTERS_JSON")
        return 2

    interval = max(5, int(args.interval))
    mqtt_timeout = max(3, int(args.mqtt_timeout))

    print(f"[local-slot-bridge] started interval={interval}s printers={len(printers)} endpoint={endpoint}")

    while True:
        try:
            payload = _collect_payload(printers, mqtt_timeout)
            payload["project"] = args.project
            payload["source"] = args.source

            pushed = _post_payload(
                endpoint=endpoint,
                payload=payload,
                timeout_sec=args.http_timeout,
                auth_user=args.auth_user,
                auth_password=args.auth_password,
            )
            if pushed.get("ok"):
                print(f"[local-slot-bridge] pushed entries={pushed.get('entries', 0)} updated={pushed.get('updated', 0)}")
            else:
                print(f"[local-slot-bridge] push error payload={pushed}")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            print(f"[local-slot-bridge] HTTP {error.code}: {body}")
        except Exception as error:
            print(f"[local-slot-bridge] unexpected error: {error}")

        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
