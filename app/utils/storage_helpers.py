from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Spool, StorageArea, StorageSubLocation

_STORAGE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def normalize_storage_code(value: Optional[str]) -> str:
    return str(value or "").strip().upper()


def normalize_storage_area_code(value: Optional[str]) -> Optional[str]:
    code = normalize_storage_code(value)
    return code if _STORAGE_CODE_RE.match(code) else None


def normalize_storage_sub_code(value: Optional[str]) -> Optional[str]:
    code = normalize_storage_code(value)
    return code if _STORAGE_CODE_RE.match(code) else None


def normalize_storage_sub_location_id(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def storage_path_code(area_code: str, sub_code: str) -> str:
    return f"{area_code}/{sub_code}"


def storage_location_options(db: Session, project: str) -> list[dict]:
    location_filters = [StorageSubLocation.project == project, StorageArea.project == project]

    rows = (
        db.query(StorageSubLocation, StorageArea)
        .join(StorageArea, StorageArea.id == StorageSubLocation.area_id)
        .filter(*location_filters)
        .order_by(StorageArea.code.asc(), StorageSubLocation.code.asc())
        .all()
    )
    options: list[dict] = []
    for sub, area in rows:
        label = sub.path_code
        if sub.name:
            label = f"{label} · {sub.name}"
        elif area.name:
            label = f"{label} · {area.name}"
        options.append(
            {
                "id": sub.id,
                "area_code": area.code,
                "area_name": area.name,
                "sub_code": sub.code,
                "sub_name": sub.name,
                "path_code": sub.path_code,
                "label": label,
            }
        )
    return options


def storage_location_map_by_id(db: Session, project: str, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    filters = [
        StorageSubLocation.project == project,
        StorageSubLocation.id.in_(ids),
    ]

    rows = (
        db.query(StorageSubLocation.id, StorageSubLocation.path_code)
        .filter(*filters)
        .all()
    )
    return {int(location_id): path_code for location_id, path_code in rows}


def spool_location_display(spool: Spool, storage_path_map: dict[int, str]) -> str:
    if spool.storage_sub_location_id and spool.storage_sub_location_id in storage_path_map:
        return storage_path_map[spool.storage_sub_location_id]
    return str(spool.location or "").strip() or "-"


def resolve_storage_sub_location(
    db: Session,
    project: str,
    storage_sub_location_id: Optional[str],
) -> tuple[Optional[StorageSubLocation], Optional[str]]:
    normalized_id = normalize_storage_sub_location_id(storage_sub_location_id)
    if normalized_id is None:
        return None, None

    filters = [
        StorageSubLocation.project == project,
        StorageSubLocation.id == normalized_id,
    ]

    sub_location = (
        db.query(StorageSubLocation)
        .filter(*filters)
        .first()
    )
    if sub_location is None:
        return None, "storage_location_invalid"
    return sub_location, None
