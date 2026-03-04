from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.models import DeviceSlotState, Printer, UsageBatchContext


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed/update a realistic demo printer including AMS live data")
    parser.add_argument("--project", default="private", help="Target project")
    parser.add_argument("--serial", default="DEMO-LIVE-001", help="Demo printer serial")
    parser.add_argument("--name", default="Demo Live Printer", help="Demo printer display name")
    parser.add_argument("--host", default="192.168.178.55", help="Demo printer host/IP")
    parser.add_argument("--port", type=int, default=8883, help="Demo printer port")
    parser.add_argument("--access-code", default="12345678", help="Demo access code")
    return parser


def _upsert_printer(db, *, project: str, serial: str, name: str, host: str, port: int, access_code: str, now: datetime) -> Printer:
    printer = (
        db.query(Printer)
        .filter(Printer.project == project, Printer.serial == serial)
        .one_or_none()
    )

    if printer is None:
        printer = Printer(project=project, serial=serial, name=name)
        db.add(printer)

    printer.name = name
    printer.host = host
    printer.port = max(1, min(int(port or 8883), 65535))
    printer.access_code = access_code
    printer.is_active = True
    printer.status = "online"
    printer.last_seen_at = now
    printer.last_source = "demo-seed"
    printer.telemetry_job_name = "Gearbox_Housing_v3.gcode"
    printer.telemetry_job_status = "printing"
    printer.telemetry_progress = 67.4
    printer.telemetry_nozzle_temp = 219.3
    printer.telemetry_bed_temp = 61.8
    printer.telemetry_chamber_temp = 34.6
    printer.telemetry_firmware = "01.09.03.50"
    printer.telemetry_error = None
    printer.updated_at = now
    return printer


def _upsert_ams_live_slots(db, *, project: str, serial: str, printer_name: str, now: datetime) -> None:
    demo_slots = [
        {"slot": 1, "brand": "Bambu Lab", "material": "PLA", "color": "Black", "age_min": 1},
        {"slot": 2, "brand": "Bambu Lab", "material": "PETG", "color": "White", "age_min": 2},
        {"slot": 3, "brand": "Polymaker", "material": "ASA", "color": "Gray", "age_min": 3},
        {"slot": 4, "brand": "eSUN", "material": "TPU", "color": "Blue", "age_min": 5},
    ]

    existing_rows = (
        db.query(DeviceSlotState)
        .filter(
            DeviceSlotState.project == project,
            DeviceSlotState.printer_serial == serial,
        )
        .all()
    )
    by_slot = {int(row.slot or 0): row for row in existing_rows if int(row.slot or 0) > 0}

    for item in demo_slots:
        slot = int(item["slot"])
        observed_at = now - timedelta(minutes=int(item["age_min"]))
        row = by_slot.get(slot)
        if row is None:
            row = DeviceSlotState(
                project=project,
                printer_name=printer_name,
                printer_serial=serial,
                slot=slot,
            )
            db.add(row)

        row.printer_name = printer_name
        row.printer_serial = serial
        row.observed_brand = item["brand"]
        row.observed_material = item["material"]
        row.observed_color = item["color"]
        row.source = "demo-seed"
        row.observed_at = observed_at
        row.updated_at = now


def _upsert_usage_batch_context(db, *, project: str, serial: str, printer_name: str, now: datetime) -> None:
    batch_id = "demo-live-batch-001"
    context = (
        db.query(UsageBatchContext)
        .filter(UsageBatchContext.project == project, UsageBatchContext.batch_id == batch_id)
        .one_or_none()
    )
    if context is None:
        context = UsageBatchContext(project=project, batch_id=batch_id)
        db.add(context)

    context.printer_name = printer_name
    context.printer_serial = serial
    context.ams_slots = "1,2,3,4"
    context.created_at = context.created_at or now


def main() -> int:
    args = _build_parser().parse_args()
    project = str(args.project or "private").strip() or "private"
    serial = str(args.serial or "DEMO-LIVE-001").strip() or "DEMO-LIVE-001"
    name = str(args.name or "Demo Live Printer").strip() or "Demo Live Printer"
    host = str(args.host or "192.168.178.55").strip() or "192.168.178.55"
    access_code = str(args.access_code or "12345678").strip() or "12345678"

    now = _utcnow_naive()

    with SessionLocal() as db:
        printer = _upsert_printer(
            db,
            project=project,
            serial=serial,
            name=name,
            host=host,
            port=args.port,
            access_code=access_code,
            now=now,
        )
        db.flush()

        _upsert_ams_live_slots(
            db,
            project=project,
            serial=serial,
            printer_name=printer.name,
            now=now,
        )
        _upsert_usage_batch_context(
            db,
            project=project,
            serial=serial,
            printer_name=printer.name,
            now=now,
        )

        db.commit()

    print(f"Demo printer seeded/updated: project='{project}', name='{name}', serial='{serial}'")
    print("Included: printer telemetry, AMS live slots (1-4), usage batch context.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
