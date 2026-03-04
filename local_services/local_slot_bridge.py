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
    for key, zero_based in (("slot", False), ("slot_id", False), ("tray_id", True), ("id", True)):
        raw = tray.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            parsed = int(float(text))
        except ValueError:
            continue
        if zero_based:
            if parsed < 0:
                continue
            return parsed + 1
        if parsed > 0:
            return parsed
    return None


def _normalize_ams_unit_id(value: Any) -> int | None:
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


def _compose_global_slot(ams_unit: int | None, slot_local: int | None) -> int | None:
    if slot_local is None:
        return None
    if ams_unit is None or ams_unit <= 1:
        return slot_local
    return (ams_unit * 100) + slot_local


_AMS_ID_NAME_FALLBACK = {
    0: "HT-A",
    128: "HT-B",
    129: "HT-C",
    130: "HT-D",
}


def _fallback_ams_name(ams_unit: int | None, raw_ams_id: int | None) -> str | None:
    if raw_ams_id is not None and raw_ams_id in _AMS_ID_NAME_FALLBACK:
        return _AMS_ID_NAME_FALLBACK[raw_ams_id]
    if ams_unit is not None and ams_unit > 0 and ams_unit <= 26:
        return f"HT-{chr(ord('A') + ams_unit - 1)}"
    if ams_unit is not None and ams_unit > 0:
        return f"AMS {ams_unit}"
    return None


def _extract_bambu_ams_units(payload: Any) -> list[dict[str, Any]]:
    report = payload.get("print") if isinstance(payload, dict) and isinstance(payload.get("print"), dict) else payload
    ams_root = report.get("ams") if isinstance(report, dict) else None

    units: list[dict[str, Any]] = []
    if isinstance(ams_root, list):
        for index, candidate in enumerate([item for item in ams_root if isinstance(item, dict)], start=1):
            trays = _extract_trays(candidate)
            if not trays:
                continue
            raw_ams_id = _normalize_ams_unit_id(
                candidate.get("id")
                or candidate.get("ams_id")
                or candidate.get("index")
                or candidate.get("unit")
            )
            ams_unit = index
            ams_name = _normalize_text(candidate.get("name") or candidate.get("ams_name") or candidate.get("sn"), 120)
            if not ams_name:
                ams_name = _fallback_ams_name(ams_unit, raw_ams_id)
            units.append({"ams_unit": ams_unit, "ams_name": ams_name, "trays": trays})
    elif isinstance(ams_root, dict):
        raw_units = ams_root.get("ams")
        candidates: list[dict[str, Any]] = []
        if isinstance(raw_units, list):
            candidates = [item for item in raw_units if isinstance(item, dict)]
        elif isinstance(raw_units, dict):
            candidates = [item for item in raw_units.values() if isinstance(item, dict)]

        if not candidates and isinstance(ams_root.get("tray"), list):
            candidates = [ams_root]

        for index, candidate in enumerate(candidates, start=1):
            trays = _extract_trays(candidate)
            if not trays:
                continue
            raw_ams_id = _normalize_ams_unit_id(
                candidate.get("id")
                or candidate.get("ams_id")
                or candidate.get("index")
                or candidate.get("unit")
            )
            ams_unit = index
            ams_name = _normalize_text(candidate.get("name") or candidate.get("ams_name") or candidate.get("sn"), 120)
            if not ams_name:
                ams_name = _fallback_ams_name(ams_unit, raw_ams_id)
            units.append({"ams_unit": ams_unit, "ams_name": ams_name, "trays": trays})

    if units:
        return units

    trays: list[dict[str, Any]] = []
    for ams in _iter_ams_objects(payload):
        trays.extend(_extract_trays(ams))
    if not trays:
        return []
    return [{"ams_unit": 1, "ams_name": None, "trays": trays}]


def _normalize_bambu_brand(tray: dict[str, Any]) -> str | None:
    brand = _normalize_text(tray.get("brand") or tray.get("tray_sub_brands"), 120)
    if brand:
        return brand

    info_idx = (_normalize_text(tray.get("tray_info_idx"), 32) or "").upper()
    if info_idx.startswith("GFL"):
        return "Generic"
    if info_idx.startswith("GFA"):
        return "Bambu Lab"
    return None


