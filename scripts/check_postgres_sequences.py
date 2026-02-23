from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import engine


def main() -> int:
    driver = str(getattr(engine.url, "drivername", ""))
    if not driver.startswith("postgresql"):
        print("sequence-check-skipped:non-postgresql")
        return 0

    targets = (("spools", "id"), ("usage_history", "id"))
    drift_fixed: list[tuple[str, str, int, int]] = []

    with engine.begin() as conn:
        for table_name, column_name in targets:
            sequence_name = conn.execute(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": table_name, "column_name": column_name},
            ).scalar()
            if not sequence_name:
                continue

            max_id = conn.execute(
                text(f'SELECT COALESCE(MAX("{column_name}"), 0) FROM "{table_name}"')
            ).scalar() or 0

            row = conn.execute(text(f"SELECT last_value, is_called FROM {sequence_name}")).first()
            if not row:
                continue

            last_value, is_called = row
            current_next = int(last_value) + (1 if bool(is_called) else 0)
            expected_next = int(max_id) + 1

            if current_next < expected_next:
                conn.execute(
                    text("SELECT setval(:sequence_name, :new_value, false)"),
                    {"sequence_name": sequence_name, "new_value": expected_next},
                )
                drift_fixed.append((table_name, sequence_name, current_next, expected_next))

    if drift_fixed:
        print("sequence-drift-fixed")
        for table_name, sequence_name, current_next, expected_next in drift_fixed:
            print(f"{table_name}: {sequence_name} {current_next} -> {expected_next}")
    else:
        print("sequence-ok")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
