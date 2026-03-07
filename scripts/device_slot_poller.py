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
from app.models import DeviceSlotState, Printer

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


def _normalize_float(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _normalize_percentage(value: Any) -> float | None:
    parsed = _normalize_float(value)
    if parsed is None:
        return None
    if 0 <= parsed <= 1:
        parsed = parsed * 100
    return max(0.0, min(100.0, parsed))


def _normalize_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if float(value) == 1:
            return True
        if float(value) == 0:
            return False
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on", "active"}:
        return True
    if normalized in {"0", "false", "no", "off", "inactive"}:
        return False
    return None


def _normalize_online_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"printing", "running", "operational", "ready", "idle", "paused", "online", "complete", "finished", "standby"}:
        return "online"
    if normalized in {"offline", "disconnected", "error", "failed", "fault", "unknown"}:
        return "offline"
    return "unknown"


def _build_url(base_url: str, path: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    suffix = str(path or "").strip()
    if not suffix:
        return base
    if not suffix.startswith("/"):
        suffix = f"/{suffix}"
    return f"{base}{suffix}"


def _http_get_json(url: str, headers: dict[str, str], timeout_sec: int) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=max(1, timeout_sec)) as response:
        payload = response.read().decode("utf-8", errors="replace")
        return json.loads(payload) if payload else None


def _parse_multi_brand_printers_env() -> list[dict[str, Any]]:
    raw = str(os.getenv("MULTIBRAND_PRINTERS_JSON", "[]")).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    brand_default_adapter = {
        "creality": "moonraker",
        "prusa": "prusalink",
        "bambu": "bambu_mqtt",
        "qidi": "moonraker",
        "elegoo": "moonraker",
        "sovol": "moonraker",
        "voron": "moonraker",
        "anycubic": "octoprint",
        "ender": "octoprint",
        "artillery": "octoprint",
    }

    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = _normalize_text(item.get("name"), 120)
        serial = _normalize_text(item.get("serial"), 120)
        brand = _normalize_text(item.get("brand"), 40)
        adapter = _normalize_text(item.get("adapter"), 40)
        base_url = _normalize_text(item.get("base_url") or item.get("url") or item.get("host"), 255)
        api_key = _normalize_text(item.get("api_key") or item.get("token"), 255)
        timeout_sec = int(_normalize_float(item.get("timeout_sec")) or 10)

        if not adapter and brand:
            adapter = brand_default_adapter.get(brand.lower())
        if not adapter:
            adapter = "octoprint"

        result.append(
            {
                "name": name,
                "serial": serial,
                "brand": brand,
                "adapter": adapter.lower(),
                "base_url": base_url,
                "api_key": api_key,
                "timeout_sec": max(3, timeout_sec),
            }
        )

    return result


