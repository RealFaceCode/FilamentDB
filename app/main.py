from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from io import BytesIO
import tempfile
from typing import Optional
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from fastapi import FastAPI, Depends, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, text, case
from sqlalchemy.exc import IntegrityError

from .db import Base, engine, get_db, SessionLocal
from .models import (
    Spool,
    UsageHistory,
    UsageBatchContext,
    DeviceSlotState,
    Printer,
    SupplyCategory,
    SupplyItem,
    AppSetting,
    AuditLog,
    ImportMappingProfile,
    StorageArea,
    StorageSubLocation,
)
from .utils.app_constants import LABEL_LAYOUTS, TRANSLATIONS
from .utils.config_helpers import (
    env_csv_list as _env_csv_list,
    env_truthy as _env_truthy,
    get_configured_lan_host as _get_configured_lan_host,
    merge_allowed_hosts as _merge_allowed_hosts,
    resolve_mobile_entry_url as _resolve_mobile_entry_url,
)
from .utils.audit_import import (
    audit_log as _audit_log,
    default_import_alias_map as _default_import_alias_map,
    load_import_mapping_profile as _load_import_mapping_profile,
    normalize_col_name as _normalize_col_name,
    save_import_mapping_profile as _save_import_mapping_profile,
)
from .utils.analysis_helpers import (
    analysis_printer_slot_usage as _analysis_printer_slot_usage,
    analysis_top_usage as _analysis_top_usage,
    analysis_usage_and_cost_in_period as _analysis_usage_and_cost_in_period,
    analysis_usage_cost_trend as _analysis_usage_cost_trend,
    bounded_int as _bounded_int,
)
from .utils.storage_helpers import (
    normalize_storage_area_code as _normalize_storage_area_code,
    normalize_storage_code as _normalize_storage_code,
    normalize_storage_sub_code as _normalize_storage_sub_code,
    normalize_storage_sub_location_id as _normalize_storage_sub_location_id,
    resolve_storage_sub_location as _resolve_storage_sub_location,
    spool_location_display as _spool_location_display,
    storage_location_map_by_id as _storage_location_map_by_id,
    storage_location_options as _storage_location_options,
    storage_path_code as _storage_path_code,
)
from .utils.lifecycle_helpers import (
    enforce_empty_lifecycle as _enforce_empty_lifecycle,
    lifecycle_status_options as _build_lifecycle_status_options,
    normalize_lifecycle_status as _normalize_lifecycle_status_value,
)
from .utils.backup_runtime import (
    backup_mode as _backup_mode,
    build_backup_filename as _build_backup_filename,
    clamp_int as _clamp_int,
    cleanup_temp_file as _cleanup_temp_file,
    configure_backup_runtime,
    create_backup_snapshot as _create_backup_snapshot,
    ensure_backup_storage_dir as _ensure_backup_storage_dir,
    is_postgresql_database as _is_postgresql_database,
    is_sqlite_database as _is_sqlite_database,
    list_backup_files as _list_backup_files,
    pg_tools_available as _pg_tools_available,
    postgres_connection_args as _postgres_connection_args,
    postgres_subprocess_env as _postgres_subprocess_env,
    resolve_backup_file_path as _resolve_backup_file_path,
    restore_from_backup_path as _restore_from_backup_path,
    run_postgres_backup_to_path as _run_postgres_backup_to_path,
    run_sqlite_backup_to_path as _run_sqlite_backup_to_path,
    sqlite_db_path as _sqlite_db_path,
)
from .utils.backup_policy import (
    build_backup_context as _build_backup_context_impl,
    delete_all_database_rows as _delete_all_database_rows_impl,
    load_backup_auto_settings as _load_backup_auto_settings_impl,
    prune_old_backup_files as _prune_old_backup_files_impl,
    save_backup_auto_settings as _save_backup_auto_settings_impl,
)
from .utils.slot_status_helpers import (
    build_slot_remap_plan as _build_slot_remap_plan_impl,
    build_slot_status_rows as _build_slot_status_rows_impl,
    extract_slot_state_entries as _extract_slot_state_entries_impl,
    migrate_slot_format_to_canonical as _migrate_slot_format_to_canonical_impl,
    summarize_slot_data_freshness as _summarize_slot_data_freshness_impl,
    upsert_slot_state_entries as _upsert_slot_state_entries_impl,
)
from .utils.qr_payload import (
    extract_location_path_from_qr_payload as _extract_location_path_from_qr_payload,
    extract_printer_id_from_qr_payload as _extract_printer_id_from_qr_payload,
    extract_spool_id_from_qr_payload as _extract_spool_id_from_qr_payload,
)
from .utils.formatting import (
    format_currency_text,
    format_length_compact,
    format_length_display,
    format_length_text,
    format_number_compact,
    format_weight_display,
    format_weight_text,
)
from .utils.printer_ams import (
    compose_ams_global_slot as _compose_ams_global_slot,
    equivalent_ams_slots as _equivalent_ams_slots,
    find_ams_slot_conflict as _find_ams_slot_conflict,
    first_present_value as _first_present_value,
    format_printer_temperatures as _format_printer_temperatures,
    humanize_observed_color as _humanize_observed_color,
    infer_ams_slot_parts as _infer_ams_slot_parts,
    normalize_ams_raw_id as _normalize_ams_raw_id,
    normalize_ams_slot as _normalize_ams_slot,
    normalize_ams_slot_canonical as _normalize_ams_slot_canonical,
    normalize_printer_name as _normalize_printer_name,
    normalize_printer_port as _normalize_printer_port,
    normalize_printer_serial as _normalize_printer_serial,
    normalize_printer_status as _normalize_printer_status,
    parse_ams_name_mapping as _parse_ams_name_mapping,
    parse_slot_tokens as _parse_slot_tokens,
    resolve_ams_label as _resolve_ams_label,
    resolve_ams_slots as _resolve_ams_slots,
    resolve_ams_unit as _resolve_ams_unit,
    resolve_or_create_printer as _resolve_or_create_printer,
    serialize_ams_name_mapping as _serialize_ams_name_mapping,
    serialize_ams_slots as _serialize_ams_slots,
    slot_scoped_spools as _slot_scoped_spools,
)
from .utils.usage_parsing import (
    matches_any as _matches_any,
    parse_optional_bool as _parse_optional_bool,
    parse_optional_float as _parse_optional_float,
    parse_usage_from_print_file as _parse_usage_from_print_file,
)
from .utils.qr import generate_qr_png


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _run_startup_tasks()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

PRESETS_PATH = Path("app/data/presets.json")
COLOR_MAP_PATH = Path("app/data/color_map.json")
SETTINGS_COOKIE_MAX_AGE = 60 * 60 * 24 * 365
VALID_THEMES = {"light", "dark", "system"}
PROJECT_OPTIONS = ["private", "business"]
DEFAULT_PROJECT = "private"
AUTO_REFRESH_OPTIONS = [0, 5, 10, 30]
DEFAULT_AUTO_REFRESH_SECONDS = 5
DEFAULT_LABEL_LAYOUT = "a4_3x8_63_5x33_9"
DEFAULT_LABEL_PRINT_MODE = "sheet"
DEFAULT_LABEL_ORIENTATION = "horizontal"
LABEL_CONTENT_SETTING_KEY = "label_content"
LABEL_TARGET_SETTING_KEY = "label_target"
CUSTOM_LABEL_LAYOUTS_SETTING_KEY = "custom_label_layouts"
CUSTOM_LABEL_LAYOUT_SETTING_PREFIX = "custom_label_layout:"
CUSTOM_LABEL_LAYOUT_DELETED_PREFIX = "custom_label_layout_deleted:"
PRINTABLE_WIDTH_MM = 190.0
LABEL_GRID_GAP_MM = 4.0
LIFECYCLE_STATUS_VALUES = ["new", "opened", "dry_stored", "humidity_risk", "drying", "brittle", "empty", "recycled", "archived"]
BACKUP_STORAGE_DIR = Path(os.getenv("BACKUP_STORAGE_DIR", "/home/appuser/backups")).resolve()
BACKUP_AUTO_ENABLED_SETTING_KEY = "backup_auto_enabled"
BACKUP_AUTO_INTERVAL_HOURS_SETTING_KEY = "backup_auto_interval_hours"
BACKUP_AUTO_RETENTION_DAYS_SETTING_KEY = "backup_auto_retention_days"
BACKUP_AUTO_LAST_RUN_AT_SETTING_KEY = "backup_auto_last_run_at"
BACKUP_MIN_INTERVAL_HOURS = 1
BACKUP_MAX_INTERVAL_HOURS = 168
BACKUP_MIN_RETENTION_DAYS = 1
BACKUP_MAX_RETENTION_DAYS = 365
BACKUP_RESET_CONFIRM_PHRASE = "DELETE ALL"
BACKUP_LOCKFILE_NAME = ".backup.lock"
BACKUP_LOCK_STALE_SECONDS = 10 * 60
BACKUP_AUTO_CHECK_COOLDOWN_SECONDS = 30
_AUTO_BACKUP_CHECK_LOCK = threading.Lock()
_AUTO_BACKUP_LAST_CHECK_AT = 0.0

configure_backup_runtime(engine, BACKUP_STORAGE_DIR)


APP_ENV = str(os.getenv("APP_ENV", "development")).strip().lower()
LOG_LEVEL = str(os.getenv("LOG_LEVEL", "info")).strip().upper()
DEFAULT_COOKIE_SECURE = APP_ENV == "production"
COOKIE_SECURE_RAW = os.getenv("COOKIE_SECURE")
COOKIE_SECURE_EXPLICIT = COOKIE_SECURE_RAW is not None
COOKIE_SECURE = _env_truthy(COOKIE_SECURE_RAW, default=DEFAULT_COOKIE_SECURE)
COOKIE_HTTPONLY = _env_truthy(os.getenv("COOKIE_HTTPONLY"), default=True)
ENABLE_BASIC_AUTH = _env_truthy(os.getenv("ENABLE_BASIC_AUTH"), default=False)
BASIC_AUTH_USERNAME = str(os.getenv("BASIC_AUTH_USERNAME", "")).strip()
BASIC_AUTH_PASSWORD = str(os.getenv("BASIC_AUTH_PASSWORD", "")).strip()
CSRF_PROTECT = _env_truthy(os.getenv("CSRF_PROTECT"), default=True)
STRICT_CSRF_CHECK = _env_truthy(os.getenv("STRICT_CSRF_CHECK"), default=False)
FORCE_HTTPS_REDIRECT = _env_truthy(os.getenv("FORCE_HTTPS_REDIRECT"), default=False)
configured_lan_host_for_security, _ = _get_configured_lan_host("8443")
allowed_hosts_config = _env_csv_list(os.getenv("ALLOWED_HOSTS"), ["localhost", "127.0.0.1", "testserver"])
if configured_lan_host_for_security:
    allowed_hosts_config.append(configured_lan_host_for_security)
ALLOWED_HOSTS = _merge_allowed_hosts(allowed_hosts_config)
trusted_origins_config = _env_csv_list(os.getenv("TRUSTED_ORIGINS"), [])
if configured_lan_host_for_security:
    trusted_origins_config.append(f"https://{configured_lan_host_for_security}:8443")
