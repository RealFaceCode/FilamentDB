from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Printer, Spool


def normalize_printer_name(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized[:120] if normalized else None


def normalize_printer_serial(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized[:120] if normalized else None


def normalize_printer_port(value: Optional[str]) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 8883
    try:
        parsed = int(float(raw))
    except ValueError:
        return 8883
    return parsed if 1 <= parsed <= 65535 else 8883


def normalize_printer_status(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"online", "offline", "unknown"}:
        return normalized
    return "unknown"


def format_printer_temperatures(printer: Printer) -> str:
    values: list[str] = []
    if printer.telemetry_nozzle_temp is not None:
        values.append(f"N {round(float(printer.telemetry_nozzle_temp), 1)}°C")
    if printer.telemetry_bed_temp is not None:
        values.append(f"B {round(float(printer.telemetry_bed_temp), 1)}°C")
    if printer.telemetry_chamber_temp is not None:
        values.append(f"C {round(float(printer.telemetry_chamber_temp), 1)}°C")
    return " · ".join(values)


def resolve_or_create_printer(
    db: Session,
    project: str,
    printer_name: Optional[str],
    printer_serial: Optional[str],
) -> Optional[Printer]:
    normalized_name = normalize_printer_name(printer_name)
    normalized_serial = normalize_printer_serial(printer_serial)

    printer = None
    if normalized_serial:
        printer = (
            db.query(Printer)
            .filter(Printer.project == project, Printer.serial == normalized_serial)
            .first()
        )

    if printer is None and normalized_name:
        printer = (
            db.query(Printer)
            .filter(Printer.project == project, Printer.name == normalized_name)
            .first()
        )

    if printer is None:
        if not normalized_name and not normalized_serial:
            return None
        fallback_name = normalized_name or normalized_serial
        fallback_serial = normalized_serial or normalized_name
        if not fallback_name or not fallback_serial:
            return None
        printer = Printer(
            project=project,
            name=fallback_name,
            serial=fallback_serial,
            status="unknown",
            is_active=True,
        )
        db.add(printer)
        db.flush()
    else:
        if normalized_name and printer.name != normalized_name:
            printer.name = normalized_name
        if normalized_serial and printer.serial != normalized_serial:
            printer.serial = normalized_serial

    return printer


def normalize_ams_slot(value: Optional[str]) -> Optional[int]:
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


def normalize_ams_raw_id(value: object) -> Optional[int]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def first_present_value(*values: object) -> object:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


_AMS_RAW_ID_TO_UNIT = {
    0: 1,
    128: 2,
    129: 3,
    130: 4,
}


def resolve_ams_unit(raw_ams_id: Optional[int], fallback_unit: Optional[int] = None) -> Optional[int]:
    if raw_ams_id is not None:
        if raw_ams_id in _AMS_RAW_ID_TO_UNIT:
            return _AMS_RAW_ID_TO_UNIT[raw_ams_id]
        if 1 <= raw_ams_id <= 26:
            return raw_ams_id
    if fallback_unit is not None and fallback_unit > 0:
        return fallback_unit
    return None


def compose_ams_global_slot(ams_unit: Optional[int], slot_local: Optional[int]) -> Optional[int]:
    if slot_local is None:
        return None
    if ams_unit is None or ams_unit <= 0:
        ams_unit = 1
    return (ams_unit * 100) + slot_local


def infer_ams_slot_parts(global_slot: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    if global_slot is None or global_slot <= 0:
        return None, None
    if global_slot >= 100:
        ams_unit = global_slot // 100
        slot_local = global_slot % 100
        if ams_unit > 0 and slot_local > 0:
            return ams_unit, slot_local
    return 1, global_slot


def normalize_ams_slot_canonical(value: Optional[str]) -> Optional[int]:
    parsed = normalize_ams_slot(value)
    if parsed is None:
        return None
    ams_unit, slot_local = infer_ams_slot_parts(parsed)
    return compose_ams_global_slot(ams_unit, slot_local)


def equivalent_ams_slots(slot: int) -> set[int]:
    if slot <= 0:
        return set()
    candidates: set[int] = {slot}
    inferred_unit, inferred_local = infer_ams_slot_parts(slot)
    canonical = compose_ams_global_slot(inferred_unit, inferred_local)
    if canonical is not None:
        candidates.add(canonical)
    if inferred_unit == 1 and inferred_local is not None:
        candidates.add(inferred_local)
    if slot < 100:
        candidates.add(100 + slot)
    return {value for value in candidates if value > 0}


_AMS_ID_NAME_FALLBACK = {
    0: "HT-A",
    128: "HT-B",
    129: "HT-C",
    130: "HT-D",
}


def fallback_ams_label(ams_unit: Optional[int], raw_ams_id: Optional[int] = None) -> str:
    if raw_ams_id is not None and raw_ams_id in _AMS_ID_NAME_FALLBACK:
        return _AMS_ID_NAME_FALLBACK[raw_ams_id]
    if ams_unit is not None and ams_unit > 0 and ams_unit <= 26:
        return f"HT-{chr(ord('A') + ams_unit - 1)}"
    if ams_unit is not None and ams_unit > 0:
        return f"AMS {ams_unit}"
    return "AMS"


def parse_ams_name_mapping(value: Optional[str]) -> dict[int, str]:
    normalized = str(value or "").strip()
    if not normalized:
        return {}

    mapping: dict[int, str] = {}
    parts = re.split(r"[\n,;]+", normalized)
    for part in parts:
        entry = str(part or "").strip()
        if not entry:
            continue

        if "=" in entry:
            key_text, label_text = entry.split("=", 1)
        elif ":" in entry:
            key_text, label_text = entry.split(":", 1)
        else:
            continue

        key_raw = str(key_text or "").strip().upper()
        label = str(label_text or "").strip()[:120]
        if not key_raw or not label:
            continue

        key_value: Optional[int] = None
        if key_raw.isdigit():
            key_value = int(key_raw)
        elif re.fullmatch(r"HT-[A-Z]", key_raw):
            key_value = ord(key_raw[-1]) - ord("A") + 1
        elif re.fullmatch(r"[A-Z]", key_raw):
            key_value = ord(key_raw) - ord("A") + 1

        if key_value is None or key_value <= 0:
            continue
        mapping[key_value] = label

    return mapping


def serialize_ams_name_mapping(mapping: dict[int, str]) -> Optional[str]:
    if not mapping:
        return None
    parts: list[str] = []
    for unit in sorted(mapping.keys()):
        label = str(mapping.get(unit) or "").strip()[:120]
        if not label:
            continue
        parts.append(f"{unit}={label}")
    if not parts:
        return None
    return ",".join(parts)


def resolve_ams_label(ams_name: Optional[str], ams_unit: Optional[int], custom_mapping: Optional[dict[int, str]] = None) -> str:
    if custom_mapping and ams_unit is not None and ams_unit in custom_mapping:
        return str(custom_mapping[ams_unit]).strip() or fallback_ams_label(ams_unit)

    normalized = str(ams_name or "").strip()
    if normalized:
        matched_id = re.fullmatch(r"AMS[-\s]*ID[-\s]*(\d+)", normalized, flags=re.IGNORECASE)
        if matched_id:
            return fallback_ams_label(ams_unit, int(matched_id.group(1)))
        if re.fullmatch(r"\d+", normalized):
            return fallback_ams_label(ams_unit, int(normalized))
        return normalized
    return fallback_ams_label(ams_unit)


def humanize_observed_color(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None

    normalized = raw.upper().lstrip("#")
    if len(normalized) == 8:
        normalized = normalized[:6]
    if len(normalized) != 6 or not re.fullmatch(r"[0-9A-F]{6}", normalized):
        return raw

    r = int(normalized[0:2], 16)
    g = int(normalized[2:4], 16)
    b = int(normalized[4:6], 16)

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
    return f"Color #{normalized}"


def slot_scoped_spools(spools: list[Spool], slot: int, printer_name: Optional[str]) -> list[Spool]:
    normalized_printer = normalize_printer_name(printer_name)
    slot_candidates = equivalent_ams_slots(slot)
    if normalized_printer:
        exact = [
            spool for spool in spools
            if int(spool.ams_slot or 0) in slot_candidates and (spool.ams_printer or "").strip() == normalized_printer
        ]
        if exact:
            return exact
        fallback_global = [
            spool for spool in spools
            if int(spool.ams_slot or 0) in slot_candidates and not str(spool.ams_printer or "").strip()
        ]
        if fallback_global:
            return fallback_global
        return []

    return [spool for spool in spools if int(spool.ams_slot or 0) in slot_candidates]


def find_ams_slot_conflict(
    db: Session,
    project: str,
    ams_printer: Optional[str],
    ams_slot: Optional[int],
    exclude_spool_id: Optional[int] = None,
) -> Optional[Spool]:
    if ams_slot is None:
        return None

    query = db.query(Spool).filter(Spool.project == project, Spool.ams_slot == ams_slot)
    normalized_printer = normalize_printer_name(ams_printer)
    if normalized_printer:
        query = query.filter(Spool.ams_printer == normalized_printer)
    else:
        query = query.filter(or_(Spool.ams_printer.is_(None), Spool.ams_printer == ""))

    if exclude_spool_id is not None:
        query = query.filter(Spool.id != exclude_spool_id)

    return query.order_by(Spool.id.asc()).first()


def parse_slot_tokens(raw: Optional[str]) -> list[int]:
    if raw is None:
        return []

    values: list[int] = []
    for token in re.split(r"[\s,;]+", str(raw).strip()):
        if not token:
            continue
        try:
            value = int(float(token))
        except ValueError:
            continue
        if value <= 0:
            continue
        values.append(value)

    seen: set[int] = set()
    unique_sorted: list[int] = []
    for value in sorted(values):
        if value in seen:
            continue
        seen.add(value)
        unique_sorted.append(value)
    return unique_sorted


def resolve_ams_slots(raw: Optional[str], usage_breakdown: list[dict]) -> list[int]:
    from_payload = parse_slot_tokens(raw)
    if from_payload:
        return from_payload

    detected: list[int] = []
    for item in usage_breakdown or []:
        slot = item.get("slot")
        if slot is None:
            continue
        try:
            value = int(float(slot))
        except (TypeError, ValueError):
            continue
        if value > 0:
            detected.append(value)

    seen: set[int] = set()
    unique_sorted: list[int] = []
    for value in sorted(detected):
        if value in seen:
            continue
        seen.add(value)
        unique_sorted.append(value)
    return unique_sorted


def serialize_ams_slots(slots: list[int]) -> Optional[str]:
    if not slots:
        return None
    return ",".join(str(slot) for slot in slots)