def _build_printer_block(
    printer: dict[str, Any],
    *,
    telemetry: dict[str, Any] | None,
    slots: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    printer_name = _normalize_text(printer.get("name"), 120)
    printer_serial = _normalize_text(printer.get("serial"), 120)
    if not printer_name and not printer_serial:
        return None
    block = {
        "printer": printer_name,
        "serial": printer_serial,
        "telemetry": telemetry or {"status": "unknown"},
        "slots": slots or [],
    }
    return block


def _adapter_octoprint(printer: dict[str, Any]) -> dict[str, Any] | None:
    base_url = _normalize_text(printer.get("base_url"), 255)
    if not base_url:
        return None
    timeout_sec = int(printer.get("timeout_sec") or 10)
    headers = {"Accept": "application/json"}
    api_key = _normalize_text(printer.get("api_key"), 255)
    if api_key:
        headers["X-Api-Key"] = api_key

    job_data = _http_get_json(_build_url(base_url, "/api/job"), headers, timeout_sec) or {}
    printer_data = _http_get_json(_build_url(base_url, "/api/printer"), headers, timeout_sec) or {}

    state_raw = _normalize_text(job_data.get("state") if isinstance(job_data, dict) else None, 80)
    progress = None
    if isinstance(job_data, dict) and isinstance(job_data.get("progress"), dict):
        progress = _normalize_percentage(job_data["progress"].get("completion"))
    job_name = None
    if isinstance(job_data, dict) and isinstance(job_data.get("job"), dict):
        file_info = job_data["job"].get("file")
        if isinstance(file_info, dict):
            job_name = _normalize_text(file_info.get("display") or file_info.get("name"), 255)

    nozzle_temp = None
    bed_temp = None
    if isinstance(printer_data, dict) and isinstance(printer_data.get("temperature"), dict):
        temperature = printer_data.get("temperature")
        if isinstance(temperature.get("tool0"), dict):
            nozzle_temp = _normalize_float(temperature["tool0"].get("actual"))
        if isinstance(temperature.get("bed"), dict):
            bed_temp = _normalize_float(temperature["bed"].get("actual"))

    telemetry = {
        "status": _normalize_online_status(state_raw),
        "job_name": job_name,
        "job_status": state_raw,
        "progress": progress,
        "nozzle_temp": nozzle_temp,
        "bed_temp": bed_temp,
        "chamber_temp": None,
        "firmware": None,
        "error": None,
    }
    return _build_printer_block(printer, telemetry=telemetry, slots=[])


def _adapter_moonraker(printer: dict[str, Any]) -> dict[str, Any] | None:
    base_url = _normalize_text(printer.get("base_url"), 255)
    if not base_url:
        return None
    timeout_sec = int(printer.get("timeout_sec") or 10)
    headers = {"Accept": "application/json"}
    api_key = _normalize_text(printer.get("api_key"), 255)
    if api_key:
        headers["X-Api-Key"] = api_key

    query = "/printer/objects/query?print_stats&extruder&heater_bed"
    data = _http_get_json(_build_url(base_url, query), headers, timeout_sec) or {}

    status_obj = None
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        status_obj = data["result"].get("status")
    if not isinstance(status_obj, dict):
        status_obj = {}

    print_stats = status_obj.get("print_stats") if isinstance(status_obj.get("print_stats"), dict) else {}
    extruder = status_obj.get("extruder") if isinstance(status_obj.get("extruder"), dict) else {}
    heater_bed = status_obj.get("heater_bed") if isinstance(status_obj.get("heater_bed"), dict) else {}

    state_raw = _normalize_text(print_stats.get("state"), 80)
    telemetry = {
        "status": _normalize_online_status(state_raw),
        "job_name": _normalize_text(print_stats.get("filename"), 255),
        "job_status": state_raw,
        "progress": _normalize_percentage(print_stats.get("progress")),
        "nozzle_temp": _normalize_float(extruder.get("temperature")),
        "bed_temp": _normalize_float(heater_bed.get("temperature")),
        "chamber_temp": None,
        "firmware": None,
        "error": None,
    }
    return _build_printer_block(printer, telemetry=telemetry, slots=[])


def _adapter_prusalink(printer: dict[str, Any]) -> dict[str, Any] | None:
    base_url = _normalize_text(printer.get("base_url"), 255)
    if not base_url:
        return None
    timeout_sec = int(printer.get("timeout_sec") or 10)
    headers = {"Accept": "application/json"}
    api_key = _normalize_text(printer.get("api_key"), 255)
    if api_key:
        headers["X-Api-Key"] = api_key

    status_data = _http_get_json(_build_url(base_url, "/api/v1/status"), headers, timeout_sec) or {}
    job_data = _http_get_json(_build_url(base_url, "/api/v1/job"), headers, timeout_sec) or {}

    state_raw = None
    nozzle_temp = None
    bed_temp = None
    if isinstance(status_data, dict):
        printer_state = status_data.get("printer") if isinstance(status_data.get("printer"), dict) else {}
        telemetry_obj = status_data.get("telemetry") if isinstance(status_data.get("telemetry"), dict) else {}
        state_raw = _normalize_text(printer_state.get("state") or status_data.get("state"), 80)
        nozzle_temp = _normalize_float(telemetry_obj.get("temp_nozzle") or telemetry_obj.get("nozzle"))
        bed_temp = _normalize_float(telemetry_obj.get("temp_bed") or telemetry_obj.get("bed"))

    job_name = None
    progress = None
    if isinstance(job_data, dict):
        progress = _normalize_percentage(job_data.get("progress") or job_data.get("completion"))
        file_obj = job_data.get("file") if isinstance(job_data.get("file"), dict) else {}
        job_name = _normalize_text(file_obj.get("display_name") or file_obj.get("name") or job_data.get("job_name"), 255)

    telemetry = {
        "status": _normalize_online_status(state_raw),
        "job_name": job_name,
        "job_status": state_raw,
        "progress": progress,
        "nozzle_temp": nozzle_temp,
        "bed_temp": bed_temp,
        "chamber_temp": None,
        "firmware": None,
        "error": None,
    }
    return _build_printer_block(printer, telemetry=telemetry, slots=[])


def _adapter_generic_http(printer: dict[str, Any]) -> dict[str, Any] | None:
    base_url = _normalize_text(printer.get("base_url"), 255)
    if not base_url:
        return None
    timeout_sec = int(printer.get("timeout_sec") or 10)
    headers = {"Accept": "application/json"}
    api_key = _normalize_text(printer.get("api_key"), 255)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = _http_get_json(base_url, headers, timeout_sec)
    if not isinstance(data, dict):
        return None

    telemetry = data.get("telemetry") if isinstance(data.get("telemetry"), dict) else {}
    slots = data.get("slots") if isinstance(data.get("slots"), list) else []
    for slot in slots:
        if isinstance(slot, dict):
            slot["slot"] = _normalize_slot(slot.get("slot") or slot.get("slot_id"))

    normalized_telemetry = {
        "status": _normalize_online_status(telemetry.get("status")),
        "job_name": _normalize_text(telemetry.get("job_name"), 255),
        "job_status": _normalize_text(telemetry.get("job_status"), 80),
        "progress": _normalize_percentage(telemetry.get("progress")),
        "nozzle_temp": _normalize_float(telemetry.get("nozzle_temp")),
        "bed_temp": _normalize_float(telemetry.get("bed_temp")),
        "chamber_temp": _normalize_float(telemetry.get("chamber_temp")),
        "firmware": _normalize_text(telemetry.get("firmware"), 120),
        "error": _normalize_text(telemetry.get("error"), 255),
    }
    return _build_printer_block(printer, telemetry=normalized_telemetry, slots=[slot for slot in slots if isinstance(slot, dict) and slot.get("slot")])


def _load_payload_multi_brand_http() -> Any:
    printers = _parse_multi_brand_printers_env()
    if not printers:
        return None

    adapter_map = {
        "octoprint": _adapter_octoprint,
        "moonraker": _adapter_moonraker,
        "klipper": _adapter_moonraker,
        "prusalink": _adapter_prusalink,
        "prusa": _adapter_prusalink,
        "creality": _adapter_moonraker,
        "generic_http": _adapter_generic_http,
    }

    rows: list[dict[str, Any]] = []
    for printer in printers:
        adapter_name = _normalize_text(printer.get("adapter"), 40) or "octoprint"
        adapter = adapter_map.get(adapter_name.lower())
        if adapter is None:
            continue
        try:
            row = adapter(printer)
        except Exception as error:
            name = printer.get("name") or printer.get("serial") or "unknown"
            print(f"[slot-poller] adapter '{adapter_name}' failed for {name}: {error}")
            row = None
        if isinstance(row, dict):
            rows.append(row)

    if not rows:
        return None
    return {"printers": rows}


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
    return parsed


def _first_present_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


_AMS_RAW_ID_TO_UNIT = {
    0: 1,
}


def _resolve_ams_unit(
    raw_ams_id: int | None,
    fallback_index: int | None = None,
    *,
    zero_based_hint: bool = False,
) -> int | None:
    if raw_ams_id is not None:
        if raw_ams_id in _AMS_RAW_ID_TO_UNIT:
            return _AMS_RAW_ID_TO_UNIT[raw_ams_id]
        if zero_based_hint and 0 <= raw_ams_id <= 25:
            return raw_ams_id + 1
        if 1 <= raw_ams_id <= 26:
            return raw_ams_id
        if 0 <= raw_ams_id <= 25:
            return raw_ams_id + 1
    if fallback_index is not None and fallback_index > 0:
        return fallback_index
    return None


def _build_ams_unit_map(candidates: list[dict[str, Any]]) -> dict[int, int]:
    """Build a stable raw-id -> logical AMS unit mapping for mixed Bambu payloads.

    Some payloads mix zero-based ids (0,1,2,...) with high ids (128,129,...).
    We first map the sequential low ids, then append high ids as extra units.
    """
    raw_ids: list[int] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_id = _normalize_ams_unit_id(
            _first_present_value(candidate.get("id"), candidate.get("ams_id"), candidate.get("index"), candidate.get("unit"))
        )
        if raw_id is None:
            continue
        raw_ids.append(raw_id)

    unique_raw = sorted(set(raw_ids))
    if not unique_raw:
        return {}

    mapping: dict[int, int] = {}
    used_units: set[int] = set()

    has_zero_based = any(raw == 0 for raw in unique_raw)
    if has_zero_based:
        for raw in sorted(raw for raw in unique_raw if 0 <= raw < 128):
            unit = raw + 1
            if unit not in used_units:
                mapping[raw] = unit
                used_units.add(unit)
    else:
        for raw in sorted(raw for raw in unique_raw if 1 <= raw <= 26):
            if raw not in used_units:
                mapping[raw] = raw
                used_units.add(raw)

    next_unit = (max(used_units) + 1) if used_units else 1
    for raw in sorted(raw for raw in unique_raw if raw >= 128):
        while next_unit in used_units:
            next_unit += 1
        mapping[raw] = next_unit
        used_units.add(next_unit)
        next_unit += 1

    for raw in unique_raw:
        if raw in mapping:
            continue
        unit = _resolve_ams_unit(raw, zero_based_hint=has_zero_based)
        if unit is not None and unit not in used_units:
            mapping[raw] = unit
            used_units.add(unit)
            continue
        while next_unit in used_units:
            next_unit += 1
        mapping[raw] = next_unit
        used_units.add(next_unit)
        next_unit += 1

    return mapping


def _build_high_raw_id_label_map(candidates: list[dict[str, Any]]) -> dict[int, str]:
    """Map high raw ids (128+) to human-friendly HT labels in order."""
    raw_ids: list[int] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_id = _normalize_ams_unit_id(
            _first_present_value(candidate.get("id"), candidate.get("ams_id"), candidate.get("index"), candidate.get("unit"))
        )
        if raw_id is None or raw_id < 128:
            continue
        raw_ids.append(raw_id)

    labels: dict[int, str] = {}
    for index, raw_id in enumerate(sorted(set(raw_ids)), start=1):
        if 1 <= index <= 26:
            labels[raw_id] = f"HT-{chr(ord('A') + index - 1)}"
        else:
            labels[raw_id] = f"HT-{index}"
    return labels


def _compose_global_slot(ams_unit: int | None, slot_local: int | None) -> int | None:
    if slot_local is None:
        return None
    if ams_unit is None or ams_unit <= 0:
        ams_unit = 1
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


def _serial_fallback_unit_map(candidates: list[dict[str, Any]]) -> dict[str, int]:
    serials: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        serial = _normalize_text(candidate.get("sn"), 120)
        if serial:
            serials.append(serial)
    unique_sorted = sorted(set(serials), key=lambda value: value.lower())
    return {serial: index for index, serial in enumerate(unique_sorted, start=1)}


def _extract_bambu_ams_units(payload: Any) -> list[dict[str, Any]]:
    report = payload.get("print") if isinstance(payload, dict) and isinstance(payload.get("print"), dict) else payload
    ams_root = report.get("ams") if isinstance(report, dict) else None

    units: list[dict[str, Any]] = []

    def _collapse_duplicate_units(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_unit: dict[int, dict[str, Any]] = {}
        passthrough: list[dict[str, Any]] = []
        for row in rows:
            ams_unit = _normalize_ams_unit_id(row.get("ams_unit"))
            trays = row.get("trays") if isinstance(row.get("trays"), list) else []
            if ams_unit is None:
                passthrough.append(row)
                continue

            current = by_unit.get(ams_unit)
            if current is None:
                by_unit[ams_unit] = row
                continue

            current_trays = current.get("trays") if isinstance(current.get("trays"), list) else []
            current_name = _normalize_text(current.get("ams_name"), 120)
            next_name = _normalize_text(row.get("ams_name"), 120)

            should_replace = len(trays) > len(current_trays)
            if len(trays) == len(current_trays) and next_name and not current_name:
                should_replace = True
            if should_replace:
                by_unit[ams_unit] = row

        collapsed = sorted(by_unit.values(), key=lambda item: _normalize_ams_unit_id(item.get("ams_unit")) or 999)
        collapsed.extend(passthrough)
        return collapsed
    if isinstance(ams_root, list):
        candidates = [item for item in ams_root if isinstance(item, dict)]
        raw_ids = {
            _normalize_ams_unit_id(
                _first_present_value(candidate.get("id"), candidate.get("ams_id"), candidate.get("index"), candidate.get("unit"))
            )
            for candidate in candidates
        }
        zero_based_hint = 0 in raw_ids
        raw_to_unit = _build_ams_unit_map(candidates)
        high_label_map = _build_high_raw_id_label_map(candidates)
        serial_unit_map = _serial_fallback_unit_map(candidates)
        for index, candidate in enumerate(candidates, start=1):
            trays = _extract_trays(candidate)
            if not trays:
                continue
            candidate_serial = _normalize_text(candidate.get("sn"), 120)
            raw_ams_id = _normalize_ams_unit_id(
                _first_present_value(
                    candidate.get("id"),
                    candidate.get("ams_id"),
                    candidate.get("index"),
                    candidate.get("unit"),
                )
            )
            serial_fallback = serial_unit_map.get(candidate_serial) if candidate_serial else None
            ams_unit = raw_to_unit.get(raw_ams_id) if raw_ams_id is not None else None
            if ams_unit is None:
                ams_unit = _resolve_ams_unit(raw_ams_id, serial_fallback or index, zero_based_hint=zero_based_hint)
            ams_name = _normalize_text(candidate.get("name") or candidate.get("ams_name") or candidate.get("sn"), 120)
            if not ams_name:
                if raw_ams_id is not None and raw_ams_id in high_label_map:
                    ams_name = high_label_map[raw_ams_id]
                else:
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

        raw_ids = {
            _normalize_ams_unit_id(
                _first_present_value(candidate.get("id"), candidate.get("ams_id"), candidate.get("index"), candidate.get("unit"))
            )
            for candidate in candidates
        }
        zero_based_hint = 0 in raw_ids
        raw_to_unit = _build_ams_unit_map(candidates)
        high_label_map = _build_high_raw_id_label_map(candidates)

        serial_unit_map = _serial_fallback_unit_map(candidates)

        for index, candidate in enumerate(candidates, start=1):
            trays = _extract_trays(candidate)
            if not trays:
                continue
            candidate_serial = _normalize_text(candidate.get("sn"), 120)
            raw_ams_id = _normalize_ams_unit_id(
                _first_present_value(
                    candidate.get("id"),
                    candidate.get("ams_id"),
                    candidate.get("index"),
                    candidate.get("unit"),
                )
            )
            serial_fallback = serial_unit_map.get(candidate_serial) if candidate_serial else None
            ams_unit = raw_to_unit.get(raw_ams_id) if raw_ams_id is not None else None
            if ams_unit is None:
                ams_unit = _resolve_ams_unit(raw_ams_id, serial_fallback or index, zero_based_hint=zero_based_hint)
            ams_name = _normalize_text(candidate.get("name") or candidate.get("ams_name") or candidate.get("sn"), 120)
            if not ams_name:
                if raw_ams_id is not None and raw_ams_id in high_label_map:
                    ams_name = high_label_map[raw_ams_id]
                else:
                    ams_name = _fallback_ams_name(ams_unit, raw_ams_id)
            units.append({"ams_unit": ams_unit, "ams_name": ams_name, "trays": trays})

    if units:
        return _collapse_duplicate_units(units)

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

            brand = _normalize_bambu_brand(tray)
            material = _normalize_text(tray.get("material") or tray.get("filament_type") or tray.get("tray_type"), 80)
            color = _normalize_bambu_color(tray.get("color") or tray.get("tray_color"))

            by_slot[(ams_unit, slot_local)] = {
                "slot": slot,
                "slot_local": slot_local,
                "ams_unit": ams_unit,
                "ams_name": ams_name,
                "brand": brand,
                "material": material,
                "color": color,
                "remain_pct": _normalize_percentage(tray.get("remain")),
                "tray_weight_g": _normalize_float(tray.get("tray_weight")),
            }

    ordered_keys = sorted(by_slot.keys(), key=lambda item: (item[0], item[1]))
    return [by_slot[key] for key in ordered_keys]


def _extract_bambu_ams_name_map(payload: Any) -> dict[int, str]:
    mapping: dict[int, str] = {}
    units = _extract_bambu_ams_units(payload)
    for unit in units:
        ams_unit = _normalize_ams_unit_id(unit.get("ams_unit"))
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


def _score_bambu_slots(slots: list[dict[str, Any]]) -> tuple[int, int]:
    """Prefer snapshots with more AMS units first, then more total slots."""
    ams_units: set[int] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        ams_unit = _normalize_ams_unit_id(slot.get("ams_unit"))
        if ams_unit is not None:
            ams_units.add(ams_unit)
    return (len(ams_units), len(slots))


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


def _load_bambu_printers_from_db() -> list[dict[str, Any]]:
    project = _normalize_text(os.getenv("SLOT_STATE_PROJECT") or "private", 40) or "private"
    rows: list[dict[str, Any]] = []

    with SessionLocal() as db:
        printers = (
            db.query(Printer)
            .filter(Printer.project == project)
            .filter(Printer.is_active.is_(True))
            .all()
        )

        for printer in printers:
            name = _normalize_text(printer.name, 120)
            host = _normalize_text(printer.host, 255)
            serial = _normalize_text(printer.serial, 120)
            access_code = _normalize_text(printer.access_code, 120)
            if not name or not host or not serial or not access_code:
                continue
            rows.append(
                {
                    "name": name,
                    "host": host,
                    "serial": serial,
                    "access_code": access_code,
                    "port": int(printer.port or 8883),
                }
            )

    return rows


def _load_payload_bambu_mqtt() -> Any:
    if mqtt is None:
        raise RuntimeError("paho-mqtt not installed")

    printers = _parse_bambu_printers_env()
    if not printers:
        printers = _load_bambu_printers_from_db()
    if not printers:
        return None

    timeout_raw = str(os.getenv("SLOT_STATE_BAMBU_TIMEOUT_SEC", "10")).strip()
    try:
        timeout_sec = max(3, int(float(timeout_raw)))
    except ValueError:
        timeout_sec = 10

    settle_raw = str(os.getenv("SLOT_STATE_BAMBU_SETTLE_SEC", "2.0")).strip()
    try:
        settle_sec = max(0.2, float(settle_raw))
    except ValueError:
        settle_sec = 2.0
    settle_sec = min(settle_sec, float(timeout_sec))

    printer_rows: list[dict[str, Any]] = []

    for printer in printers:
        message_holder: dict[str, Any] = {"payload": None, "payloads": []}
        best_slots: list[dict[str, Any]] = []
        best_slot_score: tuple[int, int] = (0, 0)
        best_payload: dict[str, Any] | None = None
        first_slots_at: float | None = None

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
                if isinstance(payload, dict):
                    slots = _extract_bambu_slots(payload)
                    if slots:
                        ams_name_map: dict[int, str] = {}
                        for item in message_holder.get("payloads") or []:
                            if isinstance(item, dict):
                                ams_name_map.update(_extract_bambu_ams_name_map(item))
                        slots = _merge_ams_names_into_slots(slots, ams_name_map)

                        score = _score_bambu_slots(slots)
                        if score > best_slot_score:
                            best_slot_score = score
                            best_slots = slots
                            best_payload = payload
                        if first_slots_at is None:
                            first_slots_at = time.time()

                        # Bambu printers can emit partial AMS frames first; wait briefly for a fuller frame.
                        if first_slots_at is not None and (time.time() - first_slots_at) >= settle_sec:
                            break
                time.sleep(0.2)

            if best_slots and best_payload is not None:
                telemetry = _extract_bambu_telemetry(best_payload)
                printer_rows.append(
                    {
                        "printer": printer["name"],
                        "serial": printer.get("serial"),
                        "slots": best_slots,
                        "telemetry": telemetry,
                    }
                )
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


def _extract_printer_rows(payload: Any) -> list[dict[str, Any]]:
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

    rows: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue

        printer_name = _normalize_text(block.get("printer") or block.get("printer_name"), 120)
        printer_serial = _normalize_text(block.get("serial") or block.get("printer_serial"), 120)
        if not printer_name and not printer_serial:
            continue

        telemetry_raw = block.get("telemetry") if isinstance(block.get("telemetry"), dict) else {}
        slots_raw = block.get("slots") if isinstance(block.get("slots"), list) else []

        parsed_slots: list[dict[str, Any]] = []
        for slot_row in slots_raw:
            if not isinstance(slot_row, dict):
                continue
            slot_number = _normalize_slot(slot_row.get("slot") or slot_row.get("slot_id"))
            raw_ams_id = _normalize_ams_unit_id(
                _first_present_value(slot_row.get("ams_id"), slot_row.get("ams_unit"), slot_row.get("ams_index"))
            )
            if slot_number is not None and slot_number >= 100:
                inferred_ams_unit = slot_number // 100
                inferred_slot_local = slot_number % 100
            elif slot_number is not None:
                inferred_ams_unit = 1
                inferred_slot_local = slot_number
            else:
                inferred_ams_unit = 1
                inferred_slot_local = None
            slot_local = _normalize_slot(_first_present_value(slot_row.get("slot_local"), slot_row.get("ams_slot")))
            if slot_local is None:
                slot_local = inferred_slot_local
            ams_unit = _resolve_ams_unit(raw_ams_id, inferred_ams_unit) or 1
            canonical_slot = _compose_global_slot(ams_unit, slot_local)
            if canonical_slot is None:
                continue
            parsed_slots.append(
                {
                    "slot": canonical_slot,
                    "slot_local": slot_local,
                    "ams_unit": ams_unit,
                    "ams_name": _normalize_text(slot_row.get("ams_name") or slot_row.get("ams_label"), 120),
                    "observed_brand": _normalize_text(slot_row.get("brand"), 120),
                    "observed_material": _normalize_text(slot_row.get("material"), 80),
                    "observed_color": _normalize_text(slot_row.get("color"), 80),
                    "remain_pct": _normalize_percentage(slot_row.get("remain_pct")),
                    "tray_weight_g": _normalize_float(slot_row.get("tray_weight_g")),
                }
            )

        rows.append(
            {
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "slots": parsed_slots,
                "telemetry": {
                    "status": _normalize_online_status(telemetry_raw.get("status")),
                    "job_name": _normalize_text(telemetry_raw.get("job_name"), 255),
                    "job_status": _normalize_text(telemetry_raw.get("job_status"), 80),
                    "progress": _normalize_percentage(telemetry_raw.get("progress")),
                    "nozzle_temp": _normalize_float(telemetry_raw.get("nozzle_temp")),
                    "bed_temp": _normalize_float(telemetry_raw.get("bed_temp")),
                    "chamber_temp": _normalize_float(telemetry_raw.get("chamber_temp")),
                    "firmware": _normalize_text(telemetry_raw.get("firmware"), 120),
                    "error": _normalize_text(telemetry_raw.get("error"), 255),
                    "external_spool_active": _normalize_optional_bool(
                        telemetry_raw.get("external_spool_active")
                        if telemetry_raw.get("external_spool_active") is not None
                        else telemetry_raw.get("external_active_spool")
                    ),
                },
            }
        )

    return rows


def _upsert_printer_rows(project: str, source: str | None, rows: list[dict[str, Any]]) -> tuple[int, int]:
    if not rows:
        return 0, 0

    now = _utcnow_naive()
    slot_count = 0
    printer_count = 0
    with SessionLocal() as db:
        current_slots_by_printer: dict[str, set[int]] = {}
        for row in rows:
            printer_name = _normalize_text(row.get("printer_name"), 120)
            printer_serial = _normalize_text(row.get("printer_serial"), 120)
            if not printer_name and not printer_serial:
                continue

            printer = None
            if printer_serial:
                printer = (
                    db.query(Printer)
                    .filter(Printer.project == project, Printer.serial == printer_serial)
                    .first()
                )
            if printer is None and printer_name:
                printer = (
                    db.query(Printer)
                    .filter(Printer.project == project, Printer.name == printer_name)
                    .first()
                )
            if printer is None:
                fallback_name = printer_name or printer_serial
                fallback_serial = printer_serial or printer_name
                printer = Printer(
                    project=project,
                    name=fallback_name,
                    serial=fallback_serial,
                    status="unknown",
                    is_active=True,
                )
                db.add(printer)
                db.flush()

            telemetry = row.get("telemetry") if isinstance(row.get("telemetry"), dict) else {}
            if telemetry:
                printer.status = _normalize_online_status(telemetry.get("status"))
                printer.telemetry_job_name = _normalize_text(telemetry.get("job_name"), 255)
                printer.telemetry_job_status = _normalize_text(telemetry.get("job_status"), 80)
                printer.telemetry_progress = _normalize_percentage(telemetry.get("progress"))
                printer.telemetry_nozzle_temp = _normalize_float(telemetry.get("nozzle_temp"))
                printer.telemetry_bed_temp = _normalize_float(telemetry.get("bed_temp"))
                printer.telemetry_chamber_temp = _normalize_float(telemetry.get("chamber_temp"))
                printer.telemetry_firmware = _normalize_text(telemetry.get("firmware"), 120)
                printer.telemetry_error = _normalize_text(telemetry.get("error"), 255)
                printer.telemetry_external_spool_active = _normalize_optional_bool(telemetry.get("external_spool_active"))
                printer.last_seen_at = now
                printer.last_source = source

            printer_count += 1

            slots = row.get("slots") if isinstance(row.get("slots"), list) else []
            current_slot_set: set[int] = set()
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                slot_number = _normalize_slot(slot.get("slot"))
                if slot_number is None:
                    continue
                current_slot_set.add(slot_number)

                state = (
                    db.query(DeviceSlotState)
                    .filter(
                        DeviceSlotState.project == project,
                        DeviceSlotState.printer_name == printer.name,
                        DeviceSlotState.slot == slot_number,
                    )
                    .first()
                )
                if state is None:
                    state = DeviceSlotState(
                        project=project,
                        printer_name=printer.name,
                        slot=slot_number,
                    )
                    db.add(state)

                state.printer_serial = printer.serial
                raw_ams_id = _normalize_ams_unit_id(
                    _first_present_value(slot.get("ams_id"), slot.get("ams_unit"), slot.get("ams_index"))
                )
                inferred_ams_unit = slot_number // 100 if slot_number >= 100 else 1
                state.ams_unit = _resolve_ams_unit(raw_ams_id, inferred_ams_unit)
                state.slot_local = _normalize_slot(_first_present_value(slot.get("slot_local"), slot.get("ams_slot")))
                if state.slot_local is None:
                    state.slot_local = slot_number % 100 if slot_number >= 100 else slot_number
                state.ams_name = _normalize_text(slot.get("ams_name") or slot.get("ams_label"), 120)
                state.observed_brand = _normalize_text(slot.get("observed_brand"), 120)
                state.observed_material = _normalize_text(slot.get("observed_material"), 80)
                state.observed_color = _normalize_text(slot.get("observed_color"), 80)
                state.source = source
                state.observed_at = now
                state.updated_at = now
                slot_count += 1

            if current_slot_set:
                current_slots_by_printer[printer.name] = current_slot_set

        for printer_name, current_slot_set in current_slots_by_printer.items():
            stale_query = db.query(DeviceSlotState).filter(
                DeviceSlotState.project == project,
                DeviceSlotState.printer_name == printer_name,
            )
            if source:
                stale_query = stale_query.filter(DeviceSlotState.source == source)
            stale_query = stale_query.filter(~DeviceSlotState.slot.in_(list(current_slot_set)))
            stale_query.delete(synchronize_session=False)

        db.commit()
    return printer_count, slot_count


def main() -> int:
    interval_raw = str(os.getenv("SLOT_STATE_POLL_INTERVAL_SEC", "45")).strip()
    try:
        interval = max(1, int(float(interval_raw)))
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
            elif provider == "multi_brand_http":
                payload = _load_payload_multi_brand_http()
            else:
                payload = _load_payload()
            printer_rows = _extract_printer_rows(payload)
            updated_printers, updated_slots = _upsert_printer_rows(project=project, source=source, rows=printer_rows)
            if printer_rows:
                print(f"[slot-poller] upserted {updated_printers} printers, {updated_slots} slot states")
            else:
                print("[slot-poller] no printer data available")
        except urllib.error.URLError as error:
            print(f"[slot-poller] fetch failed: {error}")
        except Exception as error:
            print(f"[slot-poller] unexpected error: {error}")

        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