TRUSTED_ORIGINS = set(trusted_origins_config)
MAX_UPLOAD_MB = max(1, int(float(str(os.getenv("MAX_UPLOAD_MB", "25")).strip() or "25")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
SLOT_STATE_STALE_MINUTES = max(1, int(float(str(os.getenv("SLOT_STATE_STALE_MINUTES", "10")).strip() or "10")))
PUBLIC_PATH_PREFIXES = (
    "/static/",
    "/healthz",
)
CSRF_EXEMPT_PATH_PREFIXES = (
    "/api/",
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("filament_db")

if ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
if FORCE_HTTPS_REDIRECT:
    app.add_middleware(HTTPSRedirectMiddleware)

templates.env.globals["format_weight_display"] = format_weight_display
templates.env.globals["format_weight_text"] = format_weight_text
templates.env.globals["format_length_display"] = format_length_display
templates.env.globals["format_length_text"] = format_length_text
templates.env.globals["format_number_compact"] = format_number_compact
templates.env.globals["format_currency_text"] = format_currency_text
templates.env.globals["format_length_compact"] = format_length_compact


def load_presets():
    if PRESETS_PATH.exists():
        with PRESETS_PATH.open("r", encoding="utf-8") as f:
            presets = json.load(f)
            if "materials" not in presets and "material_groups" in presets:
                presets["materials"] = [
                    item
                    for group in presets.get("material_groups", [])
                    for item in group.get("items", [])
                ]
            presets.setdefault("colors", [])
            presets.setdefault("brands", [])
            presets.setdefault("weights_g", [])
            presets.setdefault("material_groups", [])
            presets.setdefault("low_stock_thresholds", {})
            presets.setdefault("material_total_thresholds", {})
            presets.setdefault("custom_label_layouts", {})
            return presets
    return {
        "brands": [],
        "materials": [],
        "material_groups": [],
        "colors": [],
        "weights_g": [],
        "low_stock_thresholds": {},
        "material_total_thresholds": {},
        "custom_label_layouts": {},
    }


def load_color_map():
    if COLOR_MAP_PATH.exists():
        with COLOR_MAP_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_presets(presets: dict):
    with PRESETS_PATH.open("w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def save_color_map(color_map: dict):
    with COLOR_MAP_PATH.open("w", encoding="utf-8") as f:
        json.dump(color_map, f, ensure_ascii=False, indent=2)

def _run_startup_tasks() -> None:
    Base.metadata.create_all(bind=engine)
    if _is_postgresql_database():
        _sync_postgres_id_sequences()
        return

    if not _is_sqlite_database():
        return

    _apply_legacy_sqlite_schema_patches()


def _apply_legacy_sqlite_schema_patches() -> None:
    with engine.begin() as conn:
        try:
            spool_columns = {
                row[1] for row in conn.execute(text("PRAGMA table_info(spools)")).fetchall()
            }
        except Exception:
            spool_columns = set()

        if spool_columns and "low_stock_threshold_g" not in spool_columns:
            conn.execute(text("ALTER TABLE spools ADD COLUMN low_stock_threshold_g FLOAT"))
        if spool_columns and "project" not in spool_columns:
            conn.execute(text("ALTER TABLE spools ADD COLUMN project VARCHAR(40) DEFAULT 'private'"))
            conn.execute(text("UPDATE spools SET project = 'private' WHERE project IS NULL OR TRIM(project) = ''"))
        if spool_columns and "storage_sub_location_id" not in spool_columns:
            conn.execute(text("ALTER TABLE spools ADD COLUMN storage_sub_location_id INTEGER"))

        try:
            columns = {
                row[1] for row in conn.execute(text("PRAGMA table_info(usage_history)")).fetchall()
            }
        except Exception:
            columns = set()

        if columns and "batch_id" not in columns:
            conn.execute(text("ALTER TABLE usage_history ADD COLUMN batch_id VARCHAR(64)"))
        if columns and "source_app" not in columns:
            conn.execute(text("ALTER TABLE usage_history ADD COLUMN source_app VARCHAR(120)"))
        if columns and "undone" not in columns:
            conn.execute(text("ALTER TABLE usage_history ADD COLUMN undone BOOLEAN DEFAULT 0"))
        if columns and "undone_at" not in columns:
            conn.execute(text("ALTER TABLE usage_history ADD COLUMN undone_at DATETIME"))
        if columns and "project" not in columns:
            conn.execute(text("ALTER TABLE usage_history ADD COLUMN project VARCHAR(40) DEFAULT 'private'"))
            conn.execute(text("UPDATE usage_history SET project = 'private' WHERE project IS NULL OR TRIM(project) = ''"))


def _sync_postgres_id_sequences() -> None:
    sequence_targets = (
        ("spools", "id"),
        ("usage_history", "id"),
        ("supply_categories", "id"),
        ("supply_items", "id"),
    )
    with engine.begin() as conn:
        for table_name, column_name in sequence_targets:
            try:
                conn.execute(
                    text(
                        """
                        SELECT setval(
                            pg_get_serial_sequence(:table_name, :column_name),
                            COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1,
                            false
                        )
                        """.format(table_name=table_name)
                    ),
                    {"table_name": table_name, "column_name": column_name},
                )
            except Exception as exc:
                logger.warning("Could not sync PostgreSQL sequence for %s.%s: %s", table_name, column_name, exc)


def _ensure_postgres_spool_sequence_when_empty(db: Session) -> None:
    if not _is_postgresql_database():
        return
    try:
        spool_count = db.query(func.count(Spool.id)).scalar() or 0
        if int(spool_count) > 0:
            return
        db.execute(
            text(
                """
                SELECT setval(
                    pg_get_serial_sequence('spools', 'id'),
                    1,
                    false
                )
                """
            )
        )
        db.flush()
    except Exception as exc:
        logger.warning("Could not ensure PostgreSQL sequence for spools.id: %s", exc)


def get_lang(request: Request) -> str:
    lang = (
        request.query_params.get("lang")
        or request.cookies.get("lang")
        or _load_setting_from_db("lang")
    )
    if lang not in TRANSLATIONS:
        lang = "de"
    return lang


def _normalize_email(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _hash_secret_value(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _base_project_preference(request: Request) -> str:
    candidate = request.query_params.get("project") or request.cookies.get("project") or _load_setting_from_db("project")
    return _normalize_project(candidate)


def _extract_base_project_from_scope(project_scope: str) -> str:
    return _normalize_project(project_scope)


def _model_scope_filters(model, project: str) -> list:
    filters = []
    if hasattr(model, "project"):
        filters.append(getattr(model, "project") == project)
    return filters


def _scoped_query(db: Session, model, project: str):
    return db.query(model).filter(*_model_scope_filters(model, project))


def get_current_user(_: Request) -> None:
    return None


def get_theme(request: Request) -> str:
    theme = request.cookies.get("theme") or _load_setting_from_db("theme") or "system"
    if theme not in VALID_THEMES:
        return "system"
    return theme


def _normalize_auto_refresh_seconds(value: Optional[object]) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_AUTO_REFRESH_SECONDS
    return parsed if parsed in AUTO_REFRESH_OPTIONS else DEFAULT_AUTO_REFRESH_SECONDS


def get_auto_refresh_seconds(request: Request) -> int:
    raw = request.cookies.get("auto_refresh_seconds") or _load_setting_from_db("auto_refresh_seconds")
    return _normalize_auto_refresh_seconds(raw)


def _normalize_privacy_blur(value: Optional[object]) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_privacy_blur(request: Request) -> bool:
    raw = request.cookies.get("privacy_blur")
    if raw is None:
        raw = _load_setting_from_db("privacy_blur")
    return _normalize_privacy_blur(raw)


def _normalize_project(project: Optional[str]) -> str:
    candidate = str(project or "").strip().lower()
    return candidate if candidate in PROJECT_OPTIONS else DEFAULT_PROJECT


def get_project(request: Request) -> str:
    return _base_project_preference(request)


def _effective_project_for_request(request: Request, project_override: Optional[str] = None) -> str:
    base = _normalize_project(project_override) if project_override is not None else _base_project_preference(request)
    return base


@app.get("/healthz")
def healthz():
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "ok": True,
            "status": "ok",
            "database": "ok",
            "timestamp": now_iso,
        }
    except Exception as exc:
        logger.exception("Healthcheck DB probe failed")
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "status": "degraded",
                "database": "error",
                "error": str(exc.__class__.__name__),
                "timestamp": now_iso,
            },
        )


def _get_custom_label_layouts() -> dict[str, dict]:
    raw_layouts: dict[str, dict] = {}

    presets = load_presets()
    presets_layouts = presets.get("custom_label_layouts") or {}
    if isinstance(presets_layouts, dict):
        for key, value in presets_layouts.items():
            if isinstance(value, dict):
                raw_layouts[str(key)] = value

    db_layouts = _load_custom_label_layouts_from_db()
    if isinstance(db_layouts, dict):
        for key, value in db_layouts.items():
            if isinstance(value, dict):
                raw_layouts[str(key)] = value

    deleted_layout_keys: set[str] = set()
    db = SessionLocal()
    try:
        rows = (
            db.query(AppSetting)
            .filter(AppSetting.key.like(f"{CUSTOM_LABEL_LAYOUT_DELETED_PREFIX}%"))
            .all()
        )
        for row in rows:
            key = str(row.key or "")
            if not key.startswith(CUSTOM_LABEL_LAYOUT_DELETED_PREFIX):
                continue
            deleted_key = key[len(CUSTOM_LABEL_LAYOUT_DELETED_PREFIX):].strip()
            if deleted_key:
                deleted_layout_keys.add(deleted_key)
    except Exception:
        pass
    finally:
        db.close()

    for deleted_key in deleted_layout_keys:
        raw_layouts.pop(deleted_key, None)

    result: dict[str, dict] = {}
    for key, cfg in raw_layouts.items():
        layout_key = str(key or "").strip()
        if not layout_key:
            continue
        if not isinstance(cfg, dict):
            continue

        cell_w_mm = float(_parse_optional_float(cfg.get("cell_w_mm")) or 0)
        cell_h_mm = float(_parse_optional_float(cfg.get("cell_h_mm")) or 0)
        if cell_w_mm <= 0 or cell_h_mm <= 0:
            continue

        label_de = str(cfg.get("label_de") or layout_key).strip()
        label_en = str(cfg.get("label_en") or label_de).strip()
        result[layout_key] = {
            "label_de": label_de,
            "label_en": label_en,
            "cell_w_mm": round(cell_w_mm, 2),
            "cell_h_mm": round(cell_h_mm, 2),
            "is_custom": True,
        }
    return result


def _all_label_layouts() -> dict[str, dict]:
    merged = dict(LABEL_LAYOUTS)
    merged.update(_get_custom_label_layouts())
    return merged


def _get_label_layout_choices(lang: str, layouts: Optional[dict[str, dict]] = None) -> list[dict]:
    layouts_map = layouts or _all_label_layouts()
    choices: list[dict] = []
    for key, cfg in layouts_map.items():
        title = cfg.get("label_de") if lang == "de" else cfg.get("label_en")
        choices.append({"key": key, "title": str(title or key), "is_custom": bool(cfg.get("is_custom"))})
    return choices


def _normalize_label_layout(layout: Optional[str], layouts: Optional[dict[str, dict]] = None) -> str:
    layouts_map = layouts or _all_label_layouts()
    key = str(layout or "").strip()
    if key in layouts_map:
        return key
    if key == "sheet":
        return "a4_3x8_63_5x33_9"
    if key == "a4":
        return "a4_cards_2x5"
    return DEFAULT_LABEL_LAYOUT


def _resolve_label_layout_for_print(layout_cfg: dict) -> dict:
    cell_w_mm = float(_parse_optional_float(layout_cfg.get("cell_w_mm")) or 0)
    cell_h_mm = float(_parse_optional_float(layout_cfg.get("cell_h_mm")) or 0)
    if cell_w_mm <= 0:
        cell_w_mm = 63.5
    if cell_h_mm <= 0:
        cell_h_mm = 33.9

    explicit_columns = int(_parse_optional_float(layout_cfg.get("columns")) or 0)
    if explicit_columns >= 1:
        columns = min(8, explicit_columns)
    else:
        columns = int((PRINTABLE_WIDTH_MM + LABEL_GRID_GAP_MM) // (cell_w_mm + LABEL_GRID_GAP_MM))
        columns = max(1, min(8, columns))

    resolved = dict(layout_cfg)
    resolved["columns"] = columns
    resolved["cell_w_mm"] = round(cell_w_mm, 2)
    resolved["cell_h_mm"] = round(cell_h_mm, 2)
    return resolved


def _default_label_content_settings() -> dict[str, bool]:
    return {
        "show_spool_id": True,
        "show_brand": True,
        "show_material_color": True,
        "show_weight": False,
        "show_remaining": True,
        "show_location": False,
    }


def _normalize_label_print_mode(value: Optional[str]) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"sheet", "single"}:
        return mode
    return DEFAULT_LABEL_PRINT_MODE


def _normalize_label_orientation(value: Optional[str]) -> str:
    orientation = str(value or "").strip().lower()
    if orientation in {"horizontal", "vertical"}:
        return orientation
    return DEFAULT_LABEL_ORIENTATION


def _normalize_threshold_view(value: Optional[str]) -> str:
    view = str(value or "").strip().lower()
    if view in {"material-default", "spool", "material-total", "reorder"}:
        return view
    return "material-default"


def _thresholds_redirect(view: Optional[str]) -> RedirectResponse:
    normalized = _normalize_threshold_view(view)
    if normalized == "material-default":
        return RedirectResponse("/thresholds", status_code=303)
    return RedirectResponse(f"/thresholds?view={normalized}", status_code=303)


def _build_label_content_settings(overrides: Optional[dict[str, bool]] = None) -> dict[str, bool]:
    settings = _default_label_content_settings()
    if overrides:
        for key, value in overrides.items():
            if key in settings:
                settings[key] = bool(value)

    return settings


def _load_label_print_preferences(request: Request) -> dict:
    print_mode = _normalize_label_print_mode(
        request.cookies.get("label_print_mode") or _load_setting_from_db("label_print_mode")
    )
    label_orientation = _normalize_label_orientation(
        request.cookies.get("label_orientation") or _load_setting_from_db("label_orientation")
    )

    content_raw = request.cookies.get(LABEL_CONTENT_SETTING_KEY) or _load_setting_from_db(LABEL_CONTENT_SETTING_KEY)
    parsed_content: dict[str, bool] = {}
    if content_raw:
        try:
            decoded = json.loads(content_raw)
            if isinstance(decoded, dict):
                parsed_content = {str(k): bool(v) for k, v in decoded.items()}
        except Exception:
            parsed_content = {}

    return {
        "print_mode": print_mode,
        "label_orientation": label_orientation,
        "label_content": _build_label_content_settings(parsed_content),
    }


def _load_custom_label_layouts_from_db() -> dict[str, dict]:
    merged: dict[str, dict] = {}

    legacy_raw = _load_setting_from_db(CUSTOM_LABEL_LAYOUTS_SETTING_KEY)
    if legacy_raw:
        try:
            parsed = json.loads(legacy_raw)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if isinstance(value, dict):
                        merged[str(key)] = value
        except Exception:
            pass

    db = SessionLocal()
    try:
        rows = (
            db.query(AppSetting)
            .filter(AppSetting.key.like(f"{CUSTOM_LABEL_LAYOUT_SETTING_PREFIX}%"))
            .all()
        )
        for row in rows:
            key = str(row.key or "")
            if not key.startswith(CUSTOM_LABEL_LAYOUT_SETTING_PREFIX):
                continue
            layout_key = key[len(CUSTOM_LABEL_LAYOUT_SETTING_PREFIX):].strip()
            if not layout_key:
                continue
            try:
                parsed_value = json.loads(str(row.value or ""))
            except Exception:
                continue
            if isinstance(parsed_value, dict):
                merged[layout_key] = parsed_value
    except Exception:
        pass
    finally:
        db.close()

    return merged


def _save_label_print_preferences(response, print_mode: str, label_orientation: str, label_content: dict[str, bool]) -> None:
    mode = _normalize_label_print_mode(print_mode)
    orientation = _normalize_label_orientation(label_orientation)
    content = _build_label_content_settings(label_content)
    content_json = json.dumps(content, ensure_ascii=False)

    _set_cookie(response, "label_print_mode", mode)
    _set_cookie(response, "label_orientation", orientation)
    _set_cookie(response, LABEL_CONTENT_SETTING_KEY, content_json)

    _save_setting_to_db("label_print_mode", mode)
    _save_setting_to_db("label_orientation", orientation)
    _save_setting_to_db(LABEL_CONTENT_SETTING_KEY, content_json)


def _load_setting_from_db(key: str) -> Optional[str]:
    AppSetting.__table__.create(bind=engine, checkfirst=True)
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if not row:
            return None
        value = str(row.value or "").strip()
        return value or None
    except Exception:
        return None
    finally:
        db.close()


def _save_setting_to_db(key: str, value: str) -> None:
    AppSetting.__table__.create(bind=engine, checkfirst=True)
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = value
            row.updated_at = _utcnow()
        else:
            db.add(AppSetting(key=key, value=value))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _delete_setting_from_db(key: str) -> None:
    AppSetting.__table__.create(bind=engine, checkfirst=True)
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row is not None:
            db.delete(row)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _is_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _request_is_https(request: Optional[Request]) -> bool:
    if request is None:
        return False

    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_proto:
        return forwarded_proto == "https"

    return str(request.url.scheme or "").strip().lower() == "https"


def _cookie_secure_for_request(request: Optional[Request]) -> bool:
    if COOKIE_SECURE_EXPLICIT:
        return COOKIE_SECURE

    if APP_ENV == "production":
        return _request_is_https(request)

    return COOKIE_SECURE


def _set_cookie(response, key: str, value: str, max_age: int = SETTINGS_COOKIE_MAX_AGE, request: Optional[Request] = None) -> None:
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        samesite="lax",
        secure=_cookie_secure_for_request(request),
        httponly=COOKIE_HTTPONLY,
    )


def _is_public_path(path: str) -> bool:
    normalized = str(path or "").strip() or "/"
    for prefix in PUBLIC_PATH_PREFIXES:
        if prefix == "/":
            if normalized == "/":
                return True
            continue
        if normalized == prefix or normalized.startswith(prefix):
            return True
    return False


def _is_basic_auth_valid(authorization_header: Optional[str]) -> bool:
    if not ENABLE_BASIC_AUTH:
        return True
    if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
        return False

    header = str(authorization_header or "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    if ":" not in decoded:
        return False
    username, password = decoded.split(":", 1)
    return hmac.compare_digest(username, BASIC_AUTH_USERNAME) and hmac.compare_digest(password, BASIC_AUTH_PASSWORD)


def _is_csrf_safe_request(request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if any(request.url.path.startswith(prefix) for prefix in CSRF_EXEMPT_PATH_PREFIXES):
        return True

    request_host = request.headers.get("host", request.url.netloc)
    allowed = {f"{request.url.scheme}://{request_host}", *TRUSTED_ORIGINS}

    origin = str(request.headers.get("origin") or "").strip()
    referer = str(request.headers.get("referer") or "").strip()
    if origin:
        return origin in allowed
    if referer:
        parsed = urlparse(referer)
        referer_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        return referer_origin in allowed
    return not STRICT_CSRF_CHECK


def _read_upload_limited(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[Optional[bytes], bool]:
    payload = file.file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        return None, True
    return payload, False


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    request.state.current_user = None

    if ENABLE_BASIC_AUTH and not _is_public_path(request.url.path):
        if not _is_basic_auth_valid(request.headers.get("authorization")):
            return PlainTextResponse(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="FilamentDB"'},
            )

    if CSRF_PROTECT and not _is_csrf_safe_request(request):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "csrf_failed"}, status_code=403)
        return PlainTextResponse("CSRF validation failed", status_code=403)

    if request.method == "GET" and not request.url.path.startswith("/static/"):
        try:
            _run_auto_backup_if_due()
        except Exception:
            pass

    return await call_next(request)


def _normalize_next_url(next_url: Optional[str]) -> str:
    target = str(next_url or "").strip()
    if not target.startswith("/"):
        return "/"
    return target


def _normalize_lifecycle_status(value: Optional[str]) -> str:
    return _normalize_lifecycle_status_value(value, LIFECYCLE_STATUS_VALUES)


def _lifecycle_status_options(lang: str) -> list[dict]:
    return _build_lifecycle_status_options(LIFECYCLE_STATUS_VALUES, t_factory(lang))


def _spool_status_key(spool: Spool) -> str:
    remaining = float(spool.remaining_g or 0.0)
    if bool(spool.in_use):
        return "in_use"

    presets = load_presets()
    material_thresholds = _load_material_thresholds(presets)
    threshold = _effective_low_stock_threshold(spool, material_thresholds)
    if threshold is not None and remaining <= float(threshold):
        return "low_stock"
    return "idle"


def t_factory(lang: str):
    def _t(key: str):
        return TRANSLATIONS.get(lang, TRANSLATIONS["de"]).get(key, key)

    return _t


def _load_backup_auto_settings() -> dict[str, object]:
    return _load_backup_auto_settings_impl(
        load_setting_fn=_load_setting_from_db,
        is_truthy_fn=_is_truthy,
        clamp_int_fn=_clamp_int,
        enabled_key=BACKUP_AUTO_ENABLED_SETTING_KEY,
        interval_key=BACKUP_AUTO_INTERVAL_HOURS_SETTING_KEY,
        retention_key=BACKUP_AUTO_RETENTION_DAYS_SETTING_KEY,
        last_run_key=BACKUP_AUTO_LAST_RUN_AT_SETTING_KEY,
        min_interval_hours=BACKUP_MIN_INTERVAL_HOURS,
        max_interval_hours=BACKUP_MAX_INTERVAL_HOURS,
        min_retention_days=BACKUP_MIN_RETENTION_DAYS,
        max_retention_days=BACKUP_MAX_RETENTION_DAYS,
    )


def _save_backup_auto_settings(enabled: bool, interval_hours: int, retention_days: int) -> None:
    _save_backup_auto_settings_impl(
        save_setting_fn=_save_setting_to_db,
        enabled_key=BACKUP_AUTO_ENABLED_SETTING_KEY,
        interval_key=BACKUP_AUTO_INTERVAL_HOURS_SETTING_KEY,
        retention_key=BACKUP_AUTO_RETENTION_DAYS_SETTING_KEY,
        enabled=enabled,
        interval_hours=interval_hours,
        retention_days=retention_days,
    )


def _prune_old_backup_files(mode: str, retention_days: int) -> int:
    return _prune_old_backup_files_impl(
        list_files_fn=_list_backup_files,
        resolve_file_fn=_resolve_backup_file_path,
        utcnow_fn=_utcnow,
        mode=mode,
        retention_days=retention_days,
        min_retention_days=BACKUP_MIN_RETENTION_DAYS,
    )


def _acquire_backup_lock_file(storage_dir: Path) -> Optional[Path]:
    lock_path = storage_dir / BACKUP_LOCKFILE_NAME
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()} ts={int(time.time())}\n")
            return lock_path
        except FileExistsError:
            try:
                age_seconds = time.time() - lock_path.stat().st_mtime
                if age_seconds > BACKUP_LOCK_STALE_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            return None
        except OSError:
            return None
    return None


def _release_backup_lock_file(lock_path: Optional[Path]) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _run_auto_backup_if_due() -> None:
    global _AUTO_BACKUP_LAST_CHECK_AT

    now_monotonic = time.monotonic()
    with _AUTO_BACKUP_CHECK_LOCK:
        if (now_monotonic - _AUTO_BACKUP_LAST_CHECK_AT) < BACKUP_AUTO_CHECK_COOLDOWN_SECONDS:
            return
        _AUTO_BACKUP_LAST_CHECK_AT = now_monotonic

    settings = _load_backup_auto_settings()
    if not bool(settings.get("enabled")):
        return

    mode = _backup_mode()
    if mode not in {"sqlite", "postgresql"}:
        return

    interval_hours = int(settings.get("interval_hours") or 24)
    last_run_at = settings.get("last_run_at")
    if isinstance(last_run_at, datetime):
        if (_utcnow() - last_run_at) < timedelta(hours=max(BACKUP_MIN_INTERVAL_HOURS, interval_hours)):
            return

    storage_dir = _ensure_backup_storage_dir()
    if storage_dir is None:
        return
    lock_path = _acquire_backup_lock_file(storage_dir)
    if lock_path is None:
        return

    try:
        settings = _load_backup_auto_settings()
        if not bool(settings.get("enabled")):
            return
        interval_hours = int(settings.get("interval_hours") or 24)
        last_run_at = settings.get("last_run_at")
        if isinstance(last_run_at, datetime):
            if (_utcnow() - last_run_at) < timedelta(hours=max(BACKUP_MIN_INTERVAL_HOURS, interval_hours)):
                return

        created_path, _error_key = _create_backup_snapshot(mode, source="auto")
        if created_path is None:
            return

        _save_setting_to_db(BACKUP_AUTO_LAST_RUN_AT_SETTING_KEY, _utcnow().isoformat())
        retention_days = int(settings.get("retention_days") or 14)
        _prune_old_backup_files(mode, retention_days)
    finally:
        _release_backup_lock_file(lock_path)


def _build_backup_context(lang: str, **extra) -> dict:
    t = t_factory(lang)
    mode = _backup_mode()
    tools_ok = _pg_tools_available() if mode == "postgresql" else True
    auto_settings = _load_backup_auto_settings()
    backup_files = _list_backup_files(mode) if mode in {"sqlite", "postgresql"} else []
    storage_dir = _ensure_backup_storage_dir()
    storage_dir_display = str(storage_dir) if storage_dir else "-"
    return _build_backup_context_impl(
        translate=t,
        backup_mode=mode,
        tools_ok=tools_ok,
        auto_settings=auto_settings,
        backup_files=backup_files,
        storage_dir_display=storage_dir_display,
        reset_confirm_phrase=BACKUP_RESET_CONFIRM_PHRASE,
        **extra,
    )


def _delete_all_database_rows() -> int:
    return _delete_all_database_rows_impl(engine, Base.metadata.sorted_tables)


def _compute_inventory_days_left(
    db: Session,
    project: str,
    lookback_days: int,
) -> Optional[dict]:
    days = max(1, int(lookback_days))
    period_end = _utcnow()
    period_start = period_end - timedelta(days=days)

    total_remaining = (
        db.query(func.sum(Spool.remaining_g))
        .filter(Spool.project == project)
        .scalar()
        or 0.0
    )
    total_usage = (
        db.query(func.sum(UsageHistory.deducted_g))
        .filter(
            UsageHistory.project == project,
            UsageHistory.undone.is_(False),
            UsageHistory.created_at >= period_start,
            UsageHistory.created_at < period_end,
        )
        .scalar()
        or 0.0
    )

    remaining_g = round(float(total_remaining), 1)
    usage_g = round(float(total_usage), 1)
    daily_usage = float(total_usage) / float(days) if float(days) > 0 else 0.0
    if daily_usage <= 0:
        return {
            "lookback_days": days,
            "remaining_g": remaining_g,
            "usage_g": usage_g,
            "daily_usage_g": round(daily_usage, 2),
            "days_left": None,
        }

    days_left = float(total_remaining) / daily_usage if daily_usage > 0 else None
    return {
        "lookback_days": days,
        "remaining_g": remaining_g,
        "usage_g": usage_g,
        "daily_usage_g": round(daily_usage, 2),
        "days_left": round(float(days_left), 1) if days_left is not None else None,
    }


def _load_material_thresholds(presets: dict) -> dict[str, float]:
    raw = presets.get("low_stock_thresholds") if isinstance(presets, dict) else {}
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, float] = {}
    for material, threshold in raw.items():
        parsed = _parse_optional_float(str(threshold) if threshold is not None else None)
        if parsed is None:
            continue
        key = str(material or "").strip()
        if not key:
            continue
        normalized[key] = round(float(parsed), 3)
    return normalized


def _material_color_key(material: str, color: Optional[str]) -> str:
    material_part = str(material or "").strip()
    color_part = str(color or "").strip() or "*"
    return f"{material_part}::{color_part}"


def _split_material_color_key(key: str) -> tuple[str, str]:
    raw = str(key or "")
    if "::" in raw:
        material, color = raw.split("::", 1)
        return material.strip(), (color.strip() or "*")
    return raw.strip(), "*"


def _load_material_total_threshold_entries(presets: dict) -> list[dict]:
    raw = presets.get("material_total_thresholds") if isinstance(presets, dict) else {}
    if not isinstance(raw, dict):
        return []

    entries: list[dict] = []
    for raw_key, raw_value in raw.items():
        parsed = _parse_optional_float(str(raw_value) if raw_value is not None else None)
        if parsed is None:
            continue
        material, color = _split_material_color_key(str(raw_key))
        if not material:
            continue
        entries.append(
            {
                "material": material,
                "color": color,
                "threshold_g": round(float(parsed), 3),
            }
        )
    return entries


def _effective_low_stock_threshold(spool: Spool, material_thresholds: dict[str, float]) -> Optional[float]:
    if spool.low_stock_threshold_g is not None:
        return float(spool.low_stock_threshold_g)
    material = str(spool.material or "").strip()
    if material in material_thresholds:
        return float(material_thresholds[material])
    material_lower = material.lower()
    for key, value in material_thresholds.items():
        if key.lower() == material_lower:
            return float(value)
    return None


def _recommend_min_order_g(missing_g: float) -> float:
    missing = max(0.0, float(missing_g or 0.0))
    if missing <= 0:
        return 0.0
    base_step = 250.0
    steps = int((missing + base_step - 1) // base_step)
    return round(max(base_step, steps * base_step), 3)


def _build_reorder_rows(db: Session, project: str, presets: dict, critical_only: bool = True) -> list[dict]:
    material_thresholds = _load_material_thresholds(presets)
    material_total_entries = _load_material_total_threshold_entries(presets)

    material_totals_rows = (
        db.query(
            Spool.material.label("material"),
            func.sum(Spool.remaining_g).label("total_remaining_g"),
        )
        .filter(Spool.project == project)
        .group_by(Spool.material)
        .all()
    )
    total_map: dict[str, float] = {}
    for row in material_totals_rows:
        key = str(row.material or "").strip()
        if key:
            total_map[key] = float(row.total_remaining_g or 0.0)

    material_color_totals_rows = (
        db.query(
            Spool.material.label("material"),
            Spool.color.label("color"),
            func.sum(Spool.remaining_g).label("total_remaining_g"),
        )
        .filter(Spool.project == project)
        .group_by(Spool.material, Spool.color)
        .all()
    )
    total_color_map: dict[tuple[str, str], float] = {}
    for row in material_color_totals_rows:
        material_key = str(row.material or "").strip()
        color_key = str(row.color or "").strip()
        if material_key and color_key:
            total_color_map[(material_key, color_key)] = float(row.total_remaining_g or 0.0)

    reorder_map: dict[tuple[str, str], dict] = {}

    for entry in material_total_entries:
        material = str(entry.get("material") or "").strip()
        color = str(entry.get("color") or "*").strip() or "*"
        threshold = float(entry.get("threshold_g") or 0.0)
        if not material or threshold <= 0:
            continue
        total_remaining = total_map.get(material, 0.0) if color == "*" else total_color_map.get((material, color), 0.0)
        key = (material, color)
        reorder_map[key] = {
            "material": material,
            "color": color,
            "total_remaining_g": round(float(total_remaining), 3),
            "threshold_g": round(float(threshold), 3),
            "missing_g": round(max(0.0, float(threshold) - float(total_remaining)), 3),
            "source": "material_total",
        }

    spools = db.query(Spool).filter(Spool.project == project).all()
    for spool in spools:
        threshold = _effective_low_stock_threshold(spool, material_thresholds)
        remaining = float(spool.remaining_g or 0.0)
        if threshold is None or remaining <= 0 or remaining > float(threshold):
            continue

        material = str(spool.material or "").strip()
        color = str(spool.color or "").strip() or "*"
        if not material:
            continue
        key = (material, color)
        missing = max(0.0, float(threshold) - remaining)

        existing = reorder_map.get(key)
        if existing is None:
            reorder_map[key] = {
                "material": material,
                "color": color,
                "total_remaining_g": round(float(total_color_map.get((material, color), 0.0)), 3),
                "threshold_g": round(float(threshold), 3),
                "missing_g": round(float(missing), 3),
                "source": "spool_low_stock",
            }
        else:
            existing["missing_g"] = round(float(existing["missing_g"]) + float(missing), 3)
            existing["threshold_g"] = round(max(float(existing["threshold_g"]), float(threshold)), 3)
            if existing.get("source") != "material_total":
                existing["source"] = "spool_low_stock"

    rows = list(reorder_map.values())
    for row in rows:
        row["min_order_g"] = _recommend_min_order_g(float(row.get("missing_g") or 0.0))

    if critical_only:
        rows = [row for row in rows if float(row.get("missing_g") or 0.0) > 0.0]

    rows.sort(
        key=lambda item: (
            -float(item.get("missing_g") or 0.0),
            str(item["material"]).lower(),
            str(item["color"]).lower(),
        )
    )
    return rows


def _group_usage_history_rows(rows: list[UsageHistory]) -> list[dict]:
    grouped: dict[str, dict] = {}

    for row in rows:
        group_key = row.batch_id if row.batch_id else f"single:{row.id}"
        entry = grouped.get(group_key)
        if entry is None:
            entry = {
                "batch_key": group_key,
                "created_at": row.created_at,
                "mode": row.mode,
                "actor": row.actor,
                "source_app": row.source_app,
                "source_file": row.source_file,
                "printer_name": None,
                "ams_slots": [],
                "total_deducted_g": 0.0,
                "spool_count": 0,
                "primary_spool_id": None,
                "primary_spool_brand": None,
                "primary_spool_material": None,
                "primary_spool_color": None,
                "spool_items": [],
                "spool_item_map": {},
            }
            grouped[group_key] = entry

        entry["total_deducted_g"] += float(row.deducted_g or 0.0)
        entry["spool_count"] += 1

        if entry["primary_spool_id"] is None:
            entry["primary_spool_id"] = row.spool_id
            entry["primary_spool_brand"] = row.spool_brand
            entry["primary_spool_material"] = row.spool_material
            entry["primary_spool_color"] = row.spool_color

        spool_map_key = str(row.spool_id) if row.spool_id else f"none:{row.id}"
        spool_item_map = entry["spool_item_map"]
        spool_item = spool_item_map.get(spool_map_key)
        if spool_item is None:
            spool_item = {
                "spool_id": row.spool_id,
                "brand": row.spool_brand,
                "material": row.spool_material,
                "color": row.spool_color,
                "deducted_g": 0.0,
            }
            spool_item_map[spool_map_key] = spool_item
            entry["spool_items"].append(spool_item)
        spool_item["deducted_g"] += float(row.deducted_g or 0.0)

    result: list[dict] = []
    for entry in grouped.values():
        items = entry.get("spool_items", [])
        items.sort(key=lambda item: float(item.get("deducted_g") or 0.0), reverse=True)
        for item in items:
            item["deducted_g"] = round(float(item.get("deducted_g") or 0.0), 3)
            spool_id = item.get("spool_id")
            item["spool_index_label"] = f"SP-{int(spool_id):04d}" if spool_id else "-"

        entry["total_deducted_g"] = round(float(entry.get("total_deducted_g") or 0.0), 3)
        entry.pop("spool_item_map", None)
        result.append(entry)

    return result


def _build_slot_status_rows(
    mapped_spools: list[Spool],
    live_states: list[DeviceSlotState],
    printer_ams_name_maps: Optional[dict[str, dict[int, str]]] = None,
) -> tuple[list[dict], dict[str, int]]:
    return _build_slot_status_rows_impl(
        mapped_spools=mapped_spools,
        live_states=live_states,
        stale_minutes=SLOT_STATE_STALE_MINUTES,
        printer_ams_name_maps=printer_ams_name_maps,
    )


def _summarize_slot_data_freshness(observed_times: list[Optional[datetime]]) -> dict[str, object]:
    return _summarize_slot_data_freshness_impl(
        observed_times=observed_times,
        stale_minutes=SLOT_STATE_STALE_MINUTES,
    )


def _normalize_signature_text(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _build_slot_remap_plan(mapped_spools: list[Spool], live_states: list[DeviceSlotState]) -> list[tuple[Spool, int]]:
    return _build_slot_remap_plan_impl(mapped_spools, live_states)


def _migrate_slot_format_to_canonical(db: Session, project: str) -> dict[str, int]:
    return _migrate_slot_format_to_canonical_impl(db, project)


def _extract_slot_state_entries(payload: object) -> list[dict]:
    return _extract_slot_state_entries_impl(payload)


def _upsert_slot_state_entries(db: Session, project: str, source: str, entries: list[dict]) -> int:
    return _upsert_slot_state_entries_impl(
        db=db,
        project=project,
        source=source,
        entries=entries,
        utcnow_fn=_utcnow,
    )


def render(request: Request, template: str, context: dict, lang: str):
    query_params = dict(request.query_params)
    query_params["lang"] = "de"
    lang_url_de = str(request.url.replace(query=urlencode(query_params, doseq=True)))

    query_params["lang"] = "en"
    lang_url_en = str(request.url.replace(query=urlencode(query_params, doseq=True)))

    query_params_settings = dict(request.query_params)
    query_params_settings["settings"] = "1"
    query_params_settings["lang"] = "de"
    lang_url_de_settings = str(request.url.replace(query=urlencode(query_params_settings, doseq=True)))

    query_params_settings["lang"] = "en"
    lang_url_en_settings = str(request.url.replace(query=urlencode(query_params_settings, doseq=True)))

    settings_query_params = dict(request.query_params)
    settings_query_params["settings"] = "1"
    settings_query = urlencode(settings_query_params, doseq=True)
    settings_return_url = f"{request.url.path}?{settings_query}" if settings_query else request.url.path

    theme = get_theme(request)
    project_scope = get_project(request)
    project = _extract_base_project_from_scope(project_scope)
    auto_refresh_seconds = get_auto_refresh_seconds(request)
    privacy_blur = get_privacy_blur(request)
    mobile_entry_url = _resolve_mobile_entry_url(request)
    mobile_entry_qr_png = generate_qr_png(mobile_entry_url)
    mobile_entry_qr_data_url = f"data:image/png;base64,{base64.b64encode(mobile_entry_qr_png).decode('ascii')}"

    response = templates.TemplateResponse(
        request,
        template,
        {
            "lang": lang,
            "theme": theme,
            "project": project,
            "project_options": PROJECT_OPTIONS,
            "auto_refresh_seconds": auto_refresh_seconds,
            "auto_refresh_options": AUTO_REFRESH_OPTIONS,
            "privacy_blur": privacy_blur,
            "is_authenticated": True,
            "current_user_email": None,
            "current_user_name": None,
            "t": t_factory(lang),
            "lang_url_de": lang_url_de,
            "lang_url_en": lang_url_en,
            "lang_url_de_settings": lang_url_de_settings,
            "lang_url_en_settings": lang_url_en_settings,
            "settings_return_url": settings_return_url,
            "mobile_entry_url": mobile_entry_url,
            "mobile_entry_qr_data_url": mobile_entry_qr_data_url,
            **context,
        },
    )
    _set_cookie(response, "lang", lang)
    if not request.cookies.get("theme"):
        _set_cookie(response, "theme", theme)
    if not request.cookies.get("project"):
        _set_cookie(response, "project", project)
    if not request.cookies.get("auto_refresh_seconds"):
        _set_cookie(response, "auto_refresh_seconds", str(auto_refresh_seconds))
    if not request.cookies.get("privacy_blur"):
        _set_cookie(response, "privacy_blur", "1" if privacy_blur else "0")
    return response


@app.post("/settings")
def save_settings(
    request: Request,
    lang: Optional[str] = Form(None),
    theme: Optional[str] = Form(None),
    project: Optional[str] = Form(None),
    auto_refresh_seconds: Optional[str] = Form(None),
    privacy_blur: Optional[str] = Form(None),
    persist_db: Optional[str] = Form("1"),
    next_url: Optional[str] = Form("/"),
):
    normalized_lang = lang if lang in TRANSLATIONS else None
    normalized_theme = theme if theme in VALID_THEMES else None
    normalized_project = _normalize_project(project) if project is not None else None
    normalized_auto_refresh_seconds = (
        _normalize_auto_refresh_seconds(auto_refresh_seconds)
        if auto_refresh_seconds is not None
        else None
    )
    normalized_privacy_blur = (
        _normalize_privacy_blur(privacy_blur)
        if privacy_blur is not None
        else None
    )
    should_persist_db = _is_truthy(persist_db)

    response = RedirectResponse(_normalize_next_url(next_url), status_code=303)

    if normalized_lang:
        _set_cookie(response, "lang", normalized_lang)
        if should_persist_db:
            _save_setting_to_db("lang", normalized_lang)

    if normalized_theme:
        _set_cookie(response, "theme", normalized_theme)
        if should_persist_db:
            _save_setting_to_db("theme", normalized_theme)

    if normalized_project:
        _set_cookie(response, "project", normalized_project)
        if should_persist_db:
            _save_setting_to_db("project", normalized_project)

    if normalized_auto_refresh_seconds is not None:
        normalized_auto_refresh_str = str(normalized_auto_refresh_seconds)
        _set_cookie(response, "auto_refresh_seconds", normalized_auto_refresh_str)
        if should_persist_db:
            _save_setting_to_db("auto_refresh_seconds", normalized_auto_refresh_str)

    if normalized_privacy_blur is not None:
        normalized_privacy_blur_str = "1" if normalized_privacy_blur else "0"
        _set_cookie(response, "privacy_blur", normalized_privacy_blur_str)
        if should_persist_db:
            _save_setting_to_db("privacy_blur", normalized_privacy_blur_str)

    if (
        not normalized_lang
        and not normalized_theme
        and not normalized_project
        and normalized_auto_refresh_seconds is None
        and normalized_privacy_blur is None
    ):
        _set_cookie(response, "lang", get_lang(request))
        _set_cookie(response, "theme", get_theme(request))
        _set_cookie(response, "project", get_project(request))
        _set_cookie(response, "auto_refresh_seconds", str(get_auto_refresh_seconds(request)))
        _set_cookie(response, "privacy_blur", "1" if get_privacy_blur(request) else "0")

    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            get_project(request),
            "settings_update",
            request=request,
            entity_type="settings",
            details={
                "lang": normalized_lang,
                "theme": normalized_theme,
                "project": normalized_project,
                "auto_refresh_seconds": normalized_auto_refresh_seconds,
                "privacy_blur": normalized_privacy_blur,
                "persist_db": should_persist_db,
            },
        )
        db_local.commit()
    finally:
        db_local.close()

    return response


@app.get("/settings")
def open_settings(next_url: Optional[str] = None):
    target = _normalize_next_url(next_url or "/")
    separator = "&" if "?" in target else "?"
    if "settings=" not in target:
        target = f"{target}{separator}settings=1"
    return RedirectResponse(target, status_code=303)


def _render_dashboard(
    request: Request,
    q: Optional[str] = None,
    location_id: Optional[str] = None,
    lifecycle_status: Optional[str] = None,
    sort: Optional[str] = None,
    dir: Optional[str] = None,
    page: Optional[int] = 1,
    page_size: Optional[int] = 25,
    hide_empty: bool = False,
    db: Session = Depends(get_db),
    show_stats: bool = True,
    show_spool_list: bool = True,
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)
    notice_key = str(request.query_params.get("notice") or "").strip()
    presets = load_presets()
    material_thresholds = _load_material_thresholds(presets)
    sort_key = (sort or "spool_index").strip().lower()
    sort_dir = "desc" if (dir or "desc").strip().lower() == "desc" else "asc"
    page_size_options = [10, 25, 50, 100]
    page_size = page_size if page_size in page_size_options else 25
    page = max(1, int(page or 1))

    spool_scope_filters = _model_scope_filters(Spool, project)
    usage_scope_filters = _model_scope_filters(UsageHistory, project)

    query = db.query(Spool).filter(*spool_scope_filters)
    normalized_location_id = _normalize_storage_sub_location_id(location_id)
    normalized_lifecycle_status = _normalize_lifecycle_status(lifecycle_status) if lifecycle_status else None
    if normalized_location_id is not None:
        query = query.filter(Spool.storage_sub_location_id == normalized_location_id)
    if normalized_lifecycle_status is not None:
        query = query.filter(Spool.lifecycle_status == normalized_lifecycle_status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Spool.brand.ilike(like),
                Spool.material.ilike(like),
                Spool.color.ilike(like),
                Spool.location.ilike(like),
            )
        )
    if hide_empty:
        query = query.filter(Spool.remaining_g > 0).filter(func.coalesce(Spool.lifecycle_status, "new") != "empty")

    status_sort_expr = case(
        (Spool.remaining_g <= 0, 0),
        (Spool.in_use.is_(True), 2),
        else_=1,
    )
    sort_fields = {
        "spool_index": Spool.id,
        "brand": Spool.brand,
        "material": Spool.material,
        "color": Spool.color,
        "weight": Spool.weight_g,
        "remaining": Spool.remaining_g,
        "threshold": func.coalesce(Spool.low_stock_threshold_g, -1),
        "price": func.coalesce(Spool.price, -1),
        "location": Spool.location,
        "lifecycle": Spool.lifecycle_status,
        "status": status_sort_expr,
    }
    if sort_key not in sort_fields:
        sort_key = "spool_index"

    sort_expr = sort_fields[sort_key]
    ordered_query = query.order_by(sort_expr.desc() if sort_dir == "desc" else sort_expr.asc())
    total_count = query.count()
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    spools = ordered_query.offset(offset).limit(page_size).all()

    storage_map = _storage_location_map_by_id(
        db,
        project,
        [int(spool.storage_sub_location_id) for spool in spools if spool.storage_sub_location_id],
    )

    for spool in spools:
        if spool.low_stock_threshold_g is not None:
            threshold = float(spool.low_stock_threshold_g)
            threshold_source = "spool"
        else:
            threshold = _effective_low_stock_threshold(spool, material_thresholds)
            threshold_source = "material" if threshold is not None else None
        remaining = float(spool.remaining_g or 0)
        spool.is_low_stock = bool(threshold is not None and remaining > 0 and remaining <= threshold)
        spool.low_stock_threshold_effective_g = threshold
        spool.low_stock_threshold_source = threshold_source
        spool.location_display = _spool_location_display(spool, storage_map)

    low_stock_spools_count = 0
    total_inventory_value_eur = 0.0
    all_spools_for_low_stock = db.query(Spool).filter(*spool_scope_filters).all()
    for spool in all_spools_for_low_stock:
        threshold = _effective_low_stock_threshold(spool, material_thresholds)
        remaining = float(spool.remaining_g or 0)
        if threshold is not None and remaining > 0 and remaining <= threshold:
            low_stock_spools_count += 1

        weight = float(spool.weight_g or 0)
        price = float(spool.price or 0)
        if weight > 0 and price > 0 and remaining > 0:
            remaining_clamped = min(remaining, weight)
            total_inventory_value_eur += (remaining_clamped / weight) * price

    stats = {
        "total_spools": db.query(func.count(Spool.id)).filter(*spool_scope_filters).scalar() or 0,
        "total_weight": round(db.query(func.sum(Spool.weight_g)).filter(*spool_scope_filters).scalar() or 0, 1),
        "total_remaining": round(db.query(func.sum(Spool.remaining_g)).filter(*spool_scope_filters).scalar() or 0, 1),
        "total_value": round(total_inventory_value_eur, 2),
        "empty_spools": db.query(func.count(Spool.id)).filter(*spool_scope_filters, Spool.remaining_g <= 0).scalar() or 0,
        "low_stock_spools": low_stock_spools_count,
    }

    total_remaining = float(stats["total_remaining"] or 0)
    top5_rows = (
        db.query(
            Spool.material.label("name"),
            func.sum(Spool.remaining_g).label("remaining_g"),
        )
        .filter(*spool_scope_filters)
        .group_by(Spool.material)
        .order_by(func.sum(Spool.remaining_g).desc())
        .limit(5)
        .all()
    )
    top5_materials = [
        {
            "name": row.name if row.name not in (None, "") else "-",
            "remaining_g": round(float(row.remaining_g or 0), 1),
            "share_pct": round((float(row.remaining_g or 0) / total_remaining * 100), 1)
            if total_remaining
            else 0.0,
        }
        for row in top5_rows
    ]

    top5_color_rows = (
        db.query(
            Spool.color.label("name"),
            func.sum(Spool.remaining_g).label("remaining_g"),
        )
        .filter(*spool_scope_filters)
        .group_by(Spool.color)
        .order_by(func.sum(Spool.remaining_g).desc())
        .limit(5)
        .all()
    )
    top5_colors = [
        {
            "name": row.name if row.name not in (None, "") else "-",
            "remaining_g": round(float(row.remaining_g or 0), 1),
            "share_pct": round((float(row.remaining_g or 0) / total_remaining * 100), 1)
            if total_remaining
            else 0.0,
        }
        for row in top5_color_rows
    ]

    now = _utcnow()
    month_start = datetime(now.year, now.month, 1)
    if now.month == 12:
        next_month_start = datetime(now.year + 1, 1, 1)
    else:
        next_month_start = datetime(now.year, now.month + 1, 1)

    month_usage_g = (
        db.query(func.sum(UsageHistory.deducted_g))
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= month_start)
        .filter(UsageHistory.created_at < next_month_start)
        .scalar()
        or 0.0
    )

    month_cost_eur = (
        db.query(
            func.sum(
                UsageHistory.deducted_g
                * (
                    func.coalesce(Spool.price, 0.0)
                    / func.nullif(func.coalesce(Spool.weight_g, 0.0), 0.0)
                )
            )
        )
        .outerjoin(Spool, Spool.id == UsageHistory.spool_id)
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= month_start)
        .filter(UsageHistory.created_at < next_month_start)
        .scalar()
        or 0.0
    )

    month_keys = []
    y, m = now.year, now.month
    for _ in range(6):
        month_keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    month_keys = list(reversed(month_keys))
    first_month_key = month_keys[0]
    first_year, first_month = first_month_key.split("-")
    trend_start = datetime(int(first_year), int(first_month), 1)

    dialect_name = ""
    if db.bind is not None and getattr(db.bind, "dialect", None) is not None:
        dialect_name = str(db.bind.dialect.name or "").lower()

    def month_key_expr(column):
        if dialect_name == "postgresql":
            return func.to_char(column, "YYYY-MM")
        if dialect_name in {"mysql", "mariadb"}:
            return func.date_format(column, "%Y-%m")
        return func.strftime("%Y-%m", column)

    month_expr = month_key_expr(UsageHistory.created_at)

    usage_by_month_rows = (
        db.query(
            month_expr.label("month_key"),
            func.sum(UsageHistory.deducted_g).label("usage_g"),
        )
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= trend_start)
        .group_by(month_expr)
        .all()
    )
    usage_by_month = {
        row.month_key: round(float(row.usage_g or 0.0), 1) for row in usage_by_month_rows
    }

    cost_by_month_rows = (
        db.query(
            month_expr.label("month_key"),
            func.sum(
                UsageHistory.deducted_g
                * (
                    func.coalesce(Spool.price, 0.0)
                    / func.nullif(func.coalesce(Spool.weight_g, 0.0), 0.0)
                )
            ).label("cost_eur"),
        )
        .outerjoin(Spool, Spool.id == UsageHistory.spool_id)
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= trend_start)
        .group_by(month_expr)
        .all()
    )
    cost_by_month = {
        row.month_key: round(float(row.cost_eur or 0.0), 2) for row in cost_by_month_rows
    }

    material_name_expr = func.coalesce(UsageHistory.spool_material, "-")
    material_month_rows = (
        db.query(
            month_expr.label("month_key"),
            material_name_expr.label("name"),
            func.sum(UsageHistory.deducted_g).label("usage_g"),
        )
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= trend_start)
        .group_by(
            month_expr,
            material_name_expr,
        )
        .all()
    )
    top_material_by_month = {}
    for row in material_month_rows:
        month_key = row.month_key
        grams = float(row.usage_g or 0.0)
        current = top_material_by_month.get(month_key)
        if current is None or grams > current["usage_g"]:
            top_material_by_month[month_key] = {
                "name": row.name or "-",
                "usage_g": round(grams, 1),
            }

    color_name_expr = func.coalesce(UsageHistory.spool_color, "-")
    color_month_rows = (
        db.query(
            month_expr.label("month_key"),
            color_name_expr.label("name"),
            func.sum(UsageHistory.deducted_g).label("usage_g"),
        )
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= trend_start)
        .group_by(
            month_expr,
            color_name_expr,
        )
        .all()
    )
    top_color_by_month = {}
    for row in color_month_rows:
        month_key = row.month_key
        grams = float(row.usage_g or 0.0)
        current = top_color_by_month.get(month_key)
        if current is None or grams > current["usage_g"]:
            top_color_by_month[month_key] = {
                "name": row.name or "-",
                "usage_g": round(grams, 1),
            }

    monthly_usage_trend = []
    top_material_trend = []
    top_color_trend = []
    for month_key in month_keys:
        year_str, month_str = month_key.split("-")
        label = f"{month_str}/{year_str}"
        monthly_usage_trend.append(
            {
                "month_key": month_key,
                "label": label,
                "usage_g": usage_by_month.get(month_key, 0.0),
                "cost_eur": cost_by_month.get(month_key, 0.0),
            }
        )

        material_row = top_material_by_month.get(month_key)
        top_material_trend.append(
            {
                "month_key": month_key,
                "label": label,
                "name": material_row["name"] if material_row else "-",
                "usage_g": material_row["usage_g"] if material_row else 0.0,
            }
        )

        color_row = top_color_by_month.get(month_key)
        top_color_trend.append(
            {
                "month_key": month_key,
                "label": label,
                "name": color_row["name"] if color_row else "-",
                "usage_g": color_row["usage_g"] if color_row else 0.0,
            }
        )

    forecast_30 = _compute_inventory_days_left(db, project, 30)
    forecast_90 = _compute_inventory_days_left(db, project, 90)

    presets = load_presets()
    reorder_rows = _build_reorder_rows(db, project, presets)

    return render(
        request,
        "index.html",
        {
            "spools": spools,
            "stats": stats,
            "top5_materials": top5_materials,
            "top5_colors": top5_colors,
            "month_usage_g": round(float(month_usage_g or 0.0), 1),
            "month_cost_eur": round(float(month_cost_eur or 0.0), 2),
            "monthly_usage_trend": monthly_usage_trend,
            "top_material_trend": top_material_trend,
            "top_color_trend": top_color_trend,
            "forecast_30": forecast_30,
            "forecast_90": forecast_90,
            "reorder_rows": reorder_rows,
            "show_stats": show_stats,
            "show_spool_list": show_spool_list,
            "list_base_path": "/spools",
            "message": t(notice_key) if notice_key in {"qr_scan_location_loaded"} else None,
            "q": q,
            "location_id": normalized_location_id,
            "lifecycle_status": normalized_lifecycle_status,
            "lifecycle_status_options": _lifecycle_status_options(lang),
            "storage_location_options": _storage_location_options(db, project),
            "hide_empty": hide_empty,
            "sort": sort_key,
            "sort_dir": sort_dir,
            "page": page,
            "page_size": page_size,
            "page_size_options": page_size_options,
            "total_count": total_count,
            "total_pages": total_pages,
        },
        lang,
    )


