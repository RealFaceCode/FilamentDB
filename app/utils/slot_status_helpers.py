from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models import DeviceSlotState, Spool, UsageBatchContext
from .printer_ams import (
    compose_ams_global_slot,
    first_present_value,
    infer_ams_slot_parts,
    normalize_ams_raw_id,
    normalize_ams_slot,
    normalize_printer_name,
    normalize_printer_serial,
    normalize_printer_status,
    parse_slot_tokens,
    resolve_ams_label,
    resolve_ams_unit,
    resolve_or_create_printer,
    serialize_ams_slots,
)
from .usage_parsing import parse_optional_bool, parse_optional_float


def build_slot_status_rows(
    mapped_spools: list[Spool],
    live_states: list[DeviceSlotState],
    stale_minutes: int,
    printer_ams_name_maps: Optional[dict[str, dict[int, str]]] = None,
) -> tuple[list[dict], dict[str, int]]:
    printer_has_ams_signal: dict[str, bool] = {}
    for state in live_states:
        printer_key = str(state.printer_name or state.printer_serial or "").strip()
        if not printer_key:
            continue
        slot_value = int(state.slot or 0)
        has_signal = (
            int(state.ams_unit or 0) > 0
            or int(state.slot_local or 0) > 0
            or slot_value >= 100
        )
        if has_signal:
            printer_has_ams_signal[printer_key] = True

    def _canonical_slot_for_status(
        printer_name: str,
        slot: int,
        ams_unit: Optional[int] = None,
        slot_local: Optional[int] = None,
    ) -> int:
        if slot <= 0:
            return slot
        should_canonicalize = bool(printer_has_ams_signal.get(printer_name))
        if not should_canonicalize and slot >= 100:
            should_canonicalize = True
        if not should_canonicalize:
            return slot

        normalized_unit = int(ams_unit or 0) or None
        normalized_local = int(slot_local or 0) or None
        inferred_unit, inferred_local = infer_ams_slot_parts(slot)
        if normalized_unit is None:
            normalized_unit = inferred_unit
        if normalized_local is None:
            normalized_local = inferred_local
        canonical = compose_ams_global_slot(normalized_unit, normalized_local)
        return int(canonical or slot)

    state_map: dict[tuple[str, int], DeviceSlotState] = {}
    for state in live_states:
        printer_key = str(state.printer_name or state.printer_serial or "").strip()
        slot_value = int(state.slot or 0)
        canonical_slot = _canonical_slot_for_status(
            printer_key,
            slot_value,
            int(state.ams_unit or 0) or None,
            int(state.slot_local or 0) or None,
        )
        key = (printer_key, canonical_slot)
        if not key[0] or key[1] <= 0:
            continue
        current = state_map.get(key)
        if current is None:
            state_map[key] = state
            continue
        current_seen = current.observed_at
        next_seen = state.observed_at
        if current_seen is None and next_seen is not None:
            state_map[key] = state
            continue
        if current_seen is not None and next_seen is not None and next_seen > current_seen:
            state_map[key] = state

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_seconds = stale_minutes * 60
    rows: list[dict] = []
    summary = {
        "ok": 0,
        "mismatch": 0,
        "missing": 0,
        "stale": 0,
        "unknown": 0,
    }

    def _same_text(a: Optional[str], b: Optional[str]) -> bool:
        return str(a or "").strip().lower() == str(b or "").strip().lower()

    normalized_maps: dict[str, dict[int, str]] = {}
    for printer_key, mapping in (printer_ams_name_maps or {}).items():
        normalized_printer = normalize_printer_name(printer_key)
        if not normalized_printer or not isinstance(mapping, dict):
            continue
        normalized_maps[normalized_printer] = mapping

    def _resolve_ams_label_for_printer(printer_name: Optional[str], ams_unit: Optional[int], ams_name: Optional[str]) -> str:
        normalized_printer = normalize_printer_name(printer_name)
        custom_mapping = normalized_maps.get(normalized_printer) if normalized_printer else None
        return resolve_ams_label(ams_name, ams_unit, custom_mapping)

    def _format_ams_descriptor(
        printer_name: Optional[str],
        ams_unit: Optional[int],
        slot_local: Optional[int],
        ams_name: Optional[str] = None,
    ) -> str:
        label = _resolve_ams_label_for_printer(printer_name, ams_unit, ams_name)
        if slot_local is not None and slot_local > 0:
            return f"{label} · S{int(slot_local)}"
        return label

    expected_map: dict[tuple[str, int], Spool] = {}
    ordered_spools = sorted(
        mapped_spools,
        key=lambda spool: ((spool.ams_printer or "").strip().lower(), int(spool.ams_slot or 0), int(spool.id or 0)),
    )
    for spool in ordered_spools:
        printer = str(spool.ams_printer or "").strip()
        slot = _canonical_slot_for_status(printer, int(spool.ams_slot or 0))
        if not printer or slot <= 0:
            continue
        expected_map.setdefault((printer, slot), spool)

    all_keys = set(expected_map.keys()) | set(state_map.keys())
    ordered_keys = sorted(all_keys, key=lambda item: (str(item[0]).lower(), int(item[1])))

    for printer, slot in ordered_keys:
        spool = expected_map.get((printer, slot))
        state = state_map.get((printer, slot))
        state_label = "unknown"

        if spool is not None and state is None:
            state_label = "missing"
        elif state is not None:
            observed_at = state.observed_at
            is_stale = False
            if observed_at is not None:
                age_seconds = (now - observed_at).total_seconds()
                is_stale = age_seconds > stale_seconds

            if is_stale:
                state_label = "stale"
            else:
                observed_material = str(state.observed_material or "").strip()
                observed_color = str(state.observed_color or "").strip()
                observed_brand = str(state.observed_brand or "").strip()
                if not observed_material and not observed_color and not observed_brand:
                    state_label = "unknown"
                elif spool is None:
                    state_label = "unknown"
                else:
                    matches = _same_text(spool.material, state.observed_material) and _same_text(spool.color, state.observed_color)
                    state_label = "ok" if matches else "mismatch"

        summary[state_label] += 1
        expected_ams = "-"
        if spool is not None:
            expected_unit, expected_local = infer_ams_slot_parts(int(spool.ams_slot or 0))
            expected_ams = _format_ams_descriptor(printer, expected_unit, expected_local)

        observed_ams = "-"
        if state is not None:
            observed_unit = int(state.ams_unit or 0) or None
            observed_local = int(state.slot_local or 0) or None
            if observed_unit is None or observed_local is None:
                inferred_unit, inferred_local = infer_ams_slot_parts(int(state.slot or 0))
                observed_unit = observed_unit or inferred_unit
                observed_local = observed_local or inferred_local
            observed_ams = _format_ams_descriptor(
                printer,
                observed_unit,
                observed_local,
                str(state.ams_name or "").strip() or None,
            )

        rows.append(
            {
                "printer": printer,
                "slot": slot,
                "expected_ams": expected_ams,
                "observed_ams": observed_ams,
                "spool": spool,
                "observed_brand": state.observed_brand if state else None,
                "observed_material": state.observed_material if state else None,
                "observed_color": state.observed_color if state else None,
                "source": state.source if state else None,
                "observed_at": state.observed_at if state else None,
                "state": state_label,
            }
        )

    return rows, summary