def _normalize_bambu_color(value: Any) -> str | None:
    raw = _normalize_text(value, 80)
    if not raw:
        return None

    normalized = str(raw).strip().upper().lstrip("#")
    if len(normalized) == 8:
        normalized = normalized[:6]

    color_map = {
        "0ACC38": "Green",
        "F72323": "Red",
        "2850E0": "Blue",
        "000000": "Black",
        "FFFFFF": "White",
        "808080": "Gray",
    }
    if len(normalized) == 6 and normalized in color_map:
        return color_map[normalized]
    if len(normalized) == 6:
        try:
            r = int(normalized[0:2], 16)
            g = int(normalized[2:4], 16)
            b = int(normalized[4:6], 16)
        except ValueError:
            return f"#{normalized}"

        if max(r, g, b) - min(r, g, b) <= 12:
            if r <= 40:
                return "Black"
            if r >= 220:
                return "White"
            return "Gray"
        if r >= g + 40 and r >= b + 40:
            return "Red"
        if g >= r + 40 and g >= b + 40:
            return "Green"
        if b >= r + 40 and b >= g + 40:
            return "Blue"
        return f"#{normalized}"
    return raw


def _extract_bambu_slots(payload: Any) -> list[dict[str, Any]]:
    ams_units = _extract_bambu_ams_units(payload)

    by_slot: dict[tuple[int, int], dict[str, Any]] = {}
    for unit in ams_units:
        ams_unit = _normalize_ams_unit_id(unit.get("ams_unit")) or 1
        ams_name = _normalize_text(unit.get("ams_name"), 120)
        trays = unit.get("trays") if isinstance(unit.get("trays"), list) else []
        for tray_index, tray in enumerate(trays, start=1):
            if not isinstance(tray, dict):
                continue
            slot_local = _normalize_slot_id_from_tray(tray)
            if slot_local is None:
                slot_local = tray_index
            if slot_local is None:
                continue
            slot = _compose_global_slot(ams_unit, slot_local)
            if slot is None:
                continue

            by_slot[(ams_unit, slot_local)] = {
                "slot": slot,
                "slot_local": slot_local,
                "ams_unit": ams_unit,
                "ams_name": ams_name,
                "brand": _normalize_bambu_brand(tray),
                "material": _normalize_text(tray.get("material") or tray.get("filament_type") or tray.get("tray_type"), 80),
                "color": _normalize_bambu_color(tray.get("color") or tray.get("tray_color")),
            }

    ordered_keys = sorted(by_slot.keys(), key=lambda item: (item[0], item[1]))
    return [by_slot[key] for key in ordered_keys]


def _extract_bambu_ams_name_map(payload: Any) -> dict[int, str]:
    mapping: dict[int, str] = {}
    units = _extract_bambu_ams_units(payload)
    for unit in units:
        ams_unit = _normalize_slot(unit.get("ams_unit"))
        ams_name = _normalize_text(unit.get("ams_name"), 120)
        if ams_unit is None or not ams_name:
            continue
        mapping[ams_unit] = ams_name
    return mapping


