from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.models import DeviceSlotState, Spool, UsageHistory


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed demo data for help screenshots (staging only)")
    parser.add_argument("--project", default="private", choices=["private", "business"], help="Target project")
    parser.add_argument("--prefix", default="[HELP-DEMO]", help="Marker prefix for safe cleanup")
    parser.add_argument("--force", action="store_true", help="Allow running when APP_ENV=production (not recommended)")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    project = args.project
    prefix = str(args.prefix or "").strip() or "[HELP-DEMO]"

    if not args.force and str(os.getenv("APP_ENV", "development")).strip().lower() == "production":
        print("Refusing to seed demo data in production. Use staging or pass --force explicitly.")
        return 2

    now = _now_naive()

    demo_spools = [
        Spool(
            brand=f"{prefix} Bambu",
            material="PLA",
            color="Black",
            weight_g=1000,
            remaining_g=620,
            price=24.9,
            location=f"{prefix} Regal A1",
            in_use=True,
            project=project,
            ams_printer="P1S-01",
            ams_slot=1,
            updated_at=now,
        ),
        Spool(
            brand=f"{prefix} Bambu",
            material="PETG",
            color="White",
            weight_g=1000,
            remaining_g=310,
            price=27.9,
            location=f"{prefix} Regal A2",
            in_use=True,
            project=project,
            ams_printer="P1S-01",
            ams_slot=2,
            updated_at=now,
        ),
        Spool(
            brand=f"{prefix} Polymaker",
            material="ASA",
            color="Gray",
            weight_g=1000,
            remaining_g=90,
            price=34.9,
            location=f"{prefix} Regal B1",
            in_use=False,
            project=project,
            updated_at=now,
        ),
    ]

    with SessionLocal() as db:
        existing = (
            db.query(Spool)
            .filter(Spool.project == project, Spool.brand.like(f"{prefix}%"))
            .count()
        )
        if existing > 0:
            print("Demo data already exists for this project/prefix. Cleanup first or use a different prefix.")
            return 1

        db.add_all(demo_spools)
        db.flush()

        db.add_all(
            [
                UsageHistory(
                    actor="help-demo",
                    mode="manual",
                    source_app="help-seed",
                    batch_id=f"help-demo-{now.strftime('%Y%m%d%H%M%S')}",
                    source_file="demo.3mf",
                    project=project,
                    spool_id=demo_spools[0].id,
                    spool_brand=demo_spools[0].brand,
                    spool_material=demo_spools[0].material,
                    spool_color=demo_spools[0].color,
                    deducted_g=28.0,
                    remaining_before_g=648.0,
                    remaining_after_g=620.0,
                    undone=False,
                ),
                UsageHistory(
                    actor="help-demo",
                    mode="auto_file",
                    source_app="Bambu Studio",
                    batch_id=f"help-demo-{now.strftime('%Y%m%d%H%M%S')}-2",
                    source_file="demo_auto.3mf",
                    project=project,
                    spool_id=demo_spools[1].id,
                    spool_brand=demo_spools[1].brand,
                    spool_material=demo_spools[1].material,
                    spool_color=demo_spools[1].color,
                    deducted_g=40.0,
                    remaining_before_g=350.0,
                    remaining_after_g=310.0,
                    undone=False,
                ),
            ]
        )

        db.add_all(
            [
                DeviceSlotState(
                    project=project,
                    printer_name="P1S-01",
                    slot=1,
                    observed_brand=f"{prefix} Bambu",
                    observed_material="PLA",
                    observed_color="Black",
                    source="help-demo-seed",
                    observed_at=now,
                    updated_at=now,
                ),
                DeviceSlotState(
                    project=project,
                    printer_name="P1S-01",
                    slot=2,
                    observed_brand=f"{prefix} Bambu",
                    observed_material="PETG",
                    observed_color="White",
                    source="help-demo-seed",
                    observed_at=now,
                    updated_at=now,
                ),
            ]
        )

        db.commit()

    print(f"Seed complete for project='{project}' with prefix='{prefix}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