def _analysis_low_stock(
    db: Session,
    spool_scope_filters: list,
    material_thresholds: dict[str, float],
    limit: int,
) -> dict:
    all_spools = db.query(Spool).filter(*spool_scope_filters).all()
    items: list[dict] = []
    for spool in all_spools:
        threshold = _effective_low_stock_threshold(spool, material_thresholds)
        remaining = float(spool.remaining_g or 0.0)
        if threshold is None or remaining <= 0 or remaining > float(threshold):
            continue
        items.append(
            {
                "id": int(spool.id),
                "name": f"SP-{int(spool.id):04d}",
                "material": str(spool.material or "-").strip() or "-",
                "color": str(spool.color or "-").strip() or "-",
                "remaining_g": round(remaining, 1),
                "threshold_g": round(float(threshold), 1),
            }
        )

    items.sort(key=lambda item: (float(item["remaining_g"]), int(item["id"])))
    return {
        "count": len(items),
        "items": items[:limit],
    }


@app.get("/")
def landing_page(request: Request):
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard")
def index(
    request: Request,
    q: Optional[str] = None,
    location_id: Optional[str] = None,
    lifecycle_status: Optional[str] = None,
    sort: Optional[str] = None,
    dir: Optional[str] = None,
    page: Optional[int] = 1,
    page_size: Optional[int] = 25,
    hide_empty: bool = False,
    db: Session = Depends(get_db),
):
    return _render_dashboard(
        request=request,
        q=q,
        location_id=location_id,
        lifecycle_status=lifecycle_status,
        sort=sort,
        dir=dir,
        page=page,
        page_size=page_size,
        hide_empty=hide_empty,
        db=db,
        show_stats=True,
        show_spool_list=False,
    )


@app.get("/spools")
def spool_list_page(
    request: Request,
    q: Optional[str] = None,
    location_id: Optional[str] = None,
    lifecycle_status: Optional[str] = None,
    sort: Optional[str] = None,
    dir: Optional[str] = None,
    page: Optional[int] = 1,
    page_size: Optional[int] = 25,
    hide_empty: bool = True,
    db: Session = Depends(get_db),
):
    return _render_dashboard(
        request=request,
        q=q,
        location_id=location_id,
        lifecycle_status=lifecycle_status,
        sort=sort,
        dir=dir,
        page=page,
        page_size=page_size,
        hide_empty=hide_empty,
        db=db,
        show_stats=False,
        show_spool_list=True,
    )