def summarize_slot_data_freshness(observed_times: list[Optional[datetime]], stale_minutes: int) -> dict[str, object]:
    valid_times = [timestamp for timestamp in observed_times if isinstance(timestamp, datetime)]
    if not valid_times:
        return {
            "has_data": False,
            "status": "no_data",
            "is_stale": True,
            "last_seen_at": None,
            "age_seconds": None,
        }

    latest_seen = max(valid_times)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    age_seconds = max(0, int((now - latest_seen).total_seconds()))
    stale_seconds = int(stale_minutes * 60)
    is_stale = age_seconds > stale_seconds

    return {
        "has_data": True,
        "status": "stale" if is_stale else "fresh",
        "is_stale": is_stale,
        "last_seen_at": latest_seen,
        "age_seconds": age_seconds,
    }


def normalize_signature_text(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def build_slot_remap_plan(mapped_spools: list[Spool], live_states: list[DeviceSlotState]) -> list[tuple[Spool, int]]:
    spools_by_printer: dict[str, list[Spool]] = {}
    for spool in mapped_spools:
        printer = normalize_printer_name(spool.ams_printer)
        slot = int(spool.ams_slot or 0)
        if not printer or slot <= 0:
            continue
        spools_by_printer.setdefault(printer, []).append(spool)

    states_by_printer: dict[str, list[DeviceSlotState]] = {}
    for state in live_states:
        printer = normalize_printer_name(state.printer_name or state.printer_serial)
        slot = int(state.slot or 0)
        if not printer or slot <= 0:
            continue
        states_by_printer.setdefault(printer, []).append(state)

    plan: list[tuple[Spool, int]] = []
    for printer, printer_spools in spools_by_printer.items():
        printer_states = states_by_printer.get(printer, [])
        if not printer_states:
            continue

        spool_sig_counts: dict[tuple[str, str], int] = {}
        state_sig_counts: dict[tuple[str, str], int] = {}
        state_sig_to_slot: dict[tuple[str, str], int] = {}

        for spool in printer_spools:
            sig = (normalize_signature_text(spool.material), normalize_signature_text(spool.color))
            if not sig[0] or not sig[1]:
                continue
            spool_sig_counts[sig] = int(spool_sig_counts.get(sig, 0)) + 1

        for state in printer_states:
            sig = (normalize_signature_text(state.observed_material), normalize_signature_text(state.observed_color))
            if not sig[0] or not sig[1]:
                continue
            state_sig_counts[sig] = int(state_sig_counts.get(sig, 0)) + 1
            state_sig_to_slot[sig] = int(state.slot or 0)

        for spool in printer_spools:
            current_slot = int(spool.ams_slot or 0)
            sig = (normalize_signature_text(spool.material), normalize_signature_text(spool.color))
            if not sig[0] or not sig[1]:
                continue
            if int(spool_sig_counts.get(sig, 0)) != 1:
                continue
            if int(state_sig_counts.get(sig, 0)) != 1:
                continue

            target_slot = int(state_sig_to_slot.get(sig, 0) or 0)
            if target_slot <= 0 or target_slot == current_slot:
                continue
            plan.append((spool, target_slot))

    return plan


def migrate_slot_format_to_canonical(db: Session, project: str) -> dict[str, int]:
    result = {
        "spools": 0,
        "states": 0,
        "contexts": 0,
        "skipped": 0,
    }

    spool_rows = (
        db.query(Spool)
        .filter(Spool.project == project, Spool.ams_slot.is_not(None), Spool.ams_slot > 0)
        .all()
    )
    for spool in spool_rows:
        old_slot = int(spool.ams_slot or 0)
        ams_unit, slot_local = infer_ams_slot_parts(old_slot)
        new_slot = compose_ams_global_slot(ams_unit, slot_local)
        if new_slot is None or new_slot == old_slot:
            continue
        conflict = (
            db.query(Spool)
            .filter(
                Spool.project == project,
                Spool.id != spool.id,
                Spool.ams_printer == spool.ams_printer,
                Spool.ams_slot == new_slot,
            )
            .first()
        )
        if conflict is not None:
            result["skipped"] += 1
            continue
        spool.ams_slot = int(new_slot)
        result["spools"] += 1

    state_rows = (
        db.query(DeviceSlotState)
        .filter(DeviceSlotState.project == project, DeviceSlotState.slot.is_not(None), DeviceSlotState.slot > 0)
        .all()
    )
    for state in state_rows:
        old_slot = int(state.slot or 0)
        ams_unit = int(state.ams_unit or 0) or None
        slot_local = int(state.slot_local or 0) or None
        if ams_unit is None and old_slot < 100:
            continue
        if slot_local is None:
            _, inferred_local = infer_ams_slot_parts(old_slot)
            slot_local = inferred_local
        if ams_unit is None:
            inferred_unit, _ = infer_ams_slot_parts(old_slot)
            ams_unit = inferred_unit
        new_slot = compose_ams_global_slot(ams_unit, slot_local)
        if new_slot is None or new_slot == old_slot:
            continue
        conflict = (
            db.query(DeviceSlotState)
            .filter(
                DeviceSlotState.project == project,
                DeviceSlotState.id != state.id,
                DeviceSlotState.printer_name == state.printer_name,
                DeviceSlotState.slot == new_slot,
            )
            .first()
        )
        if conflict is not None:
            result["skipped"] += 1
            continue
        state.slot = int(new_slot)
        if state.slot_local is None and slot_local is not None:
            state.slot_local = int(slot_local)
        if state.ams_unit is None and ams_unit is not None:
            state.ams_unit = int(ams_unit)
        result["states"] += 1

    context_rows = (
        db.query(UsageBatchContext)
        .filter(UsageBatchContext.project == project, UsageBatchContext.ams_slots.is_not(None), UsageBatchContext.ams_slots != "")
        .all()
    )
    for context in context_rows:
        old_slots = parse_slot_tokens(context.ams_slots)
        if not old_slots:
            continue
        new_slots: list[int] = []
        changed = False
        for slot in old_slots:
            ams_unit, slot_local = infer_ams_slot_parts(slot)
            canonical = compose_ams_global_slot(ams_unit, slot_local)
            if canonical is None:
                continue
            new_slots.append(int(canonical))
            if int(canonical) != int(slot):
                changed = True
        if not changed:
            continue
        context.ams_slots = serialize_ams_slots(new_slots)
        result["contexts"] += 1

    return result


def extract_slot_state_entries(payload: object) -> list[dict]:
    if payload is None:
        return []

    blocks: list[object]
    if isinstance(payload, dict) and isinstance(payload.get("printers"), list):
        blocks = payload.get("printers", [])
    elif isinstance(payload, list):
        blocks = payload
    elif isinstance(payload, dict):
        blocks = [payload]
    else:
        return []

    entries: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue

        printer_name = normalize_printer_name(block.get("printer") or block.get("printer_name"))
        printer_serial = normalize_printer_serial(block.get("serial") or block.get("printer_serial"))
        slots_raw = block.get("slots")
        if not isinstance(slots_raw, list):
            slots_raw = []
        if not printer_name and not printer_serial:
            continue

        telemetry = block.get("telemetry") if isinstance(block.get("telemetry"), dict) else {}
        telemetry_data = {
            "status": normalize_printer_status(telemetry.get("status") or block.get("status")),
            "job_name": str(telemetry.get("job_name") or telemetry.get("job") or "").strip()[:255] or None,
            "job_status": str(telemetry.get("job_status") or telemetry.get("state") or "").strip()[:80] or None,
            "progress": parse_optional_float(str(telemetry.get("progress") or "").strip() or None),
            "nozzle_temp": parse_optional_float(str(telemetry.get("nozzle_temp") or "").strip() or None),
            "bed_temp": parse_optional_float(str(telemetry.get("bed_temp") or "").strip() or None),
            "chamber_temp": parse_optional_float(str(telemetry.get("chamber_temp") or "").strip() or None),
            "firmware": str(telemetry.get("firmware") or "").strip()[:120] or None,
            "error": str(telemetry.get("error") or telemetry.get("error_message") or "").strip()[:255] or None,
            "external_spool_active": parse_optional_bool(
                telemetry.get("external_spool_active")
                if telemetry.get("external_spool_active") is not None
                else telemetry.get("external_active_spool")
            ),
        }

        if not slots_raw:
            entries.append(
                {
                    "printer_name": printer_name,
                    "printer_serial": printer_serial,
                    "slot": None,
                    "observed_brand": None,
                    "observed_material": None,
                    "observed_color": None,
                    "telemetry": telemetry_data,
                }
            )
            continue

        for row in slots_raw:
            if not isinstance(row, dict):
                continue

            slot = normalize_ams_slot(row.get("slot") or row.get("slot_id"))
            slot_local = normalize_ams_slot(first_present_value(row.get("slot_local"), row.get("ams_slot")))
            raw_ams_id = normalize_ams_raw_id(
                first_present_value(row.get("ams_id"), row.get("ams_unit"), row.get("ams_index"))
            )
            ams_unit = resolve_ams_unit(raw_ams_id)
            if ams_unit is None:
                ams_unit = normalize_ams_slot(first_present_value(row.get("ams_unit"), row.get("ams_index")))
            ams_name = str(row.get("ams_name") or row.get("ams_label") or "").strip()[:120] or None

            if slot_local is None:
                slot_local = slot
            if slot is None:
                slot = compose_ams_global_slot(ams_unit, slot_local)
            if slot is None:
                continue

            if ams_unit is None or slot_local is None:
                inferred_ams_unit, inferred_slot_local = infer_ams_slot_parts(slot)
                if ams_unit is None:
                    ams_unit = inferred_ams_unit
                if slot_local is None:
                    slot_local = inferred_slot_local

            if ams_unit is not None and slot_local is not None:
                canonical_slot = compose_ams_global_slot(ams_unit, slot_local)
                if canonical_slot is not None:
                    slot = canonical_slot

            ams_name = resolve_ams_label(ams_name, ams_unit)

            entries.append(
                {
                    "printer_name": printer_name,
                    "printer_serial": printer_serial,
                    "slot": slot,
                    "slot_local": slot_local,
                    "ams_unit": ams_unit,
                    "ams_name": ams_name,
                    "observed_brand": str(row.get("brand") or "").strip()[:120] or None,
                    "observed_material": str(row.get("material") or "").strip()[:80] or None,
                    "observed_color": str(row.get("color") or "").strip()[:80] or None,
                    "telemetry": telemetry_data,
                }
            )

    return entries


def upsert_slot_state_entries(
    db: Session,
    project: str,
    source: str,
    entries: list[dict],
    utcnow_fn: Callable[[], datetime],
) -> int:
    if not entries:
        return 0

    now = utcnow_fn().replace(tzinfo=None)
    updated = 0

    for entry in entries:
        printer = resolve_or_create_printer(
            db=db,
            project=project,
            printer_name=entry.get("printer_name"),
            printer_serial=entry.get("printer_serial"),
        )
        resolved_printer_name = normalize_printer_name(entry.get("printer_name")) or (printer.name if printer else None)
        resolved_printer_serial = normalize_printer_serial(entry.get("printer_serial")) or (printer.serial if printer else None)
        if not resolved_printer_name:
            continue

        state_filters = [
            DeviceSlotState.project == project,
            DeviceSlotState.printer_name == resolved_printer_name,
            DeviceSlotState.slot == entry["slot"],
        ]
        slot_value = entry.get("slot")
        if slot_value is not None:
            state = (
                db.query(DeviceSlotState)
                .filter(*state_filters)
                .first()
            )
            if state is None:
                state = DeviceSlotState(
                    project=project,
                    printer_name=resolved_printer_name,
                    slot=entry["slot"],
                )
                db.add(state)

            state.printer_name = resolved_printer_name
            state.printer_serial = resolved_printer_serial
            state.ams_unit = entry.get("ams_unit")
            state.slot_local = entry.get("slot_local")
            state.ams_name = entry.get("ams_name")
            state.observed_brand = entry.get("observed_brand")
            state.observed_material = entry.get("observed_material")
            state.observed_color = entry.get("observed_color")
            state.source = source
            state.observed_at = now
            state.updated_at = now

        if printer is not None:
            telemetry = entry.get("telemetry") if isinstance(entry.get("telemetry"), dict) else {}
            printer.last_seen_at = now
            printer.last_source = source
            printer.status = normalize_printer_status(telemetry.get("status"))
            printer.telemetry_job_name = telemetry.get("job_name")
            printer.telemetry_job_status = telemetry.get("job_status")
            printer.telemetry_progress = telemetry.get("progress")
            printer.telemetry_nozzle_temp = telemetry.get("nozzle_temp")
            printer.telemetry_bed_temp = telemetry.get("bed_temp")
            printer.telemetry_chamber_temp = telemetry.get("chamber_temp")
            printer.telemetry_firmware = telemetry.get("firmware")
            printer.telemetry_error = telemetry.get("error")
            external_spool_active = parse_optional_bool(telemetry.get("external_spool_active"))
            if external_spool_active is not None:
                printer.telemetry_external_spool_active = external_spool_active
            printer.updated_at = now
        updated += 1

    return updated
