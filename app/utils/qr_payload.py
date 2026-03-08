from __future__ import annotations

import re
from typing import Optional

from .storage_helpers import (
    normalize_storage_area_code,
    normalize_storage_sub_code,
    storage_path_code,
)


def extract_spool_id_from_qr_payload(payload: Optional[str]) -> Optional[int]:
    raw = str(payload or "").strip()
    if not raw:
        return None

    match = re.search(r"spool:(\d+):", raw, flags=re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except Exception:
            return None

    match = re.search(r"\bSP-(\d+)\b", raw, flags=re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except Exception:
            return None

    if raw.isdigit():
        try:
            return int(raw)
        except Exception:
            return None
    return None


def extract_location_path_from_qr_payload(payload: Optional[str], project: str) -> Optional[str]:
    raw = str(payload or "").strip()
    if not raw:
        return None

    match = re.search(r"location:([a-z0-9_-]+):([^\s]+)", raw, flags=re.IGNORECASE)
    if not match:
        return None

    project_key = str(match.group(1) or "").strip().lower()
    if project_key != str(project or "").strip().lower():
        return None

    path = str(match.group(2) or "").strip()
    if "/" not in path:
        return None

    area_raw, sub_raw = path.split("/", 1)
    area_code = normalize_storage_area_code(area_raw)
    sub_code = normalize_storage_sub_code(sub_raw)
    if area_code is None or sub_code is None:
        return None
    return storage_path_code(area_code, sub_code)


def extract_printer_id_from_qr_payload(payload: Optional[str], project: str) -> Optional[int]:
    raw = str(payload or "").strip()
    if not raw:
        return None

    match = re.search(r"printer:([a-z0-9_-]+):(\d+):", raw, flags=re.IGNORECASE)
    if not match:
        return None

    project_key = str(match.group(1) or "").strip().lower()
    if project_key != str(project or "").strip().lower():
        return None

    try:
        return int(match.group(2))
    except Exception:
        return None