@app.get("/analysis")
def analysis(
    request: Request,
    period_days: Optional[int] = 30,
    trend_months: Optional[int] = 6,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    spool_scope_filters = _model_scope_filters(Spool, project)
    usage_scope_filters = _model_scope_filters(UsageHistory, project)
    normalized_period_days = _bounded_int(period_days, default=30, minimum=7, maximum=365)
    normalized_trend_months = _bounded_int(trend_months, default=6, minimum=3, maximum=24)
    period_end = _utcnow()
    period_start = period_end - timedelta(days=normalized_period_days)
    material_thresholds = _load_material_thresholds(load_presets())

    def grouped(column):
        rows = (
            db.query(
                column.label("name"),
                func.count(Spool.id).label("count"),
                func.sum(Spool.weight_g).label("weight_g"),
                func.sum(Spool.remaining_g).label("remaining_g"),
                func.sum(Spool.price).label("value"),
            )
            .filter(*spool_scope_filters)
            .group_by(column)
            .order_by(func.sum(Spool.remaining_g).desc())
            .all()
        )
        return [
            {
                "name": row.name if row.name not in (None, "") else "-",
                "count": int(row.count or 0),
                "weight_g": round(float(row.weight_g or 0), 1),
                "remaining_g": round(float(row.remaining_g or 0), 1),
                "value": round(float(row.value or 0), 2),
            }
            for row in rows
        ]

    total_remaining = (
        db.query(func.sum(Spool.remaining_g))
        .filter(*spool_scope_filters)
        .scalar()
        or 0
    )

    grouped_data = {
        "brand": grouped(Spool.brand),
        "material": grouped(Spool.material),
        "color": grouped(Spool.color),
        "location": grouped(Spool.location),
    }

    for key in grouped_data:
        for row in grouped_data[key]:
            row["share_pct"] = round((row["remaining_g"] / total_remaining * 100), 1) if total_remaining else 0.0

    period_usage_g, period_cost_eur = _analysis_usage_and_cost_in_period(
        db,
        usage_scope_filters,
        period_start,
        period_end,
    )
    usage_cost_trend = _analysis_usage_cost_trend(
        db,
        usage_scope_filters,
        normalized_trend_months,
    )
    top_material_usage = _analysis_top_usage(
        db,
        usage_scope_filters,
        period_start,
        period_end,
        group_by="material",
        limit=5,
    )
    top_color_usage = _analysis_top_usage(
        db,
        usage_scope_filters,
        period_start,
        period_end,
        group_by="color",
        limit=5,
    )
    low_stock_summary = _analysis_low_stock(
        db,
        spool_scope_filters,
        material_thresholds,
        limit=8,
    )
    printer_slot_usage = _analysis_printer_slot_usage(
        db,
        usage_scope_filters,
        period_start,
        period_end,
        limit=8,
    )

    return render(
        request,
        "analysis.html",
        {
            "groups": grouped_data,
            "total_remaining": round(float(total_remaining), 1),
            "analysis_period_days": normalized_period_days,
            "analysis_trend_months": normalized_trend_months,
            "period_usage_g": period_usage_g,
            "period_cost_eur": period_cost_eur,
            "usage_cost_trend": usage_cost_trend,
            "top_material_usage": top_material_usage,
            "top_color_usage": top_color_usage,
            "low_stock_count": int(low_stock_summary["count"]),
            "low_stock_items": low_stock_summary["items"],
            "printer_slot_usage": printer_slot_usage,
        },
        lang,
    )


@app.get("/audit")
def audit_page(
    request: Request,
    action: Optional[str] = None,
    period_days: Optional[int] = 30,
    page: Optional[int] = 1,
    page_size: Optional[int] = 25,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    normalized_period_days = _bounded_int(period_days, default=30, minimum=7, maximum=365)
    page_size_options = [10, 25, 50, 100]
    normalized_page_size = page_size if page_size in page_size_options else 25
    normalized_page = max(1, int(page or 1))
    period_start = _utcnow() - timedelta(days=normalized_period_days)

    selected_action = str(action or "").strip()
    query = (
        db.query(AuditLog)
        .filter(AuditLog.project == project)
        .filter(AuditLog.created_at >= period_start)
    )
    if selected_action:
        query = query.filter(AuditLog.action == selected_action)

    total_count = query.count()
    total_pages = max(1, (total_count + normalized_page_size - 1) // normalized_page_size)
    if normalized_page > total_pages:
        normalized_page = total_pages

    rows = (
        query
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((normalized_page - 1) * normalized_page_size)
        .limit(normalized_page_size)
        .all()
    )

    action_rows = (
        db.query(AuditLog.action)
        .filter(AuditLog.project == project)
        .distinct()
        .order_by(AuditLog.action.asc())
        .all()
    )
    action_options = [str(item.action or "").strip() for item in action_rows if str(item.action or "").strip()]

    return render(
        request,
        "audit.html",
        {
            "audit_rows": rows,
            "audit_action": selected_action,
            "audit_period_days": normalized_period_days,
            "audit_action_options": action_options,
            "page": normalized_page,
            "page_size": normalized_page_size,
            "page_size_options": page_size_options,
            "total_count": total_count,
            "total_pages": total_pages,
        },
        lang,
    )


@app.get("/audit/export/csv")
def audit_export_csv(
    request: Request,
    action: Optional[str] = None,
    period_days: Optional[int] = 30,
    db: Session = Depends(get_db),
):
    project = get_project(request)
    normalized_period_days = _bounded_int(period_days, default=30, minimum=7, maximum=365)
    period_start = _utcnow() - timedelta(days=normalized_period_days)
    selected_action = str(action or "").strip()

    query = (
        db.query(AuditLog)
        .filter(AuditLog.project == project)
        .filter(AuditLog.created_at >= period_start)
    )
    if selected_action:
        query = query.filter(AuditLog.action == selected_action)

    rows = (
        query
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .all()
    )

    payload = [
        {
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "project": row.project,
            "actor": row.actor,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "details_json": row.details_json,
        }
        for row in rows
    ]

    import pandas as pd

    df = pd.DataFrame(payload)
    buffer = BytesIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    filename = f"filament_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/analysis/usage-cost-trend")
def api_analysis_usage_cost_trend(
    request: Request,
    trend_months: Optional[int] = 6,
    db: Session = Depends(get_db),
):
    project = get_project(request)
    usage_scope_filters = _model_scope_filters(UsageHistory, project)
    normalized_trend_months = _bounded_int(trend_months, default=6, minimum=3, maximum=24)
    series = _analysis_usage_cost_trend(db, usage_scope_filters, normalized_trend_months)
    return {
        "ok": True,
        "trend_months": normalized_trend_months,
        "series": series,
    }


@app.get("/api/analysis/top-usage")
def api_analysis_top_usage(
    request: Request,
    group_by: Optional[str] = "material",
    period_days: Optional[int] = 30,
    limit: Optional[int] = 5,
    db: Session = Depends(get_db),
):
    project = get_project(request)
    usage_scope_filters = _model_scope_filters(UsageHistory, project)
    normalized_group_by = "color" if str(group_by or "").strip().lower() == "color" else "material"
    normalized_period_days = _bounded_int(period_days, default=30, minimum=7, maximum=365)
    normalized_limit = _bounded_int(limit, default=5, minimum=1, maximum=20)
    period_end = _utcnow()
    period_start = period_end - timedelta(days=normalized_period_days)
    rows = _analysis_top_usage(
        db,
        usage_scope_filters,
        period_start,
        period_end,
        group_by=normalized_group_by,
        limit=normalized_limit,
    )
    return {
        "ok": True,
        "group_by": normalized_group_by,
        "period_days": normalized_period_days,
        "rows": rows,
    }


@app.get("/api/analysis/printer-slot-usage")
def api_analysis_printer_slot_usage(
    request: Request,
    period_days: Optional[int] = 30,
    limit: Optional[int] = 8,
    db: Session = Depends(get_db),
):
    project = get_project(request)
    usage_scope_filters = _model_scope_filters(UsageHistory, project)
    normalized_period_days = _bounded_int(period_days, default=30, minimum=7, maximum=365)
    normalized_limit = _bounded_int(limit, default=8, minimum=1, maximum=20)
    period_end = _utcnow()
    period_start = period_end - timedelta(days=normalized_period_days)
    rows = _analysis_printer_slot_usage(
        db,
        usage_scope_filters,
        period_start,
        period_end,
        limit=normalized_limit,
    )
    return {
        "ok": True,
        "period_days": normalized_period_days,
        "rows": rows,
    }


@app.get("/api/analysis/low-stock")
def api_analysis_low_stock(
    request: Request,
    limit: Optional[int] = 8,
    db: Session = Depends(get_db),
):
    project = get_project(request)
    spool_scope_filters = _model_scope_filters(Spool, project)
    normalized_limit = _bounded_int(limit, default=8, minimum=1, maximum=50)
    material_thresholds = _load_material_thresholds(load_presets())
    summary = _analysis_low_stock(db, spool_scope_filters, material_thresholds, normalized_limit)
    return {
        "ok": True,
        "count": int(summary["count"]),
        "items": summary["items"],
    }


@app.get("/slot-status")
def slot_status_page(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)

    mapped_spools = (
        db.query(Spool)
        .filter(Spool.project == project, Spool.ams_slot.is_not(None), Spool.ams_slot > 0)
        .all()
    )
    live_states = (
        db.query(DeviceSlotState)
        .filter(DeviceSlotState.project == project)
        .all()
    )

    printers = (
        db.query(Printer)
        .filter(Printer.project == project)
        .all()
    )
    printer_ams_name_maps: dict[str, dict[int, str]] = {}
    for printer in printers:
        printer_name = _normalize_printer_name(printer.name)
        if not printer_name:
            continue
        printer_ams_name_maps[printer_name] = _parse_ams_name_mapping(printer.ams_name_map)

    slot_rows, slot_summary = _build_slot_status_rows(mapped_spools, live_states, printer_ams_name_maps)
    slot_data_freshness = _summarize_slot_data_freshness([state.observed_at for state in live_states])

    return render(
        request,
        "slot_status.html",
        {
            "slot_rows": slot_rows,
            "slot_summary": slot_summary,
            "has_live_data": len(live_states) > 0,
            "stale_minutes": SLOT_STATE_STALE_MINUTES,
            "slot_data_freshness": slot_data_freshness,
        },
        lang,
    )


@app.post("/slot-status/remap-ams")
def slot_status_remap_ams(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)

    mapped_spools = (
        db.query(Spool)
        .filter(Spool.project == project, Spool.ams_slot.is_not(None), Spool.ams_slot > 0)
        .all()
    )
    live_states = (
        db.query(DeviceSlotState)
        .filter(DeviceSlotState.project == project)
        .all()
    )

    printers = (
        db.query(Printer)
        .filter(Printer.project == project)
        .all()
    )
    printer_ams_name_maps: dict[str, dict[int, str]] = {}
    for printer in printers:
        printer_name = _normalize_printer_name(printer.name)
        if not printer_name:
            continue
        printer_ams_name_maps[printer_name] = _parse_ams_name_mapping(printer.ams_name_map)

    message: Optional[str] = None
    error: Optional[str] = None

    if not live_states:
        error = t("slot_remap_no_live")
    else:
        remap_plan = _build_slot_remap_plan(mapped_spools, live_states)
        updated = 0
        now = _utcnow().replace(tzinfo=None)
        for spool, target_slot in remap_plan:
            if int(spool.ams_slot or 0) == int(target_slot):
                continue
            spool.ams_slot = int(target_slot)
            spool.updated_at = now
            updated += 1

        if updated > 0:
            _audit_log(
                db,
                project,
                "slot_status_remap_ams",
                request=request,
                entity_type="spool",
                details={"updated": int(updated)},
            )
            db.commit()
            message = t("slot_remap_done").format(updated=updated)
        else:
            message = t("slot_remap_none")

    refreshed_spools = (
        db.query(Spool)
        .filter(Spool.project == project, Spool.ams_slot.is_not(None), Spool.ams_slot > 0)
        .all()
    )
    refreshed_states = (
        db.query(DeviceSlotState)
        .filter(DeviceSlotState.project == project)
        .all()
    )

    slot_rows, slot_summary = _build_slot_status_rows(refreshed_spools, refreshed_states, printer_ams_name_maps)
    slot_data_freshness = _summarize_slot_data_freshness([state.observed_at for state in refreshed_states])

    return render(
        request,
        "slot_status.html",
        {
            "slot_rows": slot_rows,
            "slot_summary": slot_summary,
            "has_live_data": len(refreshed_states) > 0,
            "stale_minutes": SLOT_STATE_STALE_MINUTES,
            "slot_data_freshness": slot_data_freshness,
            "message": message,
            "error": error,
        },
        lang,
    )


@app.post("/slot-status/migrate-slot-format")
def slot_status_migrate_slot_format(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)

    migration = _migrate_slot_format_to_canonical(db, project)
    db.commit()

    printers = (
        db.query(Printer)
        .filter(Printer.project == project)
        .all()
    )
    printer_ams_name_maps: dict[str, dict[int, str]] = {}
    for printer in printers:
        printer_name = _normalize_printer_name(printer.name)
        if not printer_name:
            continue
        printer_ams_name_maps[printer_name] = _parse_ams_name_mapping(printer.ams_name_map)

    mapped_spools = (
        db.query(Spool)
        .filter(Spool.project == project, Spool.ams_slot.is_not(None), Spool.ams_slot > 0)
        .all()
    )
    live_states = (
        db.query(DeviceSlotState)
        .filter(DeviceSlotState.project == project)
        .all()
    )

    slot_rows, slot_summary = _build_slot_status_rows(mapped_spools, live_states, printer_ams_name_maps)
    slot_data_freshness = _summarize_slot_data_freshness([state.observed_at for state in live_states])

    message = t("slot_format_migrate_done").format(
        spools=int(migration.get("spools", 0)),
        states=int(migration.get("states", 0)),
        contexts=int(migration.get("contexts", 0)),
    )
    skip_count = int(migration.get("skipped", 0))
    if skip_count > 0:
        message = f"{message} {t('slot_format_migrate_skip').format(count=skip_count)}"

    return render(
        request,
        "slot_status.html",
        {
            "slot_rows": slot_rows,
            "slot_summary": slot_summary,
            "has_live_data": len(live_states) > 0,
            "stale_minutes": SLOT_STATE_STALE_MINUTES,
            "slot_data_freshness": slot_data_freshness,
            "message": message,
        },
        lang,
    )


@app.get("/thresholds")
def thresholds_page(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    presets = load_presets()
    material_thresholds = _load_material_thresholds(presets)
    material_total_entries = _load_material_total_threshold_entries(presets)

    material_rows = [
        {"material": material, "threshold_g": round(float(threshold), 3)}
        for material, threshold in sorted(material_thresholds.items(), key=lambda x: x[0].lower())
    ]

    spool_rows = []
    all_spools = db.query(Spool).filter(Spool.project == project).order_by(Spool.id.asc()).all()
    for spool in all_spools:
        spool_rows.append(
            {
                "id": spool.id,
                "brand": spool.brand,
                "material": spool.material,
                "color": spool.color,
                "remaining_g": round(float(spool.remaining_g or 0), 3),
                "threshold_g": round(float(spool.low_stock_threshold_g), 3) if spool.low_stock_threshold_g is not None else None,
                "is_low_stock": bool(
                    spool.low_stock_threshold_g is not None
                    and float(spool.remaining_g or 0) > 0
                    and float(spool.remaining_g or 0) <= float(spool.low_stock_threshold_g or 0)
                ),
            }
        )

    spool_threshold_rows = []
    spools_with_threshold = (
        db.query(Spool)
        .filter(Spool.project == project)
        .filter(Spool.low_stock_threshold_g.is_not(None))
        .order_by(Spool.id.asc())
        .all()
    )
    for spool in spools_with_threshold:
        threshold = float(spool.low_stock_threshold_g or 0)
        remaining = float(spool.remaining_g or 0)
        spool_threshold_rows.append(
            {
                "id": spool.id,
                "brand": spool.brand,
                "material": spool.material,
                "color": spool.color,
                "remaining_g": round(remaining, 3),
                "threshold_g": round(threshold, 3),
                "is_low_stock": threshold > 0 and remaining > 0 and remaining <= threshold,
            }
        )

    material_totals_rows = (
        db.query(
            Spool.material.label("material"),
            func.sum(Spool.remaining_g).label("total_remaining_g"),
        )
        .filter(Spool.project == project)
        .group_by(Spool.material)
        .all()
    )
    total_map: dict[str, float] = {}
    for row in material_totals_rows:
        key = str(row.material or "").strip()
        if not key:
            continue
        total_map[key] = round(float(row.total_remaining_g or 0), 3)

    material_color_totals_rows = (
        db.query(
            Spool.material.label("material"),
            Spool.color.label("color"),
            func.sum(Spool.remaining_g).label("total_remaining_g"),
        )
        .filter(Spool.project == project)
        .group_by(Spool.material, Spool.color)
        .all()
    )
    total_color_map: dict[tuple[str, str], float] = {}
    for row in material_color_totals_rows:
        material_key = str(row.material or "").strip()
        color_key = str(row.color or "").strip()
        if not material_key or not color_key:
            continue
        total_color_map[(material_key, color_key)] = round(float(row.total_remaining_g or 0), 3)

    material_total_rows = []
    for entry in sorted(material_total_entries, key=lambda x: (x["material"].lower(), x["color"].lower())):
        material = entry["material"]
        color = entry["color"]
        threshold = float(entry["threshold_g"])
        if color == "*":
            total_remaining = total_map.get(material, 0.0)
        else:
            total_remaining = total_color_map.get((material, color), 0.0)
        below = total_remaining <= threshold
        material_total_rows.append(
            {
                "material": material,
                "color": color,
                "threshold_g": round(float(threshold), 3),
                "total_remaining_g": round(float(total_remaining), 3),
                "missing_g": round(max(0.0, float(threshold) - float(total_remaining)), 3),
                "needs_reorder": below,
            }
        )

    reorder_critical_only = _is_truthy(request.query_params.get("reorder_critical") or "1")
    reorder_rows = _build_reorder_rows(db, project, presets, critical_only=reorder_critical_only)

    active_threshold_view = _normalize_threshold_view(request.query_params.get("view"))

    return render(
        request,
        "thresholds.html",
        {
            "material_rows": material_rows,
            "spool_rows": spool_rows,
            "spool_threshold_rows": spool_threshold_rows,
            "material_total_rows": material_total_rows,
            "reorder_rows": reorder_rows,
            "reorder_critical_only": reorder_critical_only,
            "materials": sorted(presets.get("materials", []), key=lambda x: str(x).lower()),
            "material_groups": presets.get("material_groups", []),
            "brands": sorted(presets.get("brands", []), key=lambda x: str(x).lower()),
            "colors": sorted(presets.get("colors", []), key=lambda x: str(x).lower()),
            "color_map": load_color_map(),
            "active_threshold_view": active_threshold_view,
        },
        lang,
    )


@app.post("/thresholds/spool")
def set_spool_threshold(
    request: Request,
    spool_id: int = Form(...),
    threshold_g: Optional[str] = Form(None),
    view: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        parsed = _parse_optional_float(threshold_g)
        spool.low_stock_threshold_g = None if parsed is None or parsed < 0 else round(float(parsed), 3)
        spool.updated_at = _utcnow()
        _audit_log(
            db,
            project,
            "threshold_spool_set",
            request=request,
            entity_type="spool",
            entity_id=spool.id,
            details={"threshold_g": spool.low_stock_threshold_g},
        )
        db.commit()
    return _thresholds_redirect(view)


@app.post("/thresholds/spool/delete")
def delete_spool_threshold(
    request: Request,
    spool_id: int = Form(...),
    view: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        spool.low_stock_threshold_g = None
        spool.updated_at = _utcnow()
        _audit_log(
            db,
            project,
            "threshold_spool_delete",
            request=request,
            entity_type="spool",
            entity_id=spool.id,
        )
        db.commit()
    return _thresholds_redirect(view)


@app.post("/thresholds/material-default")
def set_material_default_threshold(
    request: Request,
    material: str = Form(...),
    threshold_g: Optional[str] = Form(None),
    view: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("low_stock_thresholds", {})
    key = material.strip()
    if not key:
        return _thresholds_redirect(view)

    parsed = _parse_optional_float(threshold_g)
    if parsed is None or parsed < 0:
        thresholds.pop(key, None)
    else:
        thresholds[key] = round(float(parsed), 3)

    save_presets(presets)
    project = get_project(request)
    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "threshold_material_default_set",
            request=request,
            entity_type="material_threshold",
            entity_id=key,
            details={"threshold_g": thresholds.get(key)},
        )
        db_local.commit()
    finally:
        db_local.close()
    return _thresholds_redirect(view)


@app.post("/thresholds/material-default/delete")
def delete_material_default_threshold(
    request: Request,
    material: str = Form(...),
    view: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("low_stock_thresholds", {})
    key = material.strip()
    if key:
        thresholds.pop(key, None)
        save_presets(presets)
        project = get_project(request)
        db_local = SessionLocal()
        try:
            _audit_log(
                db_local,
                project,
                "threshold_material_default_delete",
                request=request,
                entity_type="material_threshold",
                entity_id=key,
            )
            db_local.commit()
        finally:
            db_local.close()
    return _thresholds_redirect(view)


def _render_storage_locations_page(
    request: Request,
    db: Session,
    lang: str,
    message: Optional[str] = None,
    error: Optional[str] = None,
    form_data: Optional[dict] = None,
):
    project = get_project(request)
    spool_scope_filters = _model_scope_filters(Spool, project)
    usage_rows = (
        db.query(Spool.storage_sub_location_id, func.count(Spool.id).label("count"))
        .filter(*spool_scope_filters, Spool.storage_sub_location_id.is_not(None))
        .group_by(Spool.storage_sub_location_id)
        .all()
    )
    usage_map = {int(location_id): int(count) for location_id, count in usage_rows if location_id}
    locations = _storage_location_options(db, project)
    for location in locations:
        location["usage_count"] = usage_map.get(int(location["id"]), 0)

    return render(
        request,
        "storage_locations.html",
        {
            "title": t_factory(lang)("storage_locations_title"),
            "locations": locations,
            "message": message,
            "error": error,
            "form_data": form_data or {},
        },
        lang,
    )


def _render_printers_page(
    request: Request,
    db: Session,
    lang: str,
    message: Optional[str] = None,
    error: Optional[str] = None,
    form_data: Optional[dict] = None,
    open_printer_id: Optional[int] = None,
    open_printer_tab: Optional[str] = None,
):
    project = get_project(request)
    t = t_factory(lang)
    printer_scope_filters = _model_scope_filters(Printer, project)
    printers = (
        db.query(Printer)
        .filter(*printer_scope_filters)
        .order_by(Printer.name.asc(), Printer.id.asc())
        .all()
    )

    slot_state_scope_filters = _model_scope_filters(DeviceSlotState, project)
    live_slot_states = (
        db.query(DeviceSlotState)
        .filter(*slot_state_scope_filters)
        .order_by(DeviceSlotState.printer_name.asc(), DeviceSlotState.slot.asc(), DeviceSlotState.id.asc())
        .all()
    )

    printer_has_ams_signal_by_serial: dict[str, bool] = {}
    printer_has_ams_signal_by_name: dict[str, bool] = {}
    for state in live_slot_states:
        slot_number = int(state.slot or 0)
        has_signal = (
            int(state.ams_unit or 0) > 0
            or int(state.slot_local or 0) > 0
            or slot_number >= 100
        )
        if not has_signal:
            continue
        state_serial = _normalize_printer_serial(state.printer_serial)
        if state_serial:
            printer_has_ams_signal_by_serial[state_serial] = True
        state_name = _normalize_printer_name(state.printer_name)
        if state_name:
            printer_has_ams_signal_by_name[state_name] = True

    slots_by_serial: dict[str, list[dict]] = {}
    slots_by_name: dict[str, list[dict]] = {}
    for state in live_slot_states:
        slot_number = int(state.slot or 0)
        if slot_number <= 0:
            continue

        inferred_ams_unit, inferred_slot_local = _infer_ams_slot_parts(slot_number)
        ams_unit = int(state.ams_unit or 0) or inferred_ams_unit or 1
        slot_local = int(state.slot_local or 0) or inferred_slot_local or slot_number
        ams_name = str(state.ams_name or "").strip() or None
        ams_label = _resolve_ams_label(ams_name, ams_unit)

        state_serial = _normalize_printer_serial(state.printer_serial)
        state_name = _normalize_printer_name(state.printer_name)
        should_canonicalize_slot = (
            bool(state_serial and printer_has_ams_signal_by_serial.get(state_serial))
            or bool(state_name and printer_has_ams_signal_by_name.get(state_name))
            or slot_number >= 100
        )
        canonical_slot = _compose_ams_global_slot(ams_unit, slot_local) if should_canonicalize_slot else slot_number
        slot_display = int(canonical_slot or slot_number)

        observed_color = _humanize_observed_color(state.observed_color)
        observed_parts = [
            str(state.observed_brand or "").strip(),
            str(state.observed_material or "").strip(),
            str(observed_color or "").strip(),
        ]
        observed_parts = [part for part in observed_parts if part]
        observed_label = " · ".join(observed_parts) if observed_parts else "-"

        slot_item = {
            "slot": slot_display,
            "slot_local": slot_local,
            "ams_unit": ams_unit,
            "ams_name": ams_name,
            "ams_label": ams_label,
            "observed": observed_label,
            "source": str(state.source or "").strip() or "-",
            "observed_at": state.observed_at,
        }

        if state_serial:
            slots_by_serial.setdefault(state_serial, []).append(slot_item)

        if state_name:
            slots_by_name.setdefault(state_name, []).append(slot_item)

    for values in slots_by_serial.values():
        values.sort(key=lambda item: int(item.get("slot") or 0))
    for values in slots_by_name.values():
        values.sort(key=lambda item: int(item.get("slot") or 0))

    rows: list[dict] = []
    observed_times: list[Optional[datetime]] = [state.observed_at for state in live_slot_states]
    for printer in printers:
        if not str(printer.name or "").strip() or not str(printer.serial or "").strip():
            continue
        status_value = _normalize_printer_status(printer.status)
        status_label_key = {
            "online": "printer_status_online",
            "offline": "printer_status_offline",
        }.get(status_value, "printer_status_unknown")

        normalized_serial = _normalize_printer_serial(printer.serial)
        normalized_name = _normalize_printer_name(printer.name)
        ams_name_map = _parse_ams_name_mapping(printer.ams_name_map)
        ams_slots = slots_by_serial.get(normalized_serial or "")
        if not ams_slots and normalized_name:
            ams_slots = slots_by_name.get(normalized_name, [])
        ams_groups: list[dict] = []
        if ams_slots:
            grouped: dict[tuple[int, str], list[dict]] = {}
            for item in ams_slots:
                group_unit = int(item.get("ams_unit") or 1)
                group_label = _resolve_ams_label(item.get("ams_name"), group_unit, ams_name_map)
                grouped.setdefault((group_unit, group_label), []).append(item)
            for key in sorted(grouped.keys(), key=lambda group_key: (int(group_key[0]), str(group_key[1]).lower())):
                group_items = grouped[key]
                group_unit = int(key[0])
                group_items.sort(key=lambda slot_item: (int(slot_item.get("slot_local") or 0), int(slot_item.get("slot") or 0)))
                ams_groups.append(
                    {
                        "ams_unit": group_unit,
                        "label": str(key[1]),
                        "mapped_name": str(ams_name_map.get(group_unit) or "").strip() or None,
                        "slots": group_items,
                    }
                )
        external_spool_active = _parse_optional_bool(printer.telemetry_external_spool_active) is True

        rows.append(
            {
                "id": printer.id,
                "name": printer.name,
                "serial": printer.serial,
                "host": printer.host,
                "port": printer.port,
                "access_code": printer.access_code,
                "ams_name_map": printer.ams_name_map,
                "is_active": bool(printer.is_active),
                "status": status_value,
                "status_label": t(status_label_key),
                "last_seen_at": printer.last_seen_at,
                "job_label": printer.telemetry_job_name or printer.telemetry_job_status,
                "job_status": printer.telemetry_job_status,
                "progress": printer.telemetry_progress,
                "temps": _format_printer_temperatures(printer),
                "nozzle_temp": printer.telemetry_nozzle_temp,
                "bed_temp": printer.telemetry_bed_temp,
                "chamber_temp": printer.telemetry_chamber_temp,
                "firmware": printer.telemetry_firmware,
                "error": printer.telemetry_error,
                "external_spool_active": external_spool_active,
                "external_spool_label": t("printer_external_spool_active") if external_spool_active else t("printer_external_spool_inactive"),
                "source": printer.last_source,
                "ams_slots": ams_slots or [],
                "ams_slot_groups": ams_groups,
            }
        )
        observed_times.append(printer.last_seen_at)

    slot_data_freshness = _summarize_slot_data_freshness(observed_times)

    return render(
        request,
        "printers.html",
        {
            "title": t("printers_title"),
            "printers": rows,
            "message": message,
            "error": error,
            "form_data": form_data or {},
            "open_printer_id": int(open_printer_id) if open_printer_id else None,
            "open_printer_tab": "ams" if str(open_printer_tab or "").strip().lower() == "ams" else "device",
            "slot_data_freshness": slot_data_freshness,
            "stale_minutes": SLOT_STATE_STALE_MINUTES,
        },
        lang,
    )


def _render_supplies_page(
    request: Request,
    db: Session,
    lang: str,
    message: Optional[str] = None,
    error: Optional[str] = None,
    form_data: Optional[dict] = None,
):
    project = get_project(request)
    t = t_factory(lang)
    rows = (
        db.query(SupplyItem)
        .filter(SupplyItem.project == project)
        .order_by(SupplyItem.category.asc(), SupplyItem.name.asc(), SupplyItem.id.asc())
        .all()
    )
    storage_location_options = _storage_location_options(db, project)
    storage_path_to_id = {
        str(item.get("path_code") or ""): int(item.get("id"))
        for item in storage_location_options
        if item.get("id") is not None
    }

    prepared_rows: list[dict] = []
    low_stock_count = 0
    for row in rows:
        quantity = round(float(row.quantity or 0.0), 3)
        minimum = round(float(row.min_quantity), 3) if row.min_quantity is not None else None
        is_low_stock = minimum is not None and quantity <= minimum
        if is_low_stock:
            low_stock_count += 1
        prepared_rows.append(
            {
                "id": row.id,
                "name": row.name,
                "category": row.category,
                "quantity": quantity,
                "unit": row.unit,
                "min_quantity": minimum,
                "location": row.location,
                "storage_sub_location_id": storage_path_to_id.get(str(row.location or "").strip()),
                "notes": row.notes,
                "is_low_stock": is_low_stock,
            }
        )

    category_rows = (
        db.query(SupplyCategory)
        .filter(SupplyCategory.project == project)
        .order_by(SupplyCategory.name.asc(), SupplyCategory.id.asc())
        .all()
    )
    categories = sorted(
        {
            *[str(item.name or "").strip() for item in category_rows if str(item.name or "").strip()],
            *[str(item.category or "").strip() for item in rows if str(item.category or "").strip()],
        },
        key=lambda value: value.lower(),
    )

    return render(
        request,
        "supplies.html",
        {
            "title": t("supplies_title"),
            "supplies_rows": prepared_rows,
            "supplies_low_stock_count": low_stock_count,
            "categories": categories,
            "storage_location_options": storage_location_options,
            "form_data": form_data or {},
            "message": message,
            "error": error,
        },
        lang,
    )


def _resolve_supply_location_path(
    db: Session,
    project: str,
    storage_sub_location_id: Optional[str],
) -> Optional[str]:
    raw = str(storage_sub_location_id or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        return None
    location = (
        db.query(StorageSubLocation)
        .filter(StorageSubLocation.project == project, StorageSubLocation.id == int(raw))
        .first()
    )
    if location is None:
        return None
    return str(location.path_code or "").strip() or None


@app.get("/printers")
def printers_page(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    open_printer_id_raw = str(request.query_params.get("open_printer_id") or "").strip()
    open_printer_tab_raw = str(request.query_params.get("open_printer_tab") or "").strip().lower()
    open_printer_id = None
    if open_printer_id_raw.isdigit():
        open_printer_id = int(open_printer_id_raw)
    return _render_printers_page(
        request,
        db,
        lang,
        open_printer_id=open_printer_id,
        open_printer_tab="ams" if open_printer_tab_raw == "ams" else "device",
    )


@app.get("/supplies")
def supplies_page(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    return _render_supplies_page(request, db, lang)


@app.post("/supplies")
def create_supply_item(
    request: Request,
    name: str = Form(""),
    category: Optional[str] = Form(None),
    storage_sub_location_id: Optional[str] = Form(None),
    quantity: Optional[str] = Form(None),
    unit: Optional[str] = Form(None),
    min_quantity: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    normalized_name = str(name or "").strip()[:120]
    normalized_category = str(category or "").strip()[:80] or t("supplies_default_category")
    parsed_quantity = _parse_optional_float(quantity)
    normalized_unit = str(unit or "").strip()[:32] or t("supplies_default_unit")
    parsed_min_quantity = _parse_optional_float(min_quantity)
    normalized_location = _resolve_supply_location_path(db, project, storage_sub_location_id)
    normalized_notes = str(notes or "").strip() or None

    form_data = {
        "name": name,
        "category": category,
        "quantity": quantity,
        "unit": unit,
        "min_quantity": min_quantity,
        "storage_sub_location_id": storage_sub_location_id,
        "notes": notes,
    }

    if not normalized_name or parsed_quantity is None or float(parsed_quantity) < 0:
        return _render_supplies_page(request, db, lang, error=t("supplies_invalid"), form_data=form_data)

    item = SupplyItem(
        project=project,
        name=normalized_name,
        category=normalized_category,
        quantity=round(float(parsed_quantity), 3),
        unit=normalized_unit,
        min_quantity=(round(float(parsed_min_quantity), 3) if parsed_min_quantity is not None and float(parsed_min_quantity) >= 0 else None),
        location=normalized_location,
        notes=normalized_notes,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(item)
    _audit_log(
        db,
        project,
        "supply_create",
        request=request,
        entity_type="supply_item",
        details={
            "name": normalized_name,
            "category": normalized_category,
            "quantity": item.quantity,
            "unit": normalized_unit,
            "location": item.location,
        },
    )
    db.commit()

    return _render_supplies_page(request, db, lang, message=t("supplies_saved"))


@app.post("/supplies/categories")
def create_supply_category(
    request: Request,
    name: str = Form(""),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    normalized_name = str(name or "").strip()[:80]
    if not normalized_name:
        return _render_supplies_page(request, db, lang, error=t("supplies_invalid"))

    exists = (
        db.query(SupplyCategory)
        .filter(SupplyCategory.project == project, SupplyCategory.name == normalized_name)
        .first()
    )
    if exists is not None:
        return _render_supplies_page(request, db, lang, error=t("supplies_category_exists"))

    db.add(
        SupplyCategory(
            project=project,
            name=normalized_name,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
    )
    _audit_log(
        db,
        project,
        "supply_category_create",
        request=request,
        entity_type="supply_category",
        entity_id=normalized_name,
    )
    db.commit()
    return _render_supplies_page(request, db, lang, message=t("supplies_category_saved"))


@app.post("/supplies/{supply_id}/adjust")
def adjust_supply_item(
    supply_id: int,
    request: Request,
    delta_quantity: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    item = (
        db.query(SupplyItem)
        .filter(SupplyItem.project == project, SupplyItem.id == supply_id)
        .first()
    )
    if item is None:
        return _render_supplies_page(request, db, lang, error=t("supplies_invalid"))

    delta = _parse_optional_float(delta_quantity)
    if delta is None:
        return _render_supplies_page(request, db, lang, error=t("supplies_invalid_adjust"))

    before = float(item.quantity or 0.0)
    after = max(0.0, before + float(delta))
    item.quantity = round(after, 3)
    item.updated_at = _utcnow()

    _audit_log(
        db,
        project,
        "supply_adjust",
        request=request,
        entity_type="supply_item",
        entity_id=item.id,
        details={
            "delta": round(float(delta), 3),
            "before": round(before, 3),
            "after": item.quantity,
        },
    )
    db.commit()
    return _render_supplies_page(request, db, lang, message=t("supplies_adjusted"))


@app.post("/supplies/{supply_id}/update")
def update_supply_item(
    supply_id: int,
    request: Request,
    name: str = Form(""),
    category: Optional[str] = Form(None),
    storage_sub_location_id: Optional[str] = Form(None),
    quantity: Optional[str] = Form(None),
    unit: Optional[str] = Form(None),
    min_quantity: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    item = (
        db.query(SupplyItem)
        .filter(SupplyItem.project == project, SupplyItem.id == supply_id)
        .first()
    )
    if item is None:
        return _render_supplies_page(request, db, lang, error=t("supplies_invalid"))

    normalized_name = str(name or "").strip()[:120]
    normalized_category = str(category or "").strip()[:80] or t("supplies_default_category")
    parsed_quantity = _parse_optional_float(quantity)
    normalized_unit = str(unit or "").strip()[:32] or t("supplies_default_unit")
    parsed_min_quantity = _parse_optional_float(min_quantity)
    normalized_location = _resolve_supply_location_path(db, project, storage_sub_location_id)
    normalized_notes = str(notes or "").strip() or None

    if not normalized_name or parsed_quantity is None or float(parsed_quantity) < 0:
        return _render_supplies_page(request, db, lang, error=t("supplies_invalid"))

    before = {
        "name": item.name,
        "category": item.category,
        "quantity": round(float(item.quantity or 0.0), 3),
        "unit": item.unit,
        "min_quantity": round(float(item.min_quantity), 3) if item.min_quantity is not None else None,
        "location": item.location,
        "notes": item.notes,
    }

    item.name = normalized_name
    item.category = normalized_category
    item.quantity = round(float(parsed_quantity), 3)
    item.unit = normalized_unit
    item.min_quantity = round(float(parsed_min_quantity), 3) if parsed_min_quantity is not None and float(parsed_min_quantity) >= 0 else None
    item.location = normalized_location
    item.notes = normalized_notes
    item.updated_at = _utcnow()

    _audit_log(
        db,
        project,
        "supply_update",
        request=request,
        entity_type="supply_item",
        entity_id=item.id,
        details={
            "before": before,
            "after": {
                "name": item.name,
                "category": item.category,
                "quantity": item.quantity,
                "unit": item.unit,
                "min_quantity": item.min_quantity,
                "location": item.location,
                "notes": item.notes,
            },
        },
    )
    db.commit()
    return _render_supplies_page(request, db, lang, message=t("supplies_updated"))


@app.post("/supplies/{supply_id}/delete")
def delete_supply_item(
    supply_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    item = (
        db.query(SupplyItem)
        .filter(SupplyItem.project == project, SupplyItem.id == supply_id)
        .first()
    )
    if item is None:
        return _render_supplies_page(request, db, lang)

    deleted_snapshot = {
        "name": item.name,
        "category": item.category,
        "quantity": round(float(item.quantity or 0.0), 3),
        "unit": item.unit,
    }
    db.delete(item)
    _audit_log(
        db,
        project,
        "supply_delete",
        request=request,
        entity_type="supply_item",
        entity_id=supply_id,
        details=deleted_snapshot,
    )
    db.commit()
    return _render_supplies_page(request, db, lang, message=t("supplies_deleted"))


@app.post("/printers")
def upsert_printer(
    request: Request,
    printer_id: Optional[str] = Form(None),
    name: str = Form(""),
    serial: str = Form(""),
    host: Optional[str] = Form(None),
    port: Optional[str] = Form(None),
    access_code: Optional[str] = Form(None),
    ams_name_map: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    normalized_name = _normalize_printer_name(name)
    normalized_serial = _normalize_printer_serial(serial)
    normalized_host = str(host or "").strip()[:255] or None
    normalized_access_code = str(access_code or "").strip()[:120] or None
    normalized_ams_name_map = str(ams_name_map or "").strip()[:500] or None
    normalized_port = _normalize_printer_port(port)
    active_value = True if is_active is None else _is_truthy(is_active)

    form_data = {
        "printer_id": printer_id,
        "name": name,
        "serial": serial,
        "host": host,
        "port": str(port or ""),
        "access_code": access_code,
        "ams_name_map": ams_name_map,
        "is_active": bool(active_value),
    }

    if not normalized_name or not normalized_serial:
        return _render_printers_page(request, db, lang, error=t("printer_invalid"), form_data=form_data)

    current: Optional[Printer] = None
    if printer_id is not None and str(printer_id).strip():
        try:
            parsed_id = int(str(printer_id).strip())
        except ValueError:
            parsed_id = 0
        if parsed_id > 0:
            current = (
                db.query(Printer)
                .filter(Printer.project == project, Printer.id == parsed_id)
                .first()
            )

    if current is None:
        current = Printer(project=project, status="unknown")
        db.add(current)

    current.name = normalized_name
    current.serial = normalized_serial
    current.host = normalized_host
    current.port = normalized_port
    current.access_code = normalized_access_code
    if ams_name_map is not None:
        current.ams_name_map = normalized_ams_name_map
    current.is_active = bool(active_value)
    current.updated_at = _utcnow()

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate_serial = (
            db.query(Printer)
            .filter(
                Printer.project == project,
                Printer.serial == normalized_serial,
                Printer.id != current.id,
            )
            .first()
        )
        error_key = "printer_duplicate_serial" if duplicate_serial else "printer_duplicate_name"
        return _render_printers_page(request, db, lang, error=t(error_key), form_data=form_data)

    return _render_printers_page(request, db, lang, message=t("printer_saved"))


@app.post("/printers/{printer_id}/ams-mapping")
def update_printer_ams_mapping(
    printer_id: int,
    request: Request,
    ams_unit: str = Form(""),
    ams_label: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    printer = (
        db.query(Printer)
        .filter(Printer.project == project, Printer.id == printer_id)
        .first()
    )
    if printer is None:
        return _render_printers_page(request, db, lang, error=t("printer_invalid"))

    unit = _normalize_ams_slot(ams_unit)
    if unit is None:
        return _render_printers_page(
            request,
            db,
            lang,
            open_printer_id=printer_id,
            open_printer_tab="ams",
            error=t("printer_invalid"),
        )

    mapping = _parse_ams_name_mapping(printer.ams_name_map)
    label = str(ams_label or "").strip()[:120]
    if label:
        mapping[unit] = label
    else:
        mapping.pop(unit, None)

    printer.ams_name_map = _serialize_ams_name_mapping(mapping)
    printer.updated_at = _utcnow()
    db.commit()

    resolved_label = _resolve_ams_label(None, unit, mapping)
    requested_with = str(request.headers.get("x-requested-with") or "").strip().lower()
    accepts = str(request.headers.get("accept") or "").lower()
    if requested_with == "xmlhttprequest" or "application/json" in accepts:
        return JSONResponse({"ok": True, "ams_unit": unit, "label": resolved_label, "mapped_name": mapping.get(unit)})

    return _render_printers_page(
        request,
        db,
        lang,
        open_printer_id=printer_id,
        open_printer_tab="ams",
        message=t("printer_saved"),
    )


@app.post("/printers/{printer_id}/delete")
def delete_printer(
    printer_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    printer = (
        db.query(Printer)
        .filter(Printer.project == project, Printer.id == printer_id)
        .first()
    )
    if printer is not None:
        db.delete(printer)
        db.commit()

    return _render_printers_page(request, db, lang, message=t("printer_deleted"))


@app.get("/storage-locations")
def storage_locations_page(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    return _render_storage_locations_page(request, db, lang)


@app.post("/storage-locations")
def create_storage_location(
    request: Request,
    area_code: str = Form(""),
    area_name: Optional[str] = Form(None),
    sub_code: str = Form(""),
    sub_name: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    normalized_area_code = _normalize_storage_area_code(area_code)
    normalized_sub_code = _normalize_storage_sub_code(sub_code)
    form_data = {
        "area_code": _normalize_storage_code(area_code),
        "area_name": str(area_name or "").strip(),
        "sub_code": _normalize_storage_code(sub_code),
        "sub_name": str(sub_name or "").strip(),
    }
    if normalized_area_code is None or normalized_sub_code is None:
        return _render_storage_locations_page(
            request,
            db,
            lang,
            error=t("storage_invalid_code"),
            form_data=form_data,
        )

    area = (
        db.query(StorageArea)
        .filter(StorageArea.project == project, StorageArea.code == normalized_area_code)
        .first()
    )
    if area is None:
        area = StorageArea(
            project=project,
            code=normalized_area_code,
            name=str(area_name or "").strip() or None,
        )
        db.add(area)
        db.flush()
    elif area_name is not None and str(area_name).strip():
        area.name = str(area_name).strip()
        area.updated_at = _utcnow()

    path_code = _storage_path_code(normalized_area_code, normalized_sub_code)
    existing = (
        db.query(StorageSubLocation)
        .filter(StorageSubLocation.project == project, StorageSubLocation.path_code == path_code)
        .first()
    )
    if existing is not None:
        db.rollback()
        return _render_storage_locations_page(
            request,
            db,
            lang,
            error=t("storage_location_exists"),
            form_data=form_data,
        )

    sub_location = StorageSubLocation(
        project=project,
        area_id=area.id,
        code=normalized_sub_code,
        path_code=path_code,
        name=str(sub_name or "").strip() or None,
    )
    db.add(sub_location)
    db.commit()
    return _render_storage_locations_page(request, db, lang, message=t("storage_location_saved"))


@app.post("/storage-locations/{sub_location_id}/delete")
def delete_storage_location(
    sub_location_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    sub_location = (
        db.query(StorageSubLocation)
        .filter(StorageSubLocation.project == project, StorageSubLocation.id == sub_location_id)
        .first()
    )
    if sub_location is None:
        return _render_storage_locations_page(request, db, lang)

    usage_count = (
        db.query(func.count(Spool.id))
        .filter(Spool.project == project, Spool.storage_sub_location_id == sub_location.id)
        .scalar()
        or 0
    )
    if int(usage_count) > 0:
        return _render_storage_locations_page(request, db, lang, error=t("storage_location_in_use"))

    db.delete(sub_location)
    db.commit()
    return _render_storage_locations_page(request, db, lang, message=t("storage_location_deleted"))


@app.get("/spools/new")
def new_spool(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    presets = load_presets()
    presets["color_map"] = load_color_map()
    return render(
        request,
        "spool_form.html",
        {
            "title": t_factory(lang)("add_spool"),
            "spool": None,
            "presets": presets,
            "lifecycle_status_options": _lifecycle_status_options(lang),
            "storage_location_options": _storage_location_options(db, project),
            "next_url": _normalize_next_url(request.query_params.get("next_url") or "/spools"),
        },
        lang,
    )


@app.get("/spools/bulk")
def bulk_spool_form(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    presets = load_presets()
    presets["color_map"] = load_color_map()
    return render(
        request,
        "bulk_add.html",
        {
            "title": t_factory(lang)("bulk_add"),
            "presets": presets,
            "lifecycle_status_options": _lifecycle_status_options(lang),
            "storage_location_options": _storage_location_options(db, project),
        },
        lang,
    )


@app.post("/spools/new")
def create_spool(
    request: Request,
    brand: str = Form(...),
    material: str = Form(...),
    color: str = Form(...),
    weight_g: float = Form(...),
    remaining_g: float = Form(...),
    low_stock_threshold_g: Optional[str] = Form(None),
    price: Optional[float] = Form(None),
    location: Optional[str] = Form(None),
    storage_sub_location_id: Optional[str] = Form(None),
    lifecycle_status: Optional[str] = Form(None),
    ams_printer: Optional[str] = Form(None),
    ams_slot: Optional[str] = Form(None),
    next_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)
    normalized_lifecycle_status = _normalize_lifecycle_status(lifecycle_status)
    normalized_ams_printer = _normalize_printer_name(ams_printer)
    normalized_ams_slot = _normalize_ams_slot_canonical(ams_slot)
    storage_sub_location, storage_error_key = _resolve_storage_sub_location(
        db,
        project,
        storage_sub_location_id,
    )

    _ensure_postgres_spool_sequence_when_empty(db)

    if storage_error_key:
        presets = load_presets()
        presets["color_map"] = load_color_map()
        spool_data = {
            "brand": brand,
            "material": material,
            "color": color,
            "weight_g": weight_g,
            "remaining_g": remaining_g,
            "low_stock_threshold_g": _parse_optional_float(low_stock_threshold_g),
            "price": price,
            "location": location,
            "storage_sub_location_id": _normalize_storage_sub_location_id(storage_sub_location_id),
            "lifecycle_status": normalized_lifecycle_status,
            "ams_printer": normalized_ams_printer,
            "ams_slot": normalized_ams_slot,
        }
        return render(
            request,
            "spool_form.html",
            {
                "title": t("add_spool"),
                "spool": spool_data,
                "presets": presets,
                "lifecycle_status_options": _lifecycle_status_options(lang),
                "storage_location_options": _storage_location_options(db, project),
                "error": t(storage_error_key),
                "next_url": _normalize_next_url(next_url or "/spools"),
            },
            lang,
        )

    conflict = _find_ams_slot_conflict(
        db,
        project=project,
        ams_printer=normalized_ams_printer,
        ams_slot=normalized_ams_slot,
    )
    if conflict is not None:
        presets = load_presets()
        presets["color_map"] = load_color_map()
        spool_data = {
            "brand": brand,
            "material": material,
            "color": color,
            "weight_g": weight_g,
            "remaining_g": remaining_g,
            "low_stock_threshold_g": _parse_optional_float(low_stock_threshold_g),
            "price": price,
            "location": location,
            "storage_sub_location_id": _normalize_storage_sub_location_id(storage_sub_location_id),
            "lifecycle_status": normalized_lifecycle_status,
            "ams_printer": normalized_ams_printer,
            "ams_slot": normalized_ams_slot,
        }
        return render(
            request,
            "spool_form.html",
            {
                "title": t_factory(lang)("add_spool"),
                "spool": spool_data,
                "presets": presets,
                "lifecycle_status_options": _lifecycle_status_options(lang),
                "storage_location_options": _storage_location_options(db, project),
                "error": t_factory(lang)("ams_slot_conflict"),
                "next_url": _normalize_next_url(next_url or "/spools"),
            },
            lang,
        )

    location_value = str(location or "").strip() or None
    if storage_sub_location is not None:
        location_value = storage_sub_location.path_code

    spool = Spool(
        brand=brand,
        material=material,
        color=color,
        weight_g=weight_g,
        remaining_g=remaining_g,
        low_stock_threshold_g=_parse_optional_float(low_stock_threshold_g),
        price=price,
        location=location_value,
        storage_sub_location_id=storage_sub_location.id if storage_sub_location else None,
        lifecycle_status=normalized_lifecycle_status,
        ams_printer=normalized_ams_printer,
        ams_slot=normalized_ams_slot,
        project=project,
    )
    _enforce_empty_lifecycle(spool)
    db.add(spool)
    db.flush()
    _audit_log(
        db,
        project,
        "spool_create",
        request=request,
        entity_type="spool",
        entity_id=spool.id,
        details={
            "material": spool.material,
            "color": spool.color,
            "remaining_g": round(float(spool.remaining_g or 0.0), 3),
        },
    )
    db.commit()
    return RedirectResponse(_normalize_next_url(next_url or "/spools"), status_code=303)


@app.post("/spools/bulk")
def create_spools_bulk(
    request: Request,
    brand: list[str] = Form(...),
    material: list[str] = Form(...),
    color: list[str] = Form(...),
    weight_g: list[float] = Form(...),
    remaining_g: list[float] = Form(...),
    lifecycle_status: list[Optional[str]] = Form([]),
    price: list[Optional[float]] = Form([]),
    location: list[Optional[str]] = Form([]),
    storage_sub_location_id: list[Optional[str]] = Form([]),
    quantity: list[int] = Form([]),
    next_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    _ensure_postgres_spool_sequence_when_empty(db)
    normalized_storage_ids: list[Optional[int]] = [
        _normalize_storage_sub_location_id(value) for value in storage_sub_location_id
    ]
    requested_storage_ids = sorted(
        {
            int(value)
            for value in normalized_storage_ids
            if isinstance(value, int) and value > 0
        }
    )
    storage_lookup: dict[int, StorageSubLocation] = {}
    if requested_storage_ids:
        storage_lookup = {
            int(item.id): item
            for item in (
                db.query(StorageSubLocation)
                .filter(
                    StorageSubLocation.project == project,
                    StorageSubLocation.id.in_(requested_storage_ids),
                )
                .all()
            )
        }

    count = len(brand)
    created_count = 0
    for i in range(count):
        if not brand[i] or not material[i] or not color[i]:
            continue
        qty = 1
        if i < len(quantity) and quantity[i]:
            try:
                qty = max(1, int(quantity[i]))
            except (TypeError, ValueError):
                qty = 1
        resolved_storage_id = normalized_storage_ids[i] if i < len(normalized_storage_ids) else None
        resolved_storage = (
            storage_lookup.get(int(resolved_storage_id))
            if isinstance(resolved_storage_id, int) and resolved_storage_id > 0
            else None
        )
        location_value = str(location[i]).strip() if i < len(location) and location[i] is not None else ""
        if resolved_storage is not None:
            location_value = resolved_storage.path_code
        normalized_location_value = location_value or None
        normalized_lifecycle_status = _normalize_lifecycle_status(
            lifecycle_status[i] if i < len(lifecycle_status) else None
        )

        for _ in range(qty):
            spool = Spool(
                brand=brand[i],
                material=material[i],
                color=color[i],
                weight_g=float(weight_g[i]),
                remaining_g=float(remaining_g[i]),
                lifecycle_status=normalized_lifecycle_status,
                price=float(price[i]) if i < len(price) and price[i] not in (None, "") else None,
                location=normalized_location_value,
                storage_sub_location_id=resolved_storage.id if resolved_storage else None,
                project=project,
            )
            _enforce_empty_lifecycle(spool)
            db.add(spool)
            created_count += 1
    _audit_log(
        db,
        project,
        "spool_bulk_create",
        request=request,
        entity_type="spool",
        details={"created_count": int(created_count)},
    )
    db.commit()
    return RedirectResponse(_normalize_next_url(next_url or "/spools"), status_code=303)


@app.get("/spools/{spool_id}/edit")
def edit_spool(spool_id: int, request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    presets = load_presets()
    presets["color_map"] = load_color_map()
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if not spool:
        return RedirectResponse("/spools", status_code=303)
    return render(
        request,
        "spool_form.html",
        {
            "title": t_factory(lang)("edit"),
            "spool": spool,
            "presets": presets,
            "lifecycle_status_options": _lifecycle_status_options(lang),
            "storage_location_options": _storage_location_options(db, project),
            "next_url": _normalize_next_url(request.query_params.get("next_url") or "/spools"),
        },
        lang,
    )


@app.get("/presets")
def presets_page(request: Request):
    lang = get_lang(request)
    presets = load_presets()
    color_map = load_color_map()
    return render(
        request,
        "presets.html",
        {"title": t_factory(lang)("presets_title"), "presets": presets, "color_map": color_map},
        lang,
    )


@app.post("/presets/brand")
def add_brand(name: str = Form(...)):
    presets = load_presets()
    names = [n.strip() for n in name.split(",") if n.strip()]
    for item in names:
        if item not in presets["brands"]:
            presets["brands"].append(item)
    presets["brands"].sort()
    save_presets(presets)
    return RedirectResponse("/presets", status_code=303)


@app.post("/presets/material")
def add_material(name: str = Form(...), group: str = Form("Custom")):
    presets = load_presets()
    group_label = group.strip() or "Custom"
    group_entry = next(
        (g for g in presets["material_groups"] if g.get("label") == group_label),
        None,
    )
    if not group_entry:
        group_entry = {"label": group_label, "items": []}
        presets["material_groups"].append(group_entry)
    names = [n.strip() for n in name.split(",") if n.strip()]
    for item in names:
        if item not in group_entry["items"]:
            group_entry["items"].append(item)
    group_entry["items"].sort()
    presets["materials"] = [
        item for g in presets["material_groups"] for item in g.get("items", [])
    ]
    save_presets(presets)
    return RedirectResponse("/presets", status_code=303)


@app.post("/presets/color")
def add_color(name: str = Form(...)):
    presets = load_presets()
    names = [n.strip() for n in name.split(",") if n.strip()]
    for item in names:
        if item not in presets["colors"]:
            presets["colors"].append(item)
    presets["colors"].sort()
    save_presets(presets)
    return RedirectResponse("/presets", status_code=303)


@app.post("/presets/color-map")
def add_color_map(
    brand: str = Form(...),
    material: str = Form(...),
    color: str = Form(None),
    colors: str = Form(None),
):
    color_map = load_color_map()
    color_map.setdefault(brand, {})
    color_map[brand].setdefault(material, [])
    raw = colors or color or ""
    items = [c.strip() for c in raw.replace("\n", ",").split(",") if c.strip()]
    for item in items:
        if item not in color_map[brand][material]:
            color_map[brand][material].append(item)
    save_color_map(color_map)
    return RedirectResponse("/presets", status_code=303)


@app.post("/presets/low-stock-threshold")
def set_material_low_stock_threshold(
    material: str = Form(...),
    threshold_g: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("low_stock_thresholds", {})
    key = material.strip()
    if not key:
        return RedirectResponse("/presets", status_code=303)

    parsed = _parse_optional_float(threshold_g)
    if parsed is None or parsed < 0:
        thresholds.pop(key, None)
    else:
        thresholds[key] = round(float(parsed), 3)

    save_presets(presets)
    return RedirectResponse("/presets", status_code=303)


@app.post("/thresholds/material-total")
def set_material_total_threshold(
    request: Request,
    material: str = Form(...),
    color: Optional[str] = Form(None),
    threshold_g: Optional[str] = Form(None),
    view: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("material_total_thresholds", {})
    material_key = material.strip()
    if not material_key:
        return _thresholds_redirect(view)

    color_key = (color or "").strip()
    if color_key in ("", "__ALL__"):
        color_key = "*"
    key = _material_color_key(material_key, color_key)

    parsed = _parse_optional_float(threshold_g)
    if parsed is None or parsed < 0:
        thresholds.pop(key, None)
    else:
        thresholds[key] = round(float(parsed), 3)

    save_presets(presets)
    project = get_project(request)
    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "threshold_material_total_set",
            request=request,
            entity_type="material_threshold",
            entity_id=key,
            details={"threshold_g": thresholds.get(key)},
        )
        db_local.commit()
    finally:
        db_local.close()
    return _thresholds_redirect(view)


@app.post("/thresholds/material-total/delete")
def delete_material_total_threshold(
    request: Request,
    material: str = Form(...),
    color: Optional[str] = Form(None),
    view: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("material_total_thresholds", {})
    material_key = material.strip()
    if not material_key:
        return _thresholds_redirect(view)

    color_key = (color or "").strip() or "*"
    thresholds.pop(_material_color_key(material_key, color_key), None)
    if color_key == "*":
        thresholds.pop(material_key, None)
    save_presets(presets)
    project = get_project(request)
    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "threshold_material_total_delete",
            request=request,
            entity_type="material_threshold",
            entity_id=_material_color_key(material_key, color_key),
        )
        db_local.commit()
    finally:
        db_local.close()
    return _thresholds_redirect(view)


@app.post("/presets/color-map/import")
def import_color_map(file: UploadFile = File(...)):
    import pandas as pd

    content, too_large = _read_upload_limited(file)
    if too_large or content is None:
        return RedirectResponse("/presets", status_code=303)

    name = (file.filename or "").lower()
    if name.endswith(".csv"):
        df = pd.read_csv(BytesIO(content))
    elif name.endswith(".xlsx"):
        df = pd.read_excel(BytesIO(content))
    else:
        return RedirectResponse("/presets", status_code=303)

    color_map = load_color_map()
    presets = load_presets()

    for _, row in df.iterrows():
        brand = str(row.get("brand", "")).strip()
        material = str(row.get("material", "")).strip()
        color = str(row.get("color", "")).strip()
        if not brand or not material or not color:
            continue
        color_map.setdefault(brand, {})
        color_map[brand].setdefault(material, [])
        if color not in color_map[brand][material]:
            color_map[brand][material].append(color)
        if brand and brand not in presets["brands"]:
            presets["brands"].append(brand)
        if material and material not in presets["materials"]:
            presets["materials"].append(material)
        if color and color not in presets["colors"]:
            presets["colors"].append(color)

    presets["brands"].sort()
    presets["materials"].sort()
    presets["colors"].sort()
    save_color_map(color_map)
    save_presets(presets)

    return RedirectResponse("/presets", status_code=303)


@app.post("/spools/{spool_id}/edit")
def update_spool(
    spool_id: int,
    request: Request,
    brand: str = Form(...),
    material: str = Form(...),
    color: str = Form(...),
    weight_g: float = Form(...),
    remaining_g: float = Form(...),
    low_stock_threshold_g: Optional[str] = Form(None),
    price: Optional[float] = Form(None),
    storage_sub_location_id: Optional[str] = Form(None),
    lifecycle_status: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        normalized_lifecycle_status = _normalize_lifecycle_status(lifecycle_status)
        storage_sub_location, storage_error_key = _resolve_storage_sub_location(
            db,
            project,
            storage_sub_location_id,
        )
        if storage_error_key:
            presets = load_presets()
            presets["color_map"] = load_color_map()
            spool_data = {
                "id": spool.id,
                "brand": brand,
                "material": material,
                "color": color,
                "weight_g": weight_g,
                "remaining_g": remaining_g,
                "low_stock_threshold_g": _parse_optional_float(low_stock_threshold_g),
                "price": price,
                "storage_sub_location_id": _normalize_storage_sub_location_id(storage_sub_location_id),
                "lifecycle_status": normalized_lifecycle_status,
            }
            return render(
                request,
                "spool_form.html",
                {
                    "title": t("edit"),
                    "spool": spool_data,
                    "presets": presets,
                    "lifecycle_status_options": _lifecycle_status_options(lang),
                    "storage_location_options": _storage_location_options(db, project),
                    "error": t(storage_error_key),
                    "next_url": _normalize_next_url(request.query_params.get("next_url") or "/spools"),
                },
                lang,
            )

        location_value = spool.location
        if storage_sub_location is not None:
            location_value = storage_sub_location.path_code

        spool.brand = brand
        spool.material = material
        spool.color = color
        spool.weight_g = weight_g
        spool.remaining_g = remaining_g
        spool.low_stock_threshold_g = _parse_optional_float(low_stock_threshold_g)
        spool.price = price
        spool.location = location_value
        spool.storage_sub_location_id = storage_sub_location.id if storage_sub_location else None
        spool.lifecycle_status = normalized_lifecycle_status
        _enforce_empty_lifecycle(spool)
        spool.updated_at = _utcnow()
        _audit_log(
            db,
            project,
            "spool_update",
            request=request,
            entity_type="spool",
            entity_id=spool.id,
            details={
                "material": spool.material,
                "color": spool.color,
                "remaining_g": round(float(spool.remaining_g or 0.0), 3),
                "threshold_g": spool.low_stock_threshold_g,
            },
        )
        db.commit()
    next_url = request.query_params.get("next_url")
    return RedirectResponse(_normalize_next_url(next_url or "/spools"), status_code=303)


@app.post("/spools/{spool_id}/delete")
def delete_spool(
    spool_id: int,
    request: Request,
    next_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        _audit_log(
            db,
            project,
            "spool_delete",
            request=request,
            entity_type="spool",
            entity_id=spool.id,
            details={
                "material": spool.material,
                "color": spool.color,
            },
        )
        db.delete(spool)
        db.commit()
    return RedirectResponse(_normalize_next_url(next_url or "/spools"), status_code=303)


@app.post("/spools/{spool_id}/toggle-use")
def toggle_spool_use(
    spool_id: int,
    request: Request,
    next_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        spool.in_use = not spool.in_use
        _enforce_empty_lifecycle(spool)
        spool.updated_at = _utcnow()
        _audit_log(
            db,
            project,
            "spool_toggle_use",
            request=request,
            entity_type="spool",
            entity_id=spool.id,
            details={"in_use": bool(spool.in_use)},
        )
        db.commit()
    return RedirectResponse(_normalize_next_url(next_url or "/spools"), status_code=303)


@app.get("/spools/{spool_id}/qr")
def spool_qr(spool_id: int, request: Request, db: Session = Depends(get_db)):
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if not spool:
        return RedirectResponse("/", status_code=303)
    data = f"spool:{spool.id}:{spool.brand}:{spool.material}:{spool.color}"
    png = generate_qr_png(data)
    return StreamingResponse(BytesIO(png), media_type="image/png")


@app.get("/storage-locations/{sub_location_id}/qr")
def storage_location_qr(sub_location_id: int, request: Request, db: Session = Depends(get_db)):
    project = get_project(request)
    location = (
        db.query(StorageSubLocation)
        .filter(StorageSubLocation.project == project, StorageSubLocation.id == sub_location_id)
        .first()
    )
    if not location:
        return RedirectResponse("/storage-locations", status_code=303)
    data = f"location:{project}:{location.path_code}"
    png = generate_qr_png(data)
    return StreamingResponse(BytesIO(png), media_type="image/png")


@app.get("/printers/{printer_id}/qr")
def printer_qr(printer_id: int, request: Request, db: Session = Depends(get_db)):
    project = get_project(request)
    printer = db.query(Printer).filter(Printer.id == printer_id, Printer.project == project).first()
    if not printer:
        return RedirectResponse("/printers", status_code=303)
    data = f"printer:{project}:{printer.id}:{printer.name}:{printer.serial}"
    png = generate_qr_png(data)
    return StreamingResponse(BytesIO(png), media_type="image/png")


@app.get("/qr-scan")
def qr_scan_page(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    t = t_factory(lang)
    notice_key = str(request.query_params.get("notice") or "").strip()
    notice_message = t(notice_key) if notice_key in {"qr_scan_next_ready", "qr_scan_location_loaded", "qr_scan_printer_loaded"} else None
    return render(
        request,
        "qr_scan.html",
        {
            "qr_payload": "",
            "message": notice_message,
        },
        lang,
    )


@app.post("/qr-scan")
def qr_scan_lookup(
    request: Request,
    qr_payload: str = Form(""),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)

    spool_id = _extract_spool_id_from_qr_payload(qr_payload)
    if spool_id is not None:
        spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
        if not spool:
            return render(
                request,
                "qr_scan.html",
                {
                    "qr_payload": qr_payload,
                    "error": t("qr_scan_not_found"),
                },
                lang,
            )
        return RedirectResponse(f"/qr-scan/manage/{spool.id}", status_code=303)

    location_path = _extract_location_path_from_qr_payload(qr_payload, project)
    if location_path:
        location = (
            db.query(StorageSubLocation)
            .filter(StorageSubLocation.project == project, StorageSubLocation.path_code == location_path)
            .first()
        )
        if location:
            query = urlencode({"location_id": location.id, "hide_empty": "false", "notice": "qr_scan_location_loaded"})
            return RedirectResponse(f"/spools?{query}", status_code=303)

    printer_id = _extract_printer_id_from_qr_payload(qr_payload, project)
    if printer_id is not None:
        printer = db.query(Printer).filter(Printer.id == printer_id, Printer.project == project).first()
        if printer:
            query = urlencode({"open_printer_id": printer.id, "notice": "qr_scan_printer_loaded"})
            return RedirectResponse(f"/printers?{query}", status_code=303)

    return render(
        request,
        "qr_scan.html",
        {
            "qr_payload": qr_payload,
            "error": t("qr_scan_invalid"),
        },
        lang,
    )


@app.get("/qr-scan/manage/{spool_id}")
def qr_scan_manage_page(
    spool_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)

    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if not spool:
        return render(
            request,
            "qr_scan.html",
            {
                "qr_payload": "",
                "error": t("qr_scan_not_found"),
            },
            lang,
        )

    printers = (
        db.query(Printer)
        .filter(Printer.project == project)
        .order_by(Printer.name.asc(), Printer.id.asc())
        .all()
    )

    return render(
        request,
        "qr_scan_manage.html",
        {
            "spool": spool,
            "printers": printers,
            "storage_location_options": _storage_location_options(db, project),
            "spool_status_key": _spool_status_key(spool),
            "lifecycle_status_options": _lifecycle_status_options(lang),
        },
        lang,
    )


@app.post("/qr-scan/action")
def qr_scan_action(
    request: Request,
    spool_id: int = Form(...),
    action: str = Form(""),
    lifecycle_status: Optional[str] = Form(None),
    storage_sub_location_id: Optional[str] = Form(None),
    mapping_target: Optional[str] = Form(None),
    ams_printer: Optional[str] = Form(None),
    ams_slot: Optional[str] = Form(None),
    return_to_scan: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)

    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if not spool:
        return render(
            request,
            "qr_scan.html",
            {
                "qr_payload": "",
                "error": t("qr_scan_not_found"),
            },
            lang,
        )

    printers = (
        db.query(Printer)
        .filter(Printer.project == project)
        .order_by(Printer.name.asc(), Printer.id.asc())
        .all()
    )

    def _render_manage(error_key: Optional[str] = None, message: Optional[str] = None):
        return render(
            request,
            "qr_scan_manage.html",
            {
                "spool": spool,
                "printers": printers,
                "storage_location_options": _storage_location_options(db, project),
                "spool_status_key": _spool_status_key(spool),
                "lifecycle_status_options": _lifecycle_status_options(lang),
                "error": t(error_key) if error_key else None,
                "message": message,
            },
            lang,
        )

    action_key = str(action or "").strip().lower()
    message_key: Optional[str] = None
    if action_key == "set_empty":
        spool.remaining_g = 0.0
        _enforce_empty_lifecycle(spool)
        message_key = "qr_scan_action_done_empty"
    elif action_key == "set_in_use":
        spool.in_use = True
        message_key = "qr_scan_action_done_in_use"
    elif action_key == "set_idle":
        spool.in_use = False
        message_key = "qr_scan_action_done_idle"
    elif action_key == "set_lifecycle":
        lifecycle_candidate = str(lifecycle_status or "").strip().lower().replace("-", "_")
        if lifecycle_candidate not in LIFECYCLE_STATUS_VALUES:
            return _render_manage(error_key="qr_scan_action_invalid_lifecycle")
        spool.lifecycle_status = lifecycle_candidate
        _enforce_empty_lifecycle(spool)
        message_key = "qr_scan_action_done_lifecycle"
    elif action_key == "set_storage":
        storage_sub_location, storage_error_key = _resolve_storage_sub_location(
            db,
            project,
            storage_sub_location_id,
        )
        if storage_error_key:
            return _render_manage(error_key=storage_error_key)
        spool.storage_sub_location_id = storage_sub_location.id if storage_sub_location else None
        spool.location = storage_sub_location.path_code if storage_sub_location else None
        _enforce_empty_lifecycle(spool)
        message_key = "qr_scan_action_done_storage"
    elif action_key == "set_ams_mapping":
        target = str(mapping_target or "").strip().lower()
        normalized_printer = _normalize_printer_name(ams_printer)
        normalized_slot = _normalize_ams_slot_canonical(ams_slot)

        if target == "clear":
            spool.ams_printer = None
            spool.ams_slot = None
            message_key = "qr_scan_action_done_mapping"
        elif target == "ams":
            if normalized_slot is None:
                return _render_manage(error_key="qr_scan_action_invalid_mapping")
            conflict = _find_ams_slot_conflict(
                db,
                project=project,
                ams_printer=normalized_printer,
                ams_slot=normalized_slot,
                exclude_spool_id=spool.id,
            )
            if conflict is not None:
                return _render_manage(error_key="qr_scan_action_mapping_conflict")
            spool.ams_printer = normalized_printer
            spool.ams_slot = normalized_slot
            message_key = "qr_scan_action_done_mapping"
        elif target == "external":
            if not normalized_printer:
                return _render_manage(error_key="qr_scan_action_invalid_mapping_printer")
            conflict = (
                db.query(Spool)
                .filter(
                    Spool.project == project,
                    Spool.id != spool.id,
                    Spool.ams_printer == normalized_printer,
                    Spool.ams_slot.is_(None),
                )
                .order_by(Spool.id.asc())
                .first()
            )
            if conflict is not None:
                return _render_manage(error_key="qr_scan_action_mapping_conflict")
            spool.ams_printer = normalized_printer
            spool.ams_slot = None
            message_key = "qr_scan_action_done_mapping"
        else:
            return _render_manage(error_key="qr_scan_action_invalid_mapping")
    else:
        return _render_manage(error_key="qr_scan_action_invalid")

    spool.updated_at = _utcnow()
    db.commit()
    db.refresh(spool)

    if _is_truthy(return_to_scan):
        return RedirectResponse(f"/qr-scan?{urlencode({'notice': 'qr_scan_next_ready'})}", status_code=303)

    return _render_manage(message=t(message_key) if message_key else None)


@app.get("/labels")
def labels_form(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    requested_target = str(request.query_params.get("target") or "").strip().lower()
    stored_target = str(request.cookies.get(LABEL_TARGET_SETTING_KEY) or _load_setting_from_db(LABEL_TARGET_SETTING_KEY) or "").strip().lower()
    effective_target = requested_target if requested_target in {"spool", "location", "printer"} else (
        stored_target if stored_target in {"spool", "location", "printer"} else "spool"
    )
    requested_view = str(request.query_params.get("view") or "").strip().lower()
    active_label_view = requested_view if requested_view in {"spool", "location", "printer", "formats"} else effective_target
    layouts_map = _all_label_layouts()
    prefs = _load_label_print_preferences(request)
    selected_layout = _normalize_label_layout(request.cookies.get("label_layout") or _load_setting_from_db("label_layout"), layouts_map)

    def _parse_query_int_list(param_name: str) -> list[int]:
        values = request.query_params.getlist(param_name)
        parsed: list[int] = []
        for raw in values:
            try:
                value = int(str(raw or "").strip())
            except (TypeError, ValueError):
                continue
            if value > 0:
                parsed.append(value)
        return parsed

    def _build_preview_url(
        *,
        label_target: str,
        selected_ids: list[int],
        selected_location_ids: list[int],
        selected_printer_ids: list[int],
        layout: str,
        print_mode: str,
        label_orientation: str,
        label_content: dict,
    ) -> str:
        query_params: list[tuple[str, str]] = [
            ("preview", "1"),
            ("target", label_target),
            ("view", label_target),
            ("layout", layout),
            ("print_mode", print_mode),
            ("label_orientation", label_orientation),
        ]

        for field in ("show_spool_id", "show_brand", "show_material_color", "show_weight", "show_remaining", "show_location"):
            if _is_truthy(label_content.get(field)):
                query_params.append((field, "1"))

        for value in selected_ids:
            query_params.append(("spool_ids", str(value)))
        for value in selected_location_ids:
            query_params.append(("storage_location_ids", str(value)))
        for value in selected_printer_ids:
            query_params.append(("printer_ids", str(value)))

        return f"/labels?{urlencode(query_params)}"

    def _build_label_items(
        *,
        label_target: str,
        selected_ids: list[int],
        selected_location_ids: list[int],
        selected_printer_ids: list[int],
    ) -> list[dict]:
        label_items: list[dict] = []
        if label_target == "location":
            selected_locations = (
                db.query(StorageSubLocation)
                .filter(StorageSubLocation.project == project, StorageSubLocation.id.in_(selected_location_ids))
                .order_by(StorageSubLocation.path_code.asc())
                .all()
            )
            for location in selected_locations:
                label_items.append(
                    {
                        "qr_src": f"/storage-locations/{location.id}/qr",
                        "line_title": location.path_code,
                        "line_brand": location.name or "",
                        "line_material_color": "",
                        "line_weight": "",
                        "line_remaining": "",
                        "line_location": location.path_code,
                    }
                )
        elif label_target == "printer":
            selected_printers = (
                db.query(Printer)
                .filter(Printer.project == project, Printer.id.in_(selected_printer_ids))
                .order_by(Printer.name.asc(), Printer.id.asc())
                .all()
            )
            for printer in selected_printers:
                label_items.append(
                    {
                        "qr_src": f"/printers/{printer.id}/qr",
                        "line_title": printer.name,
                        "line_brand": printer.serial,
                        "line_material_color": f"{printer.host or '-'}:{printer.port or '-'}",
                        "line_weight": "",
                        "line_remaining": "",
                        "line_location": "",
                    }
                )
        else:
            selected_spools = (
                db.query(Spool)
                .filter(Spool.project == project, Spool.id.in_(selected_ids))
                .order_by(Spool.id.asc())
                .all()
            )
            storage_map = _storage_location_map_by_id(
                db,
                project,
                [int(spool.storage_sub_location_id) for spool in selected_spools if spool.storage_sub_location_id],
            )
            for spool in selected_spools:
                label_items.append(
                    {
                        "qr_src": f"/spools/{spool.id}/qr",
                        "line_title": f"SP-{spool.id:04d}",
                        "line_brand": spool.brand,
                        "line_material_color": f"{spool.material} · {spool.color}",
                        "line_weight": format_weight_text(spool.weight_g),
                        "line_remaining": format_weight_text(spool.remaining_g),
                        "line_location": _spool_location_display(spool, storage_map),
                    }
                )
        return label_items

    preview_requested = _is_truthy(request.query_params.get("preview"))
    if preview_requested:
        preview_target = str(request.query_params.get("target") or effective_target).strip().lower()
        if preview_target not in {"spool", "location", "printer"}:
            preview_target = "spool"

        preview_layout = _normalize_label_layout(request.query_params.get("layout") or selected_layout, layouts_map)
        preview_print_mode = _normalize_label_print_mode(request.query_params.get("print_mode") or prefs["print_mode"])
        preview_orientation = _normalize_label_orientation(request.query_params.get("label_orientation") or prefs["label_orientation"])

        preview_content = _build_label_content_settings(prefs.get("label_content") or _default_label_content_settings())
        if any(
            request.query_params.get(field) is not None
            for field in ("show_spool_id", "show_brand", "show_material_color", "show_weight", "show_remaining", "show_location")
        ):
            preview_content = _build_label_content_settings(
                {
                    "show_spool_id": _is_truthy(request.query_params.get("show_spool_id")),
                    "show_brand": _is_truthy(request.query_params.get("show_brand")),
                    "show_material_color": _is_truthy(request.query_params.get("show_material_color")),
                    "show_weight": _is_truthy(request.query_params.get("show_weight")),
                    "show_remaining": _is_truthy(request.query_params.get("show_remaining")),
                    "show_location": _is_truthy(request.query_params.get("show_location")),
                }
            )

        preview_spool_ids = _parse_query_int_list("spool_ids")
        preview_location_ids = _parse_query_int_list("storage_location_ids")
        preview_printer_ids = _parse_query_int_list("printer_ids")

        has_selection = (
            (preview_target == "spool" and bool(preview_spool_ids))
            or (preview_target == "location" and bool(preview_location_ids))
            or (preview_target == "printer" and bool(preview_printer_ids))
        )

        if has_selection:
            preview_url = _build_preview_url(
                label_target=preview_target,
                selected_ids=preview_spool_ids,
                selected_location_ids=preview_location_ids,
                selected_printer_ids=preview_printer_ids,
                layout=preview_layout,
                print_mode=preview_print_mode,
                label_orientation=preview_orientation,
                label_content=preview_content,
            )
            return render(
                request,
                "labels_print.html",
                {
                    "label_items": _build_label_items(
                        label_target=preview_target,
                        selected_ids=preview_spool_ids,
                        selected_location_ids=preview_location_ids,
                        selected_printer_ids=preview_printer_ids,
                    ),
                    "label_target": preview_target,
                    "layout": preview_layout,
                    "print_mode": preview_print_mode,
                    "label_orientation": preview_orientation,
                    "label_content": preview_content,
                    "layout_config": _resolve_label_layout_for_print(layouts_map[preview_layout]),
                    "preview_url": preview_url,
                },
                lang,
            )

    spools = (
        db.query(Spool)
        .filter(Spool.project == project)
        .order_by(Spool.id.asc())
        .all()
    )
    return render(
        request,
        "labels.html",
        {
            "spools": spools,
            "storage_locations": _storage_location_options(db, project),
            "printers": db.query(Printer).filter(Printer.project == project).order_by(Printer.name.asc(), Printer.id.asc()).all(),
            "label_target": effective_target,
            "selected_ids": [],
            "selected_location_ids": [],
            "selected_printer_ids": [],
            "layout": selected_layout,
            "print_mode": prefs["print_mode"],
            "label_orientation": prefs["label_orientation"],
            "label_content": prefs["label_content"],
            "active_label_view": active_label_view,
            "layout_choices": _get_label_layout_choices(lang, layouts_map),
            "custom_layouts": [item for item in _get_label_layout_choices(lang, layouts_map) if item.get("is_custom")],
        },
        lang,
    )


@app.post("/labels/preferences")
def save_label_preferences(
    request: Request,
    label_target: str = Form("spool"),
    layout: str = Form(DEFAULT_LABEL_LAYOUT),
    print_mode: str = Form(DEFAULT_LABEL_PRINT_MODE),
    label_orientation: str = Form(DEFAULT_LABEL_ORIENTATION),
    show_spool_id: Optional[str] = Form(None),
    show_brand: Optional[str] = Form(None),
    show_material_color: Optional[str] = Form(None),
    show_weight: Optional[str] = Form(None),
    show_remaining: Optional[str] = Form(None),
    show_location: Optional[str] = Form(None),
):
    normalized_label_target = str(label_target or "").strip().lower()
    if normalized_label_target not in {"spool", "location", "printer"}:
        normalized_label_target = "spool"
    layouts_map = _all_label_layouts()
    valid_layout = _normalize_label_layout(layout, layouts_map)
    valid_print_mode = _normalize_label_print_mode(print_mode)
    valid_label_orientation = _normalize_label_orientation(label_orientation)
    label_content = _build_label_content_settings(
        {
            "show_spool_id": _is_truthy(show_spool_id),
            "show_brand": _is_truthy(show_brand),
            "show_material_color": _is_truthy(show_material_color),
            "show_weight": _is_truthy(show_weight),
            "show_remaining": _is_truthy(show_remaining),
            "show_location": _is_truthy(show_location),
        }
    )

    response = JSONResponse({"ok": True})
    _set_cookie(response, LABEL_TARGET_SETTING_KEY, normalized_label_target, request=request)
    _set_cookie(response, "label_layout", valid_layout, request=request)
    _save_setting_to_db(LABEL_TARGET_SETTING_KEY, normalized_label_target)
    _save_setting_to_db("label_layout", valid_layout)
    _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
    return response


@app.post("/labels/layouts")
def add_custom_label_layout(
    request: Request,
    layout_name: str = Form(""),
    cell_w_mm: str = Form(""),
    cell_h_mm: str = Form(""),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)

    name = str(layout_name or "").strip()
    cell_w_value = float(_parse_optional_float(cell_w_mm) or 0)
    cell_h_value = float(_parse_optional_float(cell_h_mm) or 0)

    error_key: Optional[str] = None
    if not name:
        error_key = "label_custom_error_name"
    elif cell_w_value <= 0 or cell_h_value <= 0:
        error_key = "label_custom_error_size"

    layout_key = ""
    if not error_key:
        normalized_name = unicodedata.normalize("NFKD", name)
        ascii_name = normalized_name.encode("ascii", "ignore").decode("ascii")
        layout_key = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")
        if not layout_key:
            layout_key = re.sub(r"\W+", "_", name.lower(), flags=re.UNICODE).strip("_")
        if not layout_key:
            layout_key = f"layout_{uuid4().hex[:8]}"

    presets = load_presets()
    custom_layouts: dict[str, dict] = {}
    presets_layouts = presets.get("custom_label_layouts")
    if isinstance(presets_layouts, dict):
        for key, value in presets_layouts.items():
            if isinstance(value, dict):
                custom_layouts[str(key)] = value

    db_layouts = _load_custom_label_layouts_from_db()
    if isinstance(db_layouts, dict):
        for key, value in db_layouts.items():
            if isinstance(value, dict):
                custom_layouts[str(key)] = value

    all_layouts = _all_label_layouts()
    if not error_key and layout_key in all_layouts:
        error_key = "label_custom_error_exists"

    if not error_key:
        layout_payload = {
            "label_de": name,
            "label_en": name,
            "cell_w_mm": round(cell_w_value, 2),
            "cell_h_mm": round(cell_h_value, 2),
        }
        custom_layouts[layout_key] = layout_payload
        presets["custom_label_layouts"] = custom_layouts
        try:
            save_presets(presets)
        except Exception:
            logger.warning("Could not write presets file while saving custom label layout '%s'", layout_key, exc_info=True)
        _save_setting_to_db(
            f"{CUSTOM_LABEL_LAYOUT_SETTING_PREFIX}{layout_key}",
            json.dumps(layout_payload, ensure_ascii=False),
        )
        _delete_setting_from_db(f"{CUSTOM_LABEL_LAYOUT_DELETED_PREFIX}{layout_key}")

    spools = (
        db.query(Spool)
        .filter(Spool.project == project)
        .order_by(Spool.id.asc())
        .all()
    )
    layouts_map = _all_label_layouts()
    layout_choices = _get_label_layout_choices(lang, layouts_map)
    selected_layout = layout_key if not error_key else DEFAULT_LABEL_LAYOUT

    return render(
        request,
        "labels.html",
        {
            "spools": spools,
            "storage_locations": _storage_location_options(db, project),
            "label_target": "spool",
            "selected_ids": [],
            "selected_location_ids": [],
            "layout": _normalize_label_layout(selected_layout, layouts_map),
            "print_mode": DEFAULT_LABEL_PRINT_MODE,
            "label_orientation": DEFAULT_LABEL_ORIENTATION,
            "label_content": _default_label_content_settings(),
            "active_label_view": "formats",
            "layout_choices": layout_choices,
            "custom_layouts": [item for item in layout_choices if item.get("is_custom")],
            "message": t("label_custom_saved") if not error_key else None,
            "error": t(error_key) if error_key else None,
        },
        lang,
    )


@app.post("/labels/layouts/delete")
def delete_custom_label_layout(
    request: Request,
    layout_key: str = Form(""),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)

    normalized_key = str(layout_key or "").strip()
    error_key: Optional[str] = None
    message_key: Optional[str] = None

    if not normalized_key:
        error_key = "label_custom_error_name"
    elif normalized_key in LABEL_LAYOUTS:
        error_key = "label_custom_error_delete_builtin"
    else:
        try:
            _delete_setting_from_db(f"{CUSTOM_LABEL_LAYOUT_SETTING_PREFIX}{normalized_key}")
            _save_setting_to_db(f"{CUSTOM_LABEL_LAYOUT_DELETED_PREFIX}{normalized_key}", "1")

            presets = load_presets()
            presets_layouts = presets.get("custom_label_layouts")
            if isinstance(presets_layouts, dict):
                presets_layouts.pop(normalized_key, None)
                presets["custom_label_layouts"] = presets_layouts
                try:
                    save_presets(presets)
                except Exception:
                    logger.warning("Could not write presets file while deleting custom label layout '%s'", normalized_key, exc_info=True)

            legacy_raw = _load_setting_from_db(CUSTOM_LABEL_LAYOUTS_SETTING_KEY)
            if legacy_raw:
                try:
                    legacy_payload = json.loads(legacy_raw)
                    if isinstance(legacy_payload, dict) and normalized_key in legacy_payload:
                        legacy_payload.pop(normalized_key, None)
                        _save_setting_to_db(CUSTOM_LABEL_LAYOUTS_SETTING_KEY, json.dumps(legacy_payload, ensure_ascii=False))
                except Exception:
                    pass

            message_key = "label_custom_deleted"
        except Exception:
            logger.exception("Failed to delete custom label layout: %s", normalized_key)
            error_key = "label_custom_error_delete_failed"

    spools = (
        db.query(Spool)
        .filter(Spool.project == project)
        .order_by(Spool.id.asc())
        .all()
    )
    layouts_map = _all_label_layouts()
    layout_choices = _get_label_layout_choices(lang, layouts_map)

    return render(
        request,
        "labels.html",
        {
            "spools": spools,
            "storage_locations": _storage_location_options(db, project),
            "label_target": "spool",
            "selected_ids": [],
            "selected_location_ids": [],
            "layout": DEFAULT_LABEL_LAYOUT,
            "print_mode": DEFAULT_LABEL_PRINT_MODE,
            "label_orientation": DEFAULT_LABEL_ORIENTATION,
            "label_content": _default_label_content_settings(),
            "active_label_view": "formats",
            "layout_choices": layout_choices,
            "custom_layouts": [item for item in layout_choices if item.get("is_custom")],
            "message": t(message_key) if message_key else None,
            "error": t(error_key) if error_key else None,
        },
        lang,
    )


@app.post("/labels")
def labels_print(
    request: Request,
    label_target: str = Form("spool"),
    spool_ids: list[int] = Form([]),
    storage_location_ids: list[int] = Form([]),
    printer_ids: list[int] = Form([]),
    layout: str = Form(DEFAULT_LABEL_LAYOUT),
    print_mode: str = Form(DEFAULT_LABEL_PRINT_MODE),
    label_orientation: str = Form(DEFAULT_LABEL_ORIENTATION),
    show_spool_id: Optional[str] = Form(None),
    show_brand: Optional[str] = Form(None),
    show_material_color: Optional[str] = Form(None),
    show_weight: Optional[str] = Form(None),
    show_remaining: Optional[str] = Form(None),
    show_location: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    selected_ids = [int(value) for value in spool_ids if value]
    layouts_map = _all_label_layouts()
    valid_layout = _normalize_label_layout(layout, layouts_map)
    valid_print_mode = _normalize_label_print_mode(print_mode)
    valid_label_orientation = _normalize_label_orientation(label_orientation)
    normalized_label_target = str(label_target or "").strip().lower()
    if normalized_label_target not in {"spool", "location", "printer"}:
        normalized_label_target = "spool"
    base_label_content = _load_label_print_preferences(request).get("label_content", _default_label_content_settings())
    label_content = _build_label_content_settings(base_label_content)
    if any(
        field is not None
        for field in (
            show_spool_id,
            show_brand,
            show_material_color,
            show_weight,
            show_remaining,
            show_location,
        )
    ):
        label_content = _build_label_content_settings(
            {
                "show_spool_id": _is_truthy(show_spool_id),
                "show_brand": _is_truthy(show_brand),
                "show_material_color": _is_truthy(show_material_color),
                "show_weight": _is_truthy(show_weight),
                "show_remaining": _is_truthy(show_remaining),
                "show_location": _is_truthy(show_location),
            }
        )

    selected_location_ids = [int(value) for value in storage_location_ids if value]
    selected_printer_ids = [int(value) for value in printer_ids if value]

    if normalized_label_target == "spool" and not selected_ids:
        spools = (
            db.query(Spool)
            .filter(Spool.project == project)
            .order_by(Spool.id.asc())
            .all()
        )
        response = render(
            request,
            "labels.html",
            {
                "spools": spools,
                "storage_locations": _storage_location_options(db, project),
                "printers": db.query(Printer).filter(Printer.project == project).order_by(Printer.name.asc(), Printer.id.asc()).all(),
                "label_target": normalized_label_target,
                "selected_ids": [],
                "selected_location_ids": selected_location_ids,
                "selected_printer_ids": selected_printer_ids,
                "layout": valid_layout,
                "print_mode": valid_print_mode,
                "label_orientation": valid_label_orientation,
                "label_content": label_content,
                "layout_choices": _get_label_layout_choices(lang, layouts_map),
                "custom_layouts": [item for item in _get_label_layout_choices(lang, layouts_map) if item.get("is_custom")],
                "error": t_factory(lang)("label_none_selected"),
                "message": None,
            },
            lang,
        )
        _set_cookie(response, LABEL_TARGET_SETTING_KEY, normalized_label_target, request=request)
        _set_cookie(response, "label_layout", valid_layout)
        _save_setting_to_db(LABEL_TARGET_SETTING_KEY, normalized_label_target)
        _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
        _save_setting_to_db("label_layout", valid_layout)
        return response

    if normalized_label_target == "location" and not selected_location_ids:
        spools = (
            db.query(Spool)
            .filter(Spool.project == project)
            .order_by(Spool.id.asc())
            .all()
        )
        response = render(
            request,
            "labels.html",
            {
                "spools": spools,
                "storage_locations": _storage_location_options(db, project),
                "printers": db.query(Printer).filter(Printer.project == project).order_by(Printer.name.asc(), Printer.id.asc()).all(),
                "label_target": normalized_label_target,
                "selected_ids": selected_ids,
                "selected_location_ids": [],
                "selected_printer_ids": selected_printer_ids,
                "layout": valid_layout,
                "print_mode": valid_print_mode,
                "label_orientation": valid_label_orientation,
                "label_content": label_content,
                "layout_choices": _get_label_layout_choices(lang, layouts_map),
                "custom_layouts": [item for item in _get_label_layout_choices(lang, layouts_map) if item.get("is_custom")],
                "error": t_factory(lang)("label_location_none_selected"),
                "message": None,
            },
            lang,
        )
        _set_cookie(response, LABEL_TARGET_SETTING_KEY, normalized_label_target, request=request)
        _set_cookie(response, "label_layout", valid_layout)
        _save_setting_to_db(LABEL_TARGET_SETTING_KEY, normalized_label_target)
        _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
        _save_setting_to_db("label_layout", valid_layout)
        return response

    if normalized_label_target == "printer" and not selected_printer_ids:
        spools = (
            db.query(Spool)
            .filter(Spool.project == project)
            .order_by(Spool.id.asc())
            .all()
        )
        response = render(
            request,
            "labels.html",
            {
                "spools": spools,
                "storage_locations": _storage_location_options(db, project),
                "printers": db.query(Printer).filter(Printer.project == project).order_by(Printer.name.asc(), Printer.id.asc()).all(),
                "label_target": normalized_label_target,
                "selected_ids": selected_ids,
                "selected_location_ids": selected_location_ids,
                "selected_printer_ids": [],
                "layout": valid_layout,
                "print_mode": valid_print_mode,
                "label_orientation": valid_label_orientation,
                "label_content": label_content,
                "layout_choices": _get_label_layout_choices(lang, layouts_map),
                "custom_layouts": [item for item in _get_label_layout_choices(lang, layouts_map) if item.get("is_custom")],
                "error": t_factory(lang)("label_printer_none_selected"),
                "message": None,
            },
            lang,
        )
        _set_cookie(response, LABEL_TARGET_SETTING_KEY, normalized_label_target, request=request)
        _set_cookie(response, "label_layout", valid_layout)
        _save_setting_to_db(LABEL_TARGET_SETTING_KEY, normalized_label_target)
        _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
        _save_setting_to_db("label_layout", valid_layout)
        return response

    def _build_preview_url() -> str:
        query_params: list[tuple[str, str]] = [
            ("preview", "1"),
            ("target", normalized_label_target),
            ("view", normalized_label_target),
            ("layout", valid_layout),
            ("print_mode", valid_print_mode),
            ("label_orientation", valid_label_orientation),
        ]
        for field in ("show_spool_id", "show_brand", "show_material_color", "show_weight", "show_remaining", "show_location"):
            if _is_truthy(label_content.get(field)):
                query_params.append((field, "1"))
        for value in selected_ids:
            query_params.append(("spool_ids", str(value)))
        for value in selected_location_ids:
            query_params.append(("storage_location_ids", str(value)))
        for value in selected_printer_ids:
            query_params.append(("printer_ids", str(value)))
        return f"/labels?{urlencode(query_params)}"

    label_items: list[dict] = []
    if normalized_label_target == "location":
        selected_locations = (
            db.query(StorageSubLocation)
            .filter(StorageSubLocation.project == project, StorageSubLocation.id.in_(selected_location_ids))
            .order_by(StorageSubLocation.path_code.asc())
            .all()
        )
        for location in selected_locations:
            label_items.append(
                {
                    "qr_src": f"/storage-locations/{location.id}/qr",
                    "line_title": location.path_code,
                    "line_brand": location.name or "",
                    "line_material_color": "",
                    "line_weight": "",
                    "line_remaining": "",
                    "line_location": location.path_code,
                }
            )
    elif normalized_label_target == "printer":
        selected_printers = (
            db.query(Printer)
            .filter(Printer.project == project, Printer.id.in_(selected_printer_ids))
            .order_by(Printer.name.asc(), Printer.id.asc())
            .all()
        )
        for printer in selected_printers:
            label_items.append(
                {
                    "qr_src": f"/printers/{printer.id}/qr",
                    "line_title": printer.name,
                    "line_brand": printer.serial,
                    "line_material_color": f"{printer.host or '-'}:{printer.port or '-'}",
                    "line_weight": "",
                    "line_remaining": "",
                    "line_location": "",
                }
            )
    else:
        selected_spools = (
            db.query(Spool)
            .filter(Spool.project == project, Spool.id.in_(selected_ids))
            .order_by(Spool.id.asc())
            .all()
        )
        storage_map = _storage_location_map_by_id(
            db,
            project,
            [int(spool.storage_sub_location_id) for spool in selected_spools if spool.storage_sub_location_id],
        )
        for spool in selected_spools:
            label_items.append(
                {
                    "qr_src": f"/spools/{spool.id}/qr",
                    "line_title": f"SP-{spool.id:04d}",
                    "line_brand": spool.brand,
                    "line_material_color": f"{spool.material} · {spool.color}",
                    "line_weight": format_weight_text(spool.weight_g),
                    "line_remaining": format_weight_text(spool.remaining_g),
                    "line_location": _spool_location_display(spool, storage_map),
                }
            )

    response = RedirectResponse(_build_preview_url(), status_code=303)
    _set_cookie(response, LABEL_TARGET_SETTING_KEY, normalized_label_target, request=request)
    _set_cookie(response, "label_layout", valid_layout)
    _save_setting_to_db(LABEL_TARGET_SETTING_KEY, normalized_label_target)
    _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
    _save_setting_to_db("label_layout", valid_layout)
    return response


@app.get("/usage")
@app.get("/booking")
def booking_form(
    request: Request,
    usage_notice: Optional[str] = None,
    usage_error: Optional[str] = None,
    manual_mode: Optional[str] = None,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)
    spool_scope_filters = _model_scope_filters(Spool, project)
    active_spools = (
        db.query(Spool)
        .filter(*spool_scope_filters, Spool.in_use.is_(True))
        .order_by(Spool.brand)
        .all()
    )
    notice_map = {
        "applied": "usage_applied",
    }
    error_map = {
        "no_file": "usage_no_file",
        "manual_needed": "usage_manual_needed",
    }

    message = t(notice_map[usage_notice]) if usage_notice in notice_map else None
    error = t(error_map[usage_error]) if usage_error in error_map else None

    return render(
        request,
        "booking.html",
        {
            "message": message,
            "error": error,
            "active_spools": active_spools,
            "usage_breakdown": [],
            "preview_mode": False,
            "manual_mode": _is_truthy(manual_mode),
            "auto_plan": [],
            "usage_total_g": None,
            "advanced_usage": {},
            "source_filename": None,
        },
        lang,
    )


@app.get("/booking/tracking")
def booking_tracking_page(
    request: Request,
    usage_notice: Optional[str] = None,
    usage_error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)
    history_scope_filters = _model_scope_filters(UsageHistory, project)
    batch_scope_filters = _model_scope_filters(UsageBatchContext, project)

    usage_history_rows = (
        db.query(UsageHistory)
        .filter(*history_scope_filters, UsageHistory.undone.is_(False))
        .order_by(UsageHistory.created_at.desc(), UsageHistory.id.desc())
        .limit(30)
        .all()
    )
    usage_history = _group_usage_history_rows(usage_history_rows)

    batch_ids = [
        str(entry.get("batch_key"))
        for entry in usage_history
        if str(entry.get("batch_key", "")).strip() and not str(entry.get("batch_key", "")).startswith("single:")
    ]
    if batch_ids:
        contexts = (
            db.query(UsageBatchContext)
            .filter(*batch_scope_filters, UsageBatchContext.batch_id.in_(batch_ids))
            .all()
        )
        context_map = {context.batch_id: context for context in contexts}
        for entry in usage_history:
            batch_key = str(entry.get("batch_key") or "")
            context = context_map.get(batch_key)
            if context is None:
                continue
            entry["printer_name"] = context.printer_name
            entry["ams_slots"] = _parse_slot_tokens(context.ams_slots)

    notice_map = {
        "undo_done": "usage_undo_done",
        "undo_none": "usage_undo_none",
    }
    error_map = {
        "no_file": "usage_no_file",
        "manual_needed": "usage_manual_needed",
    }

    message = t(notice_map[usage_notice]) if usage_notice in notice_map else None
    error = t(error_map[usage_error]) if usage_error in error_map else None

    return render(
        request,
        "booking_tracking.html",
        {
            "message": message,
            "error": error,
            "usage_history": usage_history,
        },
        lang,
    )


@app.post("/usage")
@app.post("/booking")
@app.post("/booking/tracking")
def apply_usage(
    request: Request,
    file: Optional[UploadFile] = File(None),
    spool_ids: list[int] = Form([]),
    deductions: list[str] = Form([]),
    source_filename: Optional[str] = Form(None),
    action: str = Form("preview_auto"),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    spool_scope_filters = _model_scope_filters(Spool, project)
    history_scope_filters = _model_scope_filters(UsageHistory, project)
    active_spools = (
        db.query(Spool)
        .filter(*spool_scope_filters, Spool.in_use.is_(True), Spool.remaining_g > 0)
        .order_by(Spool.brand)
        .all()
    )
    available_spools = (
        db.query(Spool)
        .filter(*spool_scope_filters, Spool.remaining_g > 0)
        .order_by(Spool.in_use.desc(), Spool.remaining_g.desc(), Spool.id.asc())
        .all()
    )

    def redirect_booking(notice: Optional[str] = None, error: Optional[str] = None, manual: bool = False):
        query: dict[str, str] = {"lang": lang, "project": project}
        if notice:
            query["usage_notice"] = notice
        if error:
            query["usage_error"] = error
        if manual:
            query["manual_mode"] = "1"
        return RedirectResponse(f"/booking?{urlencode(query, doseq=True)}", status_code=303)

    def redirect_tracking(notice: Optional[str] = None, error: Optional[str] = None):
        query: dict[str, str] = {"lang": lang, "project": project}
        if notice:
            query["usage_notice"] = notice
        if error:
            query["usage_error"] = error
        return RedirectResponse(f"/booking/tracking?{urlencode(query, doseq=True)}", status_code=303)

    def base_context():
        return {
            "active_spools": active_spools,
            "usage_breakdown": [],
            "preview_mode": False,
            "manual_mode": False,
            "auto_plan": [],
            "usage_total_g": None,
            "advanced_usage": {},
            "source_filename": source_filename,
        }

    def detect_spools(filament_hints: dict, breakdown: list[dict]):
        selected_local: list[Spool] = []
        if not available_spools:
            return selected_local

        prioritized_pool = list(active_spools)
        active_ids = {spool.id for spool in active_spools}
        prioritized_pool.extend([spool for spool in available_spools if spool.id not in active_ids])

        if any(item.get("slot") is not None for item in (breakdown or [])):
            return prioritized_pool

        if any(bool(item.get("is_support")) for item in (breakdown or [])):
            return prioritized_pool

        material_hints = [m for m in filament_hints.get("materials", []) if "unknown" not in m.lower()]
        color_hints = [c for c in filament_hints.get("colors", []) if "unknown" not in c.lower()]
        brand_hints = [b for b in filament_hints.get("brands", []) if "unknown" not in b.lower()]

        if prioritized_pool and (material_hints or color_hints or brand_hints):
            matched = [
                spool
                for spool in prioritized_pool
                if (
                    (
                        not material_hints
                        or _matches_any(spool.material, material_hints)
                        or (breakdown and _matches_any(spool.material, [x.get("material") for x in breakdown if x.get("material")]))
                    )
                    and (not color_hints or _matches_any(spool.color, color_hints))
                    and (not brand_hints or _matches_any(spool.brand, brand_hints))
                )
            ]
            selected_local = matched or prioritized_pool
        else:
            selected_local = prioritized_pool

        return selected_local

    def build_auto_plan(selected_spools: list[Spool], total_grams: float, breakdown: list[dict]):
        if not selected_spools or total_grams is None:
            return []

        def is_support_spool(spool: Spool) -> bool:
            material = (spool.material or "").lower()
            return (
                "support" in material
                or "stütz" in material
                or "stutz" in material
                or material in {"pva", "bvoh", "hips"}
            )

        allocations: dict[int, float] = {s.id: 0.0 for s in selected_spools}

        def allocate_by_capacity(targets: list[Spool], grams_needed: float) -> bool:
            if grams_needed <= 0:
                return True
            ordered = sorted(
                targets,
                key=lambda spool: (bool(spool.in_use), (spool.remaining_g or 0.0), -(spool.id or 0)),
                reverse=True,
            )
            remaining = float(grams_needed)
            for spool in ordered:
                current = allocations.get(spool.id, 0.0)
                capacity = max(0.0, float(spool.remaining_g or 0.0) - current)
                if capacity <= 0:
                    continue
                take = min(capacity, remaining)
                allocations[spool.id] = current + take
                remaining -= take
                if remaining <= 1e-6:
                    return True
            return False

        breakdown_with_values = [
            item for item in (breakdown or [])
            if item.get("grams") is not None and item.get("material")
        ]

        if breakdown_with_values:
            used = 0.0
            for item in breakdown_with_values:
                material = str(item.get("parsed_material") or item.get("material", "")).strip()
                support_required = bool(item.get("is_support")) or "support" in material.lower()
                grams = float(item.get("grams") or 0)
                slot_required = _normalize_ams_slot(str(item.get("slot")) if item.get("slot") is not None else None)
                if grams <= 0:
                    continue

                candidate_pool = selected_spools
                support_spools = [s for s in selected_spools if is_support_spool(s)]
                model_spools = [s for s in selected_spools if not is_support_spool(s)]

                if support_required and support_spools:
                    candidate_pool = support_spools
                elif (not support_required) and model_spools:
                    candidate_pool = model_spools

                if support_required and not support_spools:
                    return []

                slot_targets = []
                if slot_required is not None:
                    slot_targets = _slot_scoped_spools(selected_spools, slot_required, None)

                if slot_targets:
                    targets = slot_targets
                else:
                    matches = [s for s in candidate_pool if _matches_any(s.material, [material])]
                    targets = matches if matches else candidate_pool
                if not targets:
                    return []

                if not allocate_by_capacity(targets, grams):
                    return []
                used += grams

            remaining = max(0.0, float(total_grams) - used)
            if remaining > 0 and selected_spools:
                if not allocate_by_capacity(selected_spools, remaining):
                    return []
        else:
            if not allocate_by_capacity(selected_spools, float(total_grams)):
                return []

        plan = []
        for spool in selected_spools:
            grams = round(allocations.get(spool.id, 0.0), 3)
            if grams > 0:
                plan.append({"spool": spool, "grams": grams})
        return plan

    def apply_plan(ids: list[int], grams_values: list[str], mode: str):
        changed = 0
        actor = None
        if request.client and request.client.host:
            actor = request.client.host
        history_rows: list[UsageHistory] = []
        batch_id = uuid4().hex

        for idx, spool_id in enumerate(ids):
            grams = _parse_optional_float(grams_values[idx] if idx < len(grams_values) else None)
            if not grams or grams <= 0:
                continue
            spool = db.query(Spool).filter(*spool_scope_filters, Spool.id == spool_id).first()
            if not spool:
                continue
            before = float(spool.remaining_g or 0)
            after = max(0, round(before - grams, 3))
            spool.remaining_g = after
            _enforce_empty_lifecycle(spool)
            spool.updated_at = _utcnow()
            changed += 1

            history_rows.append(
                UsageHistory(
                    actor=actor,
                    mode=mode,
                    batch_id=batch_id,
                    source_file=source_filename,
                    project=project,
                    spool_id=spool.id,
                    spool_brand=spool.brand,
                    spool_material=spool.material,
                    spool_color=spool.color,
                    deducted_g=round(float(grams), 3),
                    remaining_before_g=round(before, 3),
                    remaining_after_g=round(after, 3),
                    undone=False,
                )
            )

        if changed:
            db.add_all(history_rows)
            _audit_log(
                db,
                project,
                "usage_apply",
                request=request,
                entity_type="usage",
                entity_id=batch_id,
                details={
                    "mode": mode,
                    "changed_spools": int(changed),
                    "source_file": source_filename,
                },
            )
            db.commit()
        return changed

    def undo_last_deduction() -> bool:
        last_entry = (
            db.query(UsageHistory)
            .filter(*history_scope_filters)
            .filter(UsageHistory.undone.is_(False))
            .order_by(UsageHistory.created_at.desc(), UsageHistory.id.desc())
            .first()
        )
        if not last_entry:
            return False

        if last_entry.batch_id:
            rows = (
                db.query(UsageHistory)
                .filter(
                    *history_scope_filters,
                    UsageHistory.batch_id == last_entry.batch_id,
                    UsageHistory.undone.is_(False),
                )
                .all()
            )
        else:
            rows = [last_entry]

        if not rows:
            return False

        now = _utcnow()
        reverted_count = 0
        for row in rows:
            spool = (
                db.query(Spool).filter(*spool_scope_filters, Spool.id == row.spool_id).first()
                if row.spool_id
                else None
            )
            if spool:
                restored_value = float(spool.remaining_g or 0) + float(row.deducted_g or 0)
                capacity = float(spool.weight_g or 0)
                if capacity > 0:
                    restored_value = min(restored_value, capacity)
                spool.remaining_g = round(max(0.0, restored_value), 3)
                spool.updated_at = now
            row.undone = True
            row.undone_at = now
            reverted_count += 1

        _audit_log(
            db,
            project,
            "usage_undo",
            request=request,
            entity_type="usage",
            entity_id=(last_entry.batch_id or last_entry.id),
            details={"rows_reverted": int(reverted_count)},
        )
        db.commit()
        return True

    if action == "manual_mode":
        return redirect_booking(manual=True)

    if action == "undo_last":
        if undo_last_deduction():
            return redirect_tracking(notice="undo_done")
        else:
            return redirect_tracking(error="undo_none")

    if action in ("save_manual", "save_auto"):
        changed = apply_plan(spool_ids, deductions, action)
        if changed:
            return redirect_booking(notice="applied")
        else:
            return redirect_booking(error="manual_needed", manual=True)

    if file is None or not file.filename:
        return redirect_booking(error="no_file")

    file_bytes, too_large = _read_upload_limited(file)
    if too_large:
        context = base_context()
        context.update({"error": t_factory(lang)("upload_too_large").format(max_mb=MAX_UPLOAD_MB)})
        return render(request, "booking.html", context, lang)
    if file_bytes is None:
        return redirect_booking(error="no_file")

    grams, millimeters, metadata, filament_hints, usage_breakdown = parse_3mf_filament_usage(file_bytes)
    if not usage_breakdown and filament_hints.get("materials"):
        usage_breakdown = [
            {"material": material, "grams": None}
            for material in filament_hints.get("materials", [])
            if material and "unknown" not in material.lower()
        ]

    advanced_usage = {}
    if millimeters is not None:
        advanced_usage["total_length_m"] = round(float(millimeters) / 1000.0, 2)
    switches = _parse_optional_float(metadata.get("__bambu_filament_switches__"))
    if switches is not None:
        advanced_usage["filament_switches"] = int(switches)
    est_cost = _parse_optional_float(metadata.get("__bambu_total_cost__"))
    if est_cost is not None:
        advanced_usage["estimated_cost"] = round(est_cost, 2)

    if grams is None:
        no_grams_key = "usage_no_grams_bambu_unsliced" if metadata.get("__bambu_unsliced__") == "1" else "usage_no_grams"
        context = base_context()
        context.update(
            {
                "error": t_factory(lang)(no_grams_key),
                "usage_breakdown": usage_breakdown,
                "manual_mode": True,
                "advanced_usage": advanced_usage,
            }
        )
        return render(request, "booking.html", context, lang)

    selected = detect_spools(filament_hints, usage_breakdown)
    if not selected:
        context = base_context()
        context.update(
            {
                "error": t_factory(lang)("usage_no_match"),
                "usage_breakdown": usage_breakdown,
                "usage_total_g": round(grams, 3),
                "manual_mode": True,
                "advanced_usage": advanced_usage,
            }
        )
        return render(request, "booking.html", context, lang)

    auto_plan = build_auto_plan(selected, float(grams), usage_breakdown)
    if not auto_plan:
        context = base_context()
        context.update(
            {
                "error": t_factory(lang)("usage_no_match"),
                "usage_breakdown": usage_breakdown,
                "usage_total_g": round(float(grams), 3),
                "manual_mode": True,
                "advanced_usage": advanced_usage,
            }
        )
        return render(request, "booking.html", context, lang)

    context = base_context()
    context.update(
        {
            "message": t_factory(lang)("usage_preview_ready"),
            "preview_mode": True,
            "usage_breakdown": usage_breakdown,
            "usage_total_g": round(float(grams), 3),
            "auto_plan": auto_plan,
            "advanced_usage": advanced_usage,
            "source_filename": file.filename,
        }
    )
    return render(request, "booking.html", context, lang)


@app.post("/api/usage/auto-from-file")
@app.post("/api/usage/auto-from-3mf")
def api_auto_usage_from_3mf(
    request: Request,
    file: UploadFile = File(...),
    project: Optional[str] = Form(None),
    job_id: Optional[str] = Form(None),
    slicer: Optional[str] = Form(None),
    printer: Optional[str] = Form(None),
    ams_slots: Optional[str] = Form(None),
    dry_run: Optional[str] = Form("0"),
    db: Session = Depends(get_db),
):
    effective_project = _effective_project_for_request(request, project)
    actor = request.client.host if request.client and request.client.host else None
    slicer_name = str(slicer or "").strip()[:120] or None
    printer_name = _normalize_printer_name(printer)
    should_dry_run = _is_truthy(dry_run)

    if file is None or not file.filename:
        return {"ok": False, "error": "missing_file"}

    file_bytes, too_large = _read_upload_limited(file)
    if too_large:
        return {"ok": False, "error": "file_too_large", "max_mb": MAX_UPLOAD_MB}
    if file_bytes is None:
        return {"ok": False, "error": "missing_file"}

    grams, millimeters, metadata, filament_hints, usage_breakdown, parse_error = _parse_usage_from_print_file(
        file.filename,
        file_bytes,
    )
    if parse_error == "unsupported_file":
        return {"ok": False, "error": "unsupported_file"}
    if not usage_breakdown and filament_hints.get("materials"):
        usage_breakdown = [
            {"material": material, "grams": None}
            for material in filament_hints.get("materials", [])
            if material and "unknown" not in material.lower()
        ]

    advanced_usage = {}
    if millimeters is not None:
        advanced_usage["total_length_m"] = round(float(millimeters) / 1000.0, 2)
    switches = _parse_optional_float(metadata.get("__bambu_filament_switches__"))
    if switches is not None:
        advanced_usage["filament_switches"] = int(switches)
    est_cost = _parse_optional_float(metadata.get("__bambu_total_cost__"))
    if est_cost is not None:
        advanced_usage["estimated_cost"] = round(est_cost, 2)

    if grams is None:
        error_code = "no_grams_bambu_unsliced" if metadata.get("__bambu_unsliced__") == "1" else "no_grams"
        return {
            "ok": False,
            "error": error_code,
            "usage_breakdown": usage_breakdown,
            "advanced_usage": advanced_usage,
        }

    resolved_ams_slots = _resolve_ams_slots(ams_slots, usage_breakdown)
    serialized_ams_slots = _serialize_ams_slots(resolved_ams_slots)

    spool_scope_filters = _model_scope_filters(Spool, effective_project)
    usage_scope_filters = _model_scope_filters(UsageHistory, effective_project)
    batch_scope_filters = _model_scope_filters(UsageBatchContext, effective_project)

    active_spools = (
        db.query(Spool)
        .filter(*spool_scope_filters, Spool.in_use.is_(True), Spool.remaining_g > 0)
        .order_by(Spool.brand)
        .all()
    )
    available_spools = (
        db.query(Spool)
        .filter(*spool_scope_filters, Spool.remaining_g > 0)
        .order_by(Spool.in_use.desc(), Spool.remaining_g.desc(), Spool.id.asc())
        .all()
    )

    def detect_spools_local(hints: dict, breakdown: list[dict]) -> list[Spool]:
        if not available_spools:
            return []

        prioritized_pool = list(active_spools)
        active_ids = {spool.id for spool in active_spools}
        prioritized_pool.extend([spool for spool in available_spools if spool.id not in active_ids])

        if any(item.get("slot") is not None for item in (breakdown or [])):
            return prioritized_pool

        if any(bool(item.get("is_support")) for item in (breakdown or [])):
            return prioritized_pool

        material_hints = [m for m in hints.get("materials", []) if "unknown" not in m.lower()]
        color_hints = [c for c in hints.get("colors", []) if "unknown" not in c.lower()]
        brand_hints = [b for b in hints.get("brands", []) if "unknown" not in b.lower()]

        if prioritized_pool and (material_hints or color_hints or brand_hints):
            matched = [
                spool
                for spool in prioritized_pool
                if (
                    (
                        not material_hints
                        or _matches_any(spool.material, material_hints)
                        or (
                            breakdown
                            and _matches_any(
                                spool.material,
                                [x.get("material") for x in breakdown if x.get("material")],
                            )
                        )
                    )
                    and (not color_hints or _matches_any(spool.color, color_hints))
                    and (not brand_hints or _matches_any(spool.brand, brand_hints))
                )
            ]
            return matched or prioritized_pool

        return prioritized_pool

    def build_auto_plan_local(selected_spools: list[Spool], total_grams: float, breakdown: list[dict]) -> list[dict]:
        if not selected_spools or total_grams is None:
            return []

        def is_support_spool(spool: Spool) -> bool:
            material = (spool.material or "").lower()
            return (
                "support" in material
                or "stütz" in material
                or "stutz" in material
                or material in {"pva", "bvoh", "hips"}
            )

        allocations: dict[int, float] = {s.id: 0.0 for s in selected_spools}

        def allocate_by_capacity(targets: list[Spool], grams_needed: float) -> bool:
            if grams_needed <= 0:
                return True
            ordered = sorted(
                targets,
                key=lambda spool: (bool(spool.in_use), (spool.remaining_g or 0.0), -(spool.id or 0)),
                reverse=True,
            )
            remaining = float(grams_needed)
            for spool in ordered:
                current = allocations.get(spool.id, 0.0)
                capacity = max(0.0, float(spool.remaining_g or 0.0) - current)
                if capacity <= 0:
                    continue
                take = min(capacity, remaining)
                allocations[spool.id] = current + take
                remaining -= take
                if remaining <= 1e-6:
                    return True
            return False

        breakdown_with_values = [
            item
            for item in (breakdown or [])
            if item.get("grams") is not None and item.get("material")
        ]

        if breakdown_with_values:
            used = 0.0
            for item in breakdown_with_values:
                material = str(item.get("parsed_material") or item.get("material", "")).strip()
                support_required = bool(item.get("is_support")) or "support" in material.lower()
                grams_for_item = float(item.get("grams") or 0)
                slot_required = _normalize_ams_slot(str(item.get("slot")) if item.get("slot") is not None else None)
                if grams_for_item <= 0:
                    continue

                candidate_pool = selected_spools
                support_spools = [s for s in selected_spools if is_support_spool(s)]
                model_spools = [s for s in selected_spools if not is_support_spool(s)]

                if support_required and support_spools:
                    candidate_pool = support_spools
                elif (not support_required) and model_spools:
                    candidate_pool = model_spools

                if support_required and not support_spools:
                    return []

                slot_targets = []
                if slot_required is not None:
                    slot_targets = _slot_scoped_spools(selected_spools, slot_required, printer_name)

                if slot_targets:
                    targets = slot_targets
                else:
                    matches = [s for s in candidate_pool if _matches_any(s.material, [material])]
                    targets = matches if matches else candidate_pool
                if not targets:
                    return []

                if not allocate_by_capacity(targets, grams_for_item):
                    return []
                used += grams_for_item

            remaining = max(0.0, float(total_grams) - used)
            if remaining > 0 and selected_spools:
                if not allocate_by_capacity(selected_spools, remaining):
                    return []
        else:
            if not allocate_by_capacity(selected_spools, float(total_grams)):
                return []

        plan = []
        for spool in selected_spools:
            grams_for_spool = round(allocations.get(spool.id, 0.0), 3)
            if grams_for_spool > 0:
                plan.append({"spool": spool, "grams": grams_for_spool})
        return plan

    selected = detect_spools_local(filament_hints, usage_breakdown)
    if not selected:
        return {
            "ok": False,
            "error": "no_match",
            "usage_breakdown": usage_breakdown,
            "usage_total_g": round(float(grams), 3),
            "advanced_usage": advanced_usage,
        }

    auto_plan = build_auto_plan_local(selected, float(grams), usage_breakdown)
    if not auto_plan:
        return {
            "ok": False,
            "error": "no_match",
            "usage_breakdown": usage_breakdown,
            "usage_total_g": round(float(grams), 3),
            "advanced_usage": advanced_usage,
        }

    normalized_job_id = str(job_id or "").strip()[:64] or None
    if normalized_job_id:
        existing_count = (
            db.query(func.count(UsageHistory.id))
            .filter(
                *usage_scope_filters,
                UsageHistory.mode.in_(["bambu_auto", "auto_file"]),
                UsageHistory.batch_id == normalized_job_id,
                UsageHistory.undone.is_(False),
            )
            .scalar()
            or 0
        )
        if existing_count > 0:
            existing_context = (
                db.query(UsageBatchContext)
                .filter(
                    *batch_scope_filters,
                    UsageBatchContext.batch_id == normalized_job_id,
                )
                .first()
            )
            return {
                "ok": True,
                "already_applied": True,
                "project": effective_project,
                "job_id": normalized_job_id,
                "deducted_g": round(float(grams), 3),
                "changed_spools": int(existing_count),
                "printer": existing_context.printer_name if existing_context else printer_name,
                "ams_slots": _parse_slot_tokens(existing_context.ams_slots) if existing_context else resolved_ams_slots,
                "usage_breakdown": usage_breakdown,
                "advanced_usage": advanced_usage,
            }

    plan_rows = [
        {
            "spool_id": item["spool"].id,
            "brand": item["spool"].brand,
            "material": item["spool"].material,
            "color": item["spool"].color,
            "deducted_g": round(float(item["grams"]), 3),
            "remaining_before_g": round(float(item["spool"].remaining_g or 0), 3),
            "remaining_after_g": round(max(0.0, float(item["spool"].remaining_g or 0) - float(item["grams"])), 3),
        }
        for item in auto_plan
    ]

    if should_dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "project": effective_project,
            "job_id": normalized_job_id,
            "deducted_g": round(float(grams), 3),
            "changed_spools": len(plan_rows),
            "printer": printer_name,
            "ams_slots": resolved_ams_slots,
            "rows": plan_rows,
            "usage_breakdown": usage_breakdown,
            "advanced_usage": advanced_usage,
        }

    batch_id = normalized_job_id or uuid4().hex
    changed = 0
    history_rows: list[UsageHistory] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for item in auto_plan:
        spool: Spool = item["spool"]
        deducted_g = round(float(item["grams"]), 3)
        if deducted_g <= 0:
            continue
        before = round(float(spool.remaining_g or 0), 3)
        after = round(max(0.0, before - deducted_g), 3)
        spool.remaining_g = after
        _enforce_empty_lifecycle(spool)
        spool.updated_at = now
        changed += 1

        history_rows.append(
            UsageHistory(
                actor=actor,
                mode="auto_file",
                source_app=slicer_name,
                batch_id=batch_id,
                source_file=file.filename,
                project=effective_project,
                spool_id=spool.id,
                spool_brand=spool.brand,
                spool_material=spool.material,
                spool_color=spool.color,
                deducted_g=deducted_g,
                remaining_before_g=before,
                remaining_after_g=after,
                undone=False,
            )
        )

    if changed:
        if printer_name or serialized_ams_slots:
            existing_context = (
                db.query(UsageBatchContext)
                .filter(
                    *batch_scope_filters,
                    UsageBatchContext.batch_id == batch_id,
                )
                .first()
            )
            if existing_context is None:
                db.add(
                    UsageBatchContext(
                        project=effective_project,
                        batch_id=batch_id,
                        printer_name=printer_name,
                        printer_serial=None,
                        ams_slots=serialized_ams_slots,
                    )
                )
            else:
                if printer_name and not existing_context.printer_name:
                    existing_context.printer_name = printer_name
                if serialized_ams_slots and not existing_context.ams_slots:
                    existing_context.ams_slots = serialized_ams_slots
        db.add_all(history_rows)
        _audit_log(
            db,
            effective_project,
            "api_usage_auto_apply",
            request=request,
            actor=actor,
            entity_type="usage",
            entity_id=batch_id,
            details={
                "source_app": slicer_name,
                "printer": printer_name,
                "ams_slots": resolved_ams_slots,
                "changed_spools": int(changed),
                "source_file": file.filename,
            },
        )
        db.commit()

    return {
        "ok": changed > 0,
        "project": effective_project,
        "job_id": batch_id,
        "deducted_g": round(float(grams), 3),
        "changed_spools": changed,
        "printer": printer_name,
        "ams_slots": resolved_ams_slots,
        "rows": [
            {
                "spool_id": row.spool_id,
                "brand": row.spool_brand,
                "material": row.spool_material,
                "color": row.spool_color,
                "deducted_g": round(float(row.deducted_g or 0), 3),
                "remaining_before_g": round(float(row.remaining_before_g or 0), 3),
                "remaining_after_g": round(float(row.remaining_after_g or 0), 3),
            }
            for row in history_rows
        ],
        "usage_breakdown": usage_breakdown,
        "advanced_usage": advanced_usage,
    }


@app.post("/api/slot-state/push")
async def api_slot_state_push(
    request: Request,
    project: Optional[str] = None,
    source: Optional[str] = None,
    db: Session = Depends(get_db),
):
    try:
        payload: object = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid_json"}

    body_project = payload.get("project") if isinstance(payload, dict) else None
    if project is not None or body_project is not None:
        effective_project = _effective_project_for_request(request, project if project is not None else body_project)
    else:
        effective_project = _effective_project_for_request(request)

    body_source = payload.get("source") if isinstance(payload, dict) else None
    source_value = str(source if source is not None else body_source or "local-slot-bridge").strip()[:120] or "local-slot-bridge"

    entries = _extract_slot_state_entries(payload)
    updated = _upsert_slot_state_entries(db=db, project=effective_project, source=source_value, entries=entries)
    _audit_log(
        db,
        effective_project,
        "api_slot_state_push",
        request=request,
        entity_type="slot_state",
        details={
            "source": source_value,
            "entries": int(len(entries)),
            "updated": int(updated),
        },
    )
    db.commit()

    return {
        "ok": True,
        "project": effective_project,
        "source": source_value,
        "entries": len(entries),
        "updated": updated,
    }


@app.get("/import-export")
def import_form(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    profiles = (
        db.query(ImportMappingProfile)
        .filter(ImportMappingProfile.project == project)
        .order_by(ImportMappingProfile.name.asc(), ImportMappingProfile.id.asc())
        .all()
    )
    return render(
        request,
        "import.html",
        {
            "mapping_profiles": profiles,
        },
        lang,
    )


@app.get("/import")
def import_form_legacy(request: Request):
    query = str(request.url.query or "").strip()
    target = "/import-export"
    if query:
        target = f"{target}?{query}"
    return RedirectResponse(target, status_code=307)


@app.get("/backup")
def backup_page(request: Request):
    lang = get_lang(request)
    active_tab = str(request.query_params.get("tab") or "manual").strip().lower()
    if active_tab not in {"manual", "files", "auto"}:
        active_tab = "manual"
    context = _build_backup_context(lang, backup_active_tab=active_tab)
    return render(request, "backup.html", context, lang)


@app.post("/backup/create")
def backup_create(request: Request):
    lang = get_lang(request)
    t = t_factory(lang)
    mode = _backup_mode()
    project = get_project(request)

    if mode not in {"sqlite", "postgresql"}:
        return render(request, "backup.html", _build_backup_context(lang, warning=t("backup_unsupported")), lang)

    created_path, error_key = _create_backup_snapshot(mode, source="manual")
    if created_path is None:
        message_key = error_key if error_key in {"backup_storage_unavailable", "backup_pg_tools_missing"} else "backup_create_failed"
        return render(
            request,
            "backup.html",
            _build_backup_context(lang, error=t(message_key), backup_active_tab="manual"),
            lang,
        )

    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "backup_create",
            request=request,
            entity_type="backup",
            details={"mode": mode, "filename": created_path.name},
        )
        db_local.commit()
    finally:
        db_local.close()

    return render(
        request,
        "backup.html",
        _build_backup_context(lang, message=t("backup_create_done"), backup_active_tab="files"),
        lang,
    )


@app.get("/backup/download/{filename:path}")
def backup_download(request: Request, filename: str):
    lang = get_lang(request)
    mode = _backup_mode()
    file_path = _resolve_backup_file_path(mode, filename)
    if file_path is None or not file_path.exists():
        return render(request, "backup.html", _build_backup_context(lang, error=t_factory(lang)("backup_file_not_found"), backup_active_tab="files"), lang)
    return FileResponse(file_path, media_type="application/octet-stream", filename=file_path.name)


@app.post("/backup/restore-file")
def backup_restore_file(request: Request, filename: str = Form(...)):
    lang = get_lang(request)
    t = t_factory(lang)
    mode = _backup_mode()
    project = get_project(request)

    if mode not in {"sqlite", "postgresql"}:
        return render(request, "backup.html", _build_backup_context(lang, warning=t("backup_unsupported"), backup_active_tab="files"), lang)

    file_path = _resolve_backup_file_path(mode, filename)
    if file_path is None or not file_path.exists():
        return render(request, "backup.html", _build_backup_context(lang, error=t("backup_file_not_found"), backup_active_tab="files"), lang)

    restored = False
    try:
        restored = _restore_from_backup_path(mode, file_path)
    except Exception:
        restored = False

    if not restored:
        return render(request, "backup.html", _build_backup_context(lang, error=t("backup_file_restore_failed"), backup_active_tab="files"), lang)

    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "backup_restore_file",
            request=request,
            entity_type="backup",
            details={"mode": mode, "filename": file_path.name},
        )
        db_local.commit()
    finally:
        db_local.close()

    return render(
        request,
        "backup.html",
        _build_backup_context(lang, message=t("backup_file_restore_done"), backup_active_tab="files"),
        lang,
    )


@app.post("/backup/delete-file")
def backup_delete_file(request: Request, filename: str = Form(...)):
    lang = get_lang(request)
    t = t_factory(lang)
    mode = _backup_mode()
    project = get_project(request)

    file_path = _resolve_backup_file_path(mode, filename)
    if file_path is None or not file_path.exists():
        return render(request, "backup.html", _build_backup_context(lang, error=t("backup_file_not_found"), backup_active_tab="files"), lang)

    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        return render(request, "backup.html", _build_backup_context(lang, error=t("backup_file_delete_failed"), backup_active_tab="files"), lang)

    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "backup_delete_file",
            request=request,
            entity_type="backup",
            details={"mode": mode, "filename": file_path.name},
        )
        db_local.commit()
    finally:
        db_local.close()

    return render(
        request,
        "backup.html",
        _build_backup_context(lang, message=t("backup_file_delete_done"), backup_active_tab="files"),
        lang,
    )


@app.post("/backup/auto-settings")
def backup_auto_settings(
    request: Request,
    enabled: Optional[str] = Form(None),
    interval_hours: Optional[str] = Form(None),
    retention_days: Optional[str] = Form(None),
):
    lang = get_lang(request)
    t = t_factory(lang)
    project = get_project(request)

    normalized_enabled = _is_truthy(enabled)
    normalized_interval_hours = _clamp_int(interval_hours, BACKUP_MIN_INTERVAL_HOURS, BACKUP_MAX_INTERVAL_HOURS, 24)
    normalized_retention_days = _clamp_int(retention_days, BACKUP_MIN_RETENTION_DAYS, BACKUP_MAX_RETENTION_DAYS, 14)

    _save_backup_auto_settings(normalized_enabled, normalized_interval_hours, normalized_retention_days)

    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "backup_auto_settings",
            request=request,
            entity_type="backup",
            details={
                "enabled": normalized_enabled,
                "interval_hours": normalized_interval_hours,
                "retention_days": normalized_retention_days,
            },
        )
        db_local.commit()
    finally:
        db_local.close()

    if normalized_enabled:
        try:
            _run_auto_backup_if_due()
        except Exception:
            pass

    return render(
        request,
        "backup.html",
        _build_backup_context(lang, message=t("backup_auto_settings_saved"), backup_active_tab="auto"),
        lang,
    )


@app.post("/backup/reset-all")
def backup_reset_all(
    request: Request,
    reset_confirm_ack: Optional[str] = Form(None),
    reset_confirm_phrase: Optional[str] = Form(None),
    reset_create_backup: Optional[str] = Form(None),
):
    lang = get_lang(request)
    t = t_factory(lang)

    expected_phrase = BACKUP_RESET_CONFIRM_PHRASE
    has_ack = _is_truthy(reset_confirm_ack)
    entered_phrase = str(reset_confirm_phrase or "").strip()
    if not has_ack or entered_phrase != expected_phrase:
        return render(
            request,
            "backup.html",
            _build_backup_context(
                lang,
                error=t("backup_reset_confirm_required"),
                backup_active_tab="manual",
            ),
            lang,
        )

    created_backup_filename: Optional[str] = None
    if _is_truthy(reset_create_backup):
        mode = _backup_mode()
        if mode not in {"sqlite", "postgresql"}:
            return render(
                request,
                "backup.html",
                _build_backup_context(
                    lang,
                    error=t("backup_reset_backup_failed"),
                    backup_active_tab="manual",
                ),
                lang,
            )
        created_path, _error_key = _create_backup_snapshot(mode, source="manual")
        if created_path is None:
            return render(
                request,
                "backup.html",
                _build_backup_context(
                    lang,
                    error=t("backup_reset_backup_failed"),
                    backup_active_tab="manual",
                ),
                lang,
            )
        created_backup_filename = created_path.name

    try:
        deleted_rows = _delete_all_database_rows()
    except Exception:
        return render(
            request,
            "backup.html",
            _build_backup_context(
                lang,
                error=t("backup_reset_failed"),
                backup_active_tab="manual",
            ),
            lang,
        )

    if created_backup_filename:
        done_message = t("backup_reset_done_with_backup").format(rows=deleted_rows, filename=created_backup_filename)
    else:
        done_message = t("backup_reset_done").format(rows=deleted_rows)

    return render(
        request,
        "backup.html",
        _build_backup_context(
            lang,
            message=done_message,
            backup_active_tab="manual",
        ),
        lang,
    )


@app.get("/backup/export")
def backup_export(request: Request):
    lang = get_lang(request)
    t = t_factory(lang)

    mode = _backup_mode()
    project = get_project(request)
    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "backup_export",
            request=request,
            entity_type="backup",
            details={"mode": mode},
        )
        db_local.commit()
    finally:
        db_local.close()
    if mode == "sqlite":
        db_path = _sqlite_db_path()
        if not db_path or not db_path.exists():
            return RedirectResponse("/backup", status_code=303)
        filename = f"filament_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        return FileResponse(db_path, media_type="application/octet-stream", filename=filename)

    if mode == "postgresql":
        if not _pg_tools_available():
            return render(
                request,
                "backup.html",
                _build_backup_context(lang, warning=t("backup_pg_tools_missing")),
                lang,
            )

        tmp = tempfile.NamedTemporaryFile(prefix="filament_backup_", suffix=".dump", delete=False)
        tmp.close()
        dump_path = Path(tmp.name)

        cmd = ["pg_dump", "-Fc", "--no-owner", "--no-privileges", *_postgres_connection_args(), "-f", str(dump_path)]
        result = subprocess.run(cmd, env=_postgres_subprocess_env(), capture_output=True, text=True)
        if result.returncode != 0:
            _cleanup_temp_file(dump_path)
            return render(
                request,
                "backup.html",
                _build_backup_context(lang, error=t("backup_export_failed_postgres")),
                lang,
            )

        filename = f"filament_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.dump"
        return FileResponse(
            dump_path,
            media_type="application/octet-stream",
            filename=filename,
            background=BackgroundTask(_cleanup_temp_file, dump_path),
        )

    return render(request, "backup.html", _build_backup_context(lang, warning=t("backup_unsupported")), lang)


@app.post("/backup/import")
def backup_import(request: Request, file: UploadFile = File(...)):
    lang = get_lang(request)
    t = t_factory(lang)

    mode = _backup_mode()
    project = get_project(request)
    if mode == "unsupported":
        return render(request, "backup.html", _build_backup_context(lang, warning=t("backup_unsupported")), lang)

    if not file or not file.filename:
        return render(
            request,
            "backup.html",
            _build_backup_context(lang, error=t("backup_invalid_file")),
            lang,
        )

    raw, too_large = _read_upload_limited(file)
    if too_large:
        return render(
            request,
            "backup.html",
            _build_backup_context(lang, error=t("upload_too_large").format(max_mb=MAX_UPLOAD_MB)),
            lang,
        )
    if raw is None:
        return render(
            request,
            "backup.html",
            _build_backup_context(lang, error=t("backup_invalid_file")),
            lang,
        )

    if mode == "sqlite" and not raw.startswith(b"SQLite format 3\x00"):
        return render(
            request,
            "backup.html",
            _build_backup_context(lang, error=t("backup_invalid_file")),
            lang,
        )

    if mode == "postgresql":
        if not _pg_tools_available():
            return render(
                request,
                "backup.html",
                _build_backup_context(lang, warning=t("backup_pg_tools_missing")),
                lang,
            )

        if not raw.startswith(b"PGDMP"):
            return render(
                request,
                "backup.html",
                _build_backup_context(lang, error=t("backup_invalid_file_postgres")),
                lang,
            )

        tmp = tempfile.NamedTemporaryFile(prefix="filament_restore_", suffix=".dump", delete=False)
        tmp_path = Path(tmp.name)
        try:
            tmp.write(raw)
            tmp.close()
        except Exception:
            tmp.close()
            _cleanup_temp_file(tmp_path)
            return render(
                request,
                "backup.html",
                _build_backup_context(lang, error=t("backup_import_failed_postgres")),
                lang,
            )

        try:
            engine.dispose()
            cmd = [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                *_postgres_connection_args(),
                str(tmp_path),
            ]
            result = subprocess.run(cmd, env=_postgres_subprocess_env(), capture_output=True, text=True)
            if result.returncode != 0:
                return render(
                    request,
                    "backup.html",
                    _build_backup_context(lang, error=t("backup_import_failed_postgres")),
                    lang,
                )
        except Exception:
            return render(
                request,
                "backup.html",
                _build_backup_context(lang, error=t("backup_import_failed_postgres")),
                lang,
            )
        finally:
            _cleanup_temp_file(tmp_path)

        db_local = SessionLocal()
        try:
            _audit_log(
                db_local,
                project,
                "backup_import",
                request=request,
                entity_type="backup",
                details={"mode": mode, "filename": file.filename},
            )
            db_local.commit()
        finally:
            db_local.close()

        return render(
            request,
            "backup.html",
            _build_backup_context(lang, message=t("backup_import_done")),
            lang,
        )

    db_path = _sqlite_db_path()
    if not db_path:
        return render(
            request,
            "backup.html",
            _build_backup_context(lang, error=t("backup_import_failed")),
            lang,
        )

    tmp_path = Path("app/data/_restore_tmp.db")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(raw)

    try:
        engine.dispose()
        with sqlite3.connect(str(tmp_path)) as source_conn, sqlite3.connect(str(db_path)) as target_conn:
            source_conn.backup(target_conn)
    except Exception:
        return render(
            request,
            "backup.html",
            _build_backup_context(lang, error=t("backup_import_failed")),
            lang,
        )
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except PermissionError:
                pass

    db_local = SessionLocal()
    try:
        _audit_log(
            db_local,
            project,
            "backup_import",
            request=request,
            entity_type="backup",
            details={"mode": mode, "filename": file.filename},
        )
        db_local.commit()
    finally:
        db_local.close()

    return render(
        request,
        "backup.html",
        _build_backup_context(lang, message=t("backup_import_done")),
        lang,
    )


@app.post("/import-export")
@app.post("/import")
def import_data(
    request: Request,
    file: UploadFile = File(...),
    profile_name: Optional[str] = Form(None),
    save_profile_name: Optional[str] = Form(None),
    map_brand: Optional[str] = Form(None),
    map_material: Optional[str] = Form(None),
    map_color: Optional[str] = Form(None),
    map_weight_g: Optional[str] = Form(None),
    map_remaining_g: Optional[str] = Form(None),
    map_low_stock_threshold_g: Optional[str] = Form(None),
    map_price: Optional[str] = Form(None),
    map_location: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)

    import pandas as pd

    content, too_large = _read_upload_limited(file)
    if too_large:
        lang = get_lang(request)
        t = t_factory(lang)
        return render(request, "import.html", {"error": t("upload_too_large").format(max_mb=MAX_UPLOAD_MB)}, lang)
    if content is None:
        return RedirectResponse("/import-export", status_code=303)

    name = (file.filename or "").lower()
    if name.endswith(".csv"):
        df = pd.read_csv(BytesIO(content))
    elif name.endswith(".xlsx"):
        df = pd.read_excel(BytesIO(content))
    else:
        return RedirectResponse("/import-export", status_code=303)

    alias_map = _default_import_alias_map()
    raw_manual_map = {
        str(map_brand or "").strip(): "brand",
        str(map_material or "").strip(): "material",
        str(map_color or "").strip(): "color",
        str(map_weight_g or "").strip(): "weight_g",
        str(map_remaining_g or "").strip(): "remaining_g",
        str(map_low_stock_threshold_g or "").strip(): "low_stock_threshold_g",
        str(map_price or "").strip(): "price",
        str(map_location or "").strip(): "location",
    }
    manual_map: dict[str, str] = {}
    for source, target in raw_manual_map.items():
        key = _normalize_col_name(source)
        if key and target:
            manual_map[key] = target

    selected_profile_map = _load_import_mapping_profile(db, project, profile_name)
    effective_map: dict[str, str] = dict(alias_map)
    if selected_profile_map:
        effective_map.update(selected_profile_map)
    effective_map.update(manual_map)

    rename_map = {}
    for column_name in df.columns:
        normalized = _normalize_col_name(column_name)
        mapped = effective_map.get(normalized)
        if mapped:
            rename_map[column_name] = mapped
    df = df.rename(columns=rename_map)

    save_name = str(save_profile_name or "").strip()
    if save_name and manual_map:
        _save_import_mapping_profile(db, project, save_name, manual_map)

    created_count = 0
    for _, row in df.iterrows():
        spool = Spool(
            brand=str(row.get("brand", "")).strip(),
            material=str(row.get("material", "")).strip(),
            color=str(row.get("color", "")).strip(),
            weight_g=float(row.get("weight_g", 0) or 0),
            remaining_g=float(row.get("remaining_g", 0) or 0),
            low_stock_threshold_g=_parse_optional_float(row.get("low_stock_threshold_g")),
            price=float(row.get("price", 0) or 0) if row.get("price") == row.get("price") else None,
            location=str(row.get("location", "")).strip(),
            project=project,
        )
        _enforce_empty_lifecycle(spool)
        if spool.brand and spool.material and spool.color:
            db.add(spool)
            created_count += 1

    _audit_log(
        db,
        project,
        "import_spools",
        request=request,
        entity_type="spool",
        details={
            "filename": file.filename,
            "rows_total": int(len(df.index)),
            "rows_created": int(created_count),
            "profile_used": str(profile_name or "").strip() or None,
            "profile_saved": save_name or None,
        },
    )
    db.commit()

    return RedirectResponse("/", status_code=303)


@app.get("/export/csv")
def export_csv(request: Request, db: Session = Depends(get_db)):
    import pandas as pd

    project = get_project(request)
    spools = db.query(Spool).filter(Spool.project == project).all()
    data = [
        {
            "project": s.project,
            "brand": s.brand,
            "material": s.material,
            "color": s.color,
            "weight_g": s.weight_g,
            "remaining_g": s.remaining_g,
            "low_stock_threshold_g": s.low_stock_threshold_g,
            "price": s.price,
            "location": s.location,
        }
        for s in spools
    ]
    df = pd.DataFrame(data)
    buffer = BytesIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    _audit_log(
        db,
        project,
        "export_csv",
        request=request,
        entity_type="spool",
        details={"rows": int(len(data))},
    )
    db.commit()
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=filament_spools.csv"},
    )


@app.get("/export/excel")
def export_excel(request: Request, db: Session = Depends(get_db)):
    import pandas as pd

    project = get_project(request)
    spools = db.query(Spool).filter(Spool.project == project).all()
    data = [
        {
            "project": s.project,
            "brand": s.brand,
            "material": s.material,
            "color": s.color,
            "weight_g": s.weight_g,
            "remaining_g": s.remaining_g,
            "low_stock_threshold_g": s.low_stock_threshold_g,
            "price": s.price,
            "location": s.location,
        }
        for s in spools
    ]
    df = pd.DataFrame(data)
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)
    _audit_log(
        db,
        project,
        "export_excel",
        request=request,
        entity_type="spool",
        details={"rows": int(len(data))},
    )
    db.commit()
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=filament_spools.xlsx"},
    )
