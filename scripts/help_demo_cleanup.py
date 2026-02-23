from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.models import DeviceSlotState, Spool, UsageHistory


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cleanup demo data created for help screenshots")
    parser.add_argument("--project", default="private", choices=["private", "business"], help="Target project")
    parser.add_argument("--prefix", default="[HELP-DEMO]", help="Marker prefix used during seed")
    parser.add_argument("--confirm", action="store_true", help="Required safety flag")
    parser.add_argument("--force", action="store_true", help="Allow running when APP_ENV=production (not recommended)")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if not args.confirm:
        print("Refusing to cleanup without --confirm")
        return 2

    if not args.force and str(os.getenv("APP_ENV", "development")).strip().lower() == "production":
        print("Refusing to cleanup in production without --force")
        return 2

    project = args.project
    prefix = str(args.prefix or "").strip() or "[HELP-DEMO]"

    with SessionLocal() as db:
        spool_ids = [
            row.id
            for row in db.query(Spool.id)
            .filter(Spool.project == project, Spool.brand.like(f"{prefix}%"))
            .all()
        ]

        deleted_usage = 0
        deleted_slot_state = 0
        deleted_spools = 0

        if spool_ids:
            deleted_usage += (
                db.query(UsageHistory)
                .filter(UsageHistory.project == project, UsageHistory.spool_id.in_(spool_ids))
                .delete(synchronize_session=False)
            )
            deleted_spools += (
                db.query(Spool)
                .filter(Spool.project == project, Spool.id.in_(spool_ids))
                .delete(synchronize_session=False)
            )

        deleted_usage += (
            db.query(UsageHistory)
            .filter(UsageHistory.project == project, UsageHistory.actor == "help-demo")
            .delete(synchronize_session=False)
        )

        deleted_slot_state += (
            db.query(DeviceSlotState)
            .filter(DeviceSlotState.project == project, DeviceSlotState.source == "help-demo-seed")
            .delete(synchronize_session=False)
        )

        db.commit()

    print(
        f"Cleanup complete for project='{project}': deleted_spools={deleted_spools}, "
        f"deleted_usage={deleted_usage}, deleted_slot_state={deleted_slot_state}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
