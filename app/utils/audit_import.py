from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditLog, ImportMappingProfile


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def request_actor(request: Optional[Request]) -> Optional[str]:
    if request is None or request.client is None:
        return None
    host = str(request.client.host or "").strip()
    return host or None


def to_json_text(payload: object) -> Optional[str]:
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None


def audit_log(
    db: Session,
    project: str,
    action: str,
    *,
    request: Optional[Request] = None,
    actor: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[object] = None,
    details: Optional[dict] = None,
) -> None:
    resolved_actor = actor or request_actor(request)
    db.add(
        AuditLog(
            project=project,
            actor=(str(resolved_actor).strip()[:120] if resolved_actor else None),
            action=str(action or "").strip()[:80] or "unknown",
            entity_type=(str(entity_type).strip()[:80] if entity_type else None),
            entity_id=(str(entity_id).strip()[:120] if entity_id is not None else None),
            details_json=to_json_text(details),
        )
    )


def normalize_col_name(raw: object) -> str:
    return str(raw or "").strip().lower().replace(" ", "_")


def default_import_alias_map() -> dict[str, str]:
    return {
        "brand": "brand",
        "marke": "brand",
        "material": "material",
        "color": "color",
        "farbe": "color",
        "weight_g": "weight_g",
        "gewicht": "weight_g",
        "remaining_g": "remaining_g",
        "restmenge": "remaining_g",
        "low_stock_threshold_g": "low_stock_threshold_g",
        "niedrigbestand_schwelle_g": "low_stock_threshold_g",
        "price": "price",
        "preis": "price",
        "location": "location",
        "lagerort": "location",
    }


def load_import_mapping_profile(db: Session, project: str, profile_name: Optional[str]) -> Optional[dict[str, str]]:
    key = str(profile_name or "").strip()
    if not key:
        return None
    profile = (
        db.query(ImportMappingProfile)
        .filter(ImportMappingProfile.project == project, ImportMappingProfile.name == key)
        .first()
    )
    if profile is None:
        return None
    try:
        payload = json.loads(profile.mapping_json or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    normalized: dict[str, str] = {}
    for source, target in payload.items():
        src = normalize_col_name(source)
        dst = str(target or "").strip()
        if src and dst:
            normalized[src] = dst
    return normalized or None


def save_import_mapping_profile(db: Session, project: str, profile_name: str, mapping: dict[str, str]) -> None:
    key = str(profile_name or "").strip()
    if not key:
        return
    normalized: dict[str, str] = {}
    for source, target in (mapping or {}).items():
        src = normalize_col_name(source)
        dst = str(target or "").strip()
        if src and dst:
            normalized[src] = dst
    if not normalized:
        return

    profile = (
        db.query(ImportMappingProfile)
        .filter(ImportMappingProfile.project == project, ImportMappingProfile.name == key)
        .first()
    )
    payload = to_json_text(normalized) or "{}"
    if profile is None:
        db.add(
            ImportMappingProfile(
                project=project,
                name=key[:120],
                mapping_json=payload,
            )
        )
        return
    profile.mapping_json = payload
    profile.updated_at = _utcnow()
