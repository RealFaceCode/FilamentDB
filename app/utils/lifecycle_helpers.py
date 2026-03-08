from __future__ import annotations

from typing import Callable, Optional

from app.models import Spool


def normalize_lifecycle_status(value: Optional[str], allowed_values: list[str]) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return raw if raw in allowed_values else "new"


def lifecycle_status_options(status_values: list[str], translate: Callable[[str], str]) -> list[dict]:
    return [
        {
            "value": status,
            "label": translate(f"lifecycle_{status}"),
        }
        for status in status_values
    ]


def enforce_empty_lifecycle(spool: Optional[Spool]) -> None:
    if spool is None:
        return
    remaining = float(spool.remaining_g or 0.0)
    lifecycle_empty = str(spool.lifecycle_status or "").strip().lower() == "empty"
    if lifecycle_empty and remaining > 0:
        spool.remaining_g = 0.0
        remaining = 0.0

    if remaining <= 0:
        spool.in_use = False
        spool.lifecycle_status = "empty"
        spool.storage_sub_location_id = None
        spool.location = None