def _merge_ams_names_into_slots(slots: list[dict[str, Any]], name_map: dict[int, str]) -> list[dict[str, Any]]:
    if not slots or not name_map:
        return slots
    merged: list[dict[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        item = dict(slot)
        if not _normalize_text(item.get("ams_name"), 120):
            ams_unit = _normalize_slot(item.get("ams_unit"))
            if ams_unit is not None and ams_unit in name_map:
                item["ams_name"] = name_map[ams_unit]
        merged.append(item)
    return merged


def _publish_bambu_refresh_requests(client: Any, serial: str) -> None:
    topic = f"device/{serial}/request"
    seq_base = int(time.time() * 1000)
    requests = [
        {"pushing": {"sequence_id": str(seq_base), "command": "pushall"}},
        {"system": {"sequence_id": str(seq_base + 1), "command": "get_version"}},
    ]
    for payload in requests:
        try:
            client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=False)
        except Exception:
            continue


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


def _extract_bambu_telemetry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"status": "unknown"}

    report = payload.get("print") if isinstance(payload.get("print"), dict) else payload
    status_raw = _normalize_text(report.get("gcode_state") if isinstance(report, dict) else None, 80)
    status = "online" if status_raw else "unknown"
    state_normalized = str(status_raw or "").strip().lower()
    print_running_states = {
        "running",
        "printing",
        "prepare",
        "preparing",
        "resume",
        "paused",
    }

    def _is_external_value(value: Any) -> bool:
        if value is None:
            return False
        try:
            return int(float(str(value).strip())) in {254, 255}
        except ValueError:
            return False

    external_spool_active = False
    marker_detected = False
    if isinstance(report, dict):
        direct_values = [
            report.get("tray_tar"),
            report.get("tray_now"),
            report.get("tray_pre"),
            report.get("vt_tray"),
        ]
        marker_detected = any(_is_external_value(value) for value in direct_values)

        if not marker_detected:
            ams_root = report.get("ams")
            candidates: list[dict[str, Any]] = []
            if isinstance(ams_root, dict):
                candidates.append(ams_root)
                nested = ams_root.get("ams")
                if isinstance(nested, list):
                    candidates.extend([item for item in nested if isinstance(item, dict)])
                elif isinstance(nested, dict):
                    candidates.extend([item for item in nested.values() if isinstance(item, dict)])
            elif isinstance(ams_root, list):
                candidates.extend([item for item in ams_root if isinstance(item, dict)])

            for candidate in candidates:
                if any(_is_external_value(candidate.get(key)) for key in ("tray_tar", "tray_now", "tray_pre", "vt_tray")):
                    marker_detected = True
                    break

    external_spool_active = marker_detected and state_normalized in print_running_states

    return {
        "status": status,
        "job_name": _normalize_text(report.get("subtask_name") if isinstance(report, dict) else None, 255),
        "job_status": status_raw,
        "progress": report.get("mc_percent") if isinstance(report, dict) else None,
        "nozzle_temp": report.get("nozzle_temper") if isinstance(report, dict) else None,
        "bed_temp": report.get("bed_temper") if isinstance(report, dict) else None,
        "chamber_temp": report.get("chamber_temper") if isinstance(report, dict) else None,
        "firmware": _normalize_text(report.get("sw_ver") if isinstance(report, dict) else None, 120),
        "error": _normalize_text(report.get("print_error") if isinstance(report, dict) else None, 255),
        "external_spool_active": external_spool_active,
    }


def _read_single_printer(printer: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    if mqtt is None:
        raise RuntimeError("paho-mqtt not installed")

    message_holder: dict[str, Any] = {"payload": None, "payloads": []}

    def on_connect(client, _userdata, _flags, rc):
        if rc == 0:
            topic = f"device/{printer['serial']}/report"
            client.subscribe(topic)
            _publish_bambu_refresh_requests(client, printer["serial"])

    def on_message(_client, _userdata, msg):
        try:
            raw_text = msg.payload.decode("utf-8", errors="replace")
            parsed_payload = json.loads(raw_text)
            message_holder["payload"] = parsed_payload
            payloads = message_holder.get("payloads")
            if isinstance(payloads, list):
                payloads.append(parsed_payload)
                if len(payloads) > 20:
                    del payloads[:-20]
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
                    ams_name_map: dict[int, str] = {}
                    for item in message_holder.get("payloads") or []:
                        if isinstance(item, dict):
                            ams_name_map.update(_extract_bambu_ams_name_map(item))
                    slots = _merge_ams_names_into_slots(slots, ams_name_map)
                    telemetry = _extract_bambu_telemetry(payload)
                    return {"slots": slots, "telemetry": telemetry}
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

    return {"slots": [], "telemetry": {"status": "offline"}}


def _collect_payload(printers: list[dict[str, Any]], timeout_sec: int) -> dict[str, Any]:
    printer_rows: list[dict[str, Any]] = []
    for printer in printers:
        result = _read_single_printer(printer, timeout_sec)
        slots = result.get("slots") if isinstance(result, dict) else []
        telemetry = result.get("telemetry") if isinstance(result, dict) else {"status": "unknown"}
        if slots:
            printer_rows.append(
                {
                    "printer": printer["name"],
                    "serial": printer.get("serial"),
                    "slots": slots,
                    "telemetry": telemetry,
                }
            )
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
