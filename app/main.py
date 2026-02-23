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
import unicodedata
from pathlib import Path
from io import BytesIO
import shutil
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

from .db import Base, engine, get_db, SessionLocal
from .models import (
    Spool,
    UsageHistory,
    UsageBatchContext,
    DeviceSlotState,
    AppSetting,
    StorageArea,
    StorageSubLocation,
    User,
    UserApiToken,
    UserSession,
)
from .utils.three_mf import parse_3mf_filament_usage
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
DEFAULT_LABEL_LAYOUT = "a4_3x8_63_5x33_9"
DEFAULT_LABEL_PRINT_MODE = "sheet"
DEFAULT_LABEL_ORIENTATION = "horizontal"
LABEL_CONTENT_SETTING_KEY = "label_content"
CUSTOM_LABEL_LAYOUTS_SETTING_KEY = "custom_label_layouts"
CUSTOM_LABEL_LAYOUT_SETTING_PREFIX = "custom_label_layout:"
PRINTABLE_WIDTH_MM = 190.0
LABEL_GRID_GAP_MM = 4.0
STORAGE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
AUTH_SESSION_COOKIE = "session_token"
AUTH_SESSION_DAYS = max(1, int(float(str(os.getenv("AUTH_SESSION_DAYS", "30")).strip() or "30")))
AUTH_SESSION_MAX_AGE = AUTH_SESSION_DAYS * 24 * 60 * 60
AUTH_PBKDF2_ITERATIONS = max(120000, int(float(str(os.getenv("AUTH_PBKDF2_ITERATIONS", "240000")).strip() or "240000")))
DEFAULT_ADMIN_EMAIL = str(os.getenv("DEFAULT_ADMIN_EMAIL", "admin@filament.local")).strip().lower()
DEFAULT_ADMIN_PASSWORD = str(os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123456")).strip()
DEFAULT_ADMIN_NAME = str(os.getenv("DEFAULT_ADMIN_NAME", "Administrator")).strip() or "Administrator"
DEFAULT_ADMIN_API_TOKEN = str(os.getenv("DEFAULT_ADMIN_API_TOKEN", "")).strip()
LIFECYCLE_STATUS_VALUES = ["new", "opened", "dry_stored", "humidity_risk", "empty", "archived"]


def _env_truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_csv_list(value: Optional[str], fallback: list[str]) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return list(fallback)
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(fallback)


APP_ENV = str(os.getenv("APP_ENV", "development")).strip().lower()
LOG_LEVEL = str(os.getenv("LOG_LEVEL", "info")).strip().upper()
DEFAULT_COOKIE_SECURE = APP_ENV == "production"
COOKIE_SECURE = _env_truthy(os.getenv("COOKIE_SECURE"), default=DEFAULT_COOKIE_SECURE)
COOKIE_HTTPONLY = _env_truthy(os.getenv("COOKIE_HTTPONLY"), default=True)
ENABLE_BASIC_AUTH = _env_truthy(os.getenv("ENABLE_BASIC_AUTH"), default=False)
BASIC_AUTH_USERNAME = str(os.getenv("BASIC_AUTH_USERNAME", "")).strip()
BASIC_AUTH_PASSWORD = str(os.getenv("BASIC_AUTH_PASSWORD", "")).strip()
CSRF_PROTECT = _env_truthy(os.getenv("CSRF_PROTECT"), default=True)
STRICT_CSRF_CHECK = _env_truthy(os.getenv("STRICT_CSRF_CHECK"), default=False)
FORCE_HTTPS_REDIRECT = _env_truthy(os.getenv("FORCE_HTTPS_REDIRECT"), default=False)
ALLOWED_HOSTS = _env_csv_list(os.getenv("ALLOWED_HOSTS"), ["localhost", "127.0.0.1", "testserver"])
TRUSTED_ORIGINS = set(_env_csv_list(os.getenv("TRUSTED_ORIGINS"), []))
MAX_UPLOAD_MB = max(1, int(float(str(os.getenv("MAX_UPLOAD_MB", "25")).strip() or "25")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
SLOT_STATE_STALE_MINUTES = max(1, int(float(str(os.getenv("SLOT_STATE_STALE_MINUTES", "10")).strip() or "10")))
PUBLIC_PATH_PREFIXES = (
    "/static/",
    "/healthz",
    "/",
    "/landing",
    "/auth/login",
    "/auth/register",
    "/help",
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

LABEL_LAYOUTS = {
    "a4_3x8_63_5x33_9": {
        "label_de": "A4 Etiketten 24 (3×8, 63,5×33,9 mm · Avery/Zweckform kompatibel)",
        "label_en": "A4 labels 24 (3×8, 63.5×33.9 mm · Avery/Zweckform compatible)",
        "columns": 3,
        "cell_w_mm": 63.5,
        "cell_h_mm": 33.9,
    },
    "a4_3x7_63_5x38_1": {
        "label_de": "A4 Etiketten 21 (3×7, 63,5×38,1 mm · Avery L7160)",
        "label_en": "A4 labels 21 (3×7, 63.5×38.1 mm · Avery L7160)",
        "columns": 3,
        "cell_w_mm": 63.5,
        "cell_h_mm": 38.1,
    },
    "a4_2x8_99_1x33_9": {
        "label_de": "A4 Etiketten 16 (2×8, 99,1×33,9 mm · Avery L7162)",
        "label_en": "A4 labels 16 (2×8, 99.1×33.9 mm · Avery L7162)",
        "columns": 2,
        "cell_w_mm": 99.1,
        "cell_h_mm": 33.9,
    },
    "a4_2x7_99_1x38_1": {
        "label_de": "A4 Etiketten 14 (2×7, 99,1×38,1 mm · Avery L7163)",
        "label_en": "A4 labels 14 (2×7, 99.1×38.1 mm · Avery L7163)",
        "columns": 2,
        "cell_w_mm": 99.1,
        "cell_h_mm": 38.1,
    },
    "a4_2x4_99_1x67_7": {
        "label_de": "A4 Etiketten 8 (2×4, 99,1×67,7 mm · Avery L7165)",
        "label_en": "A4 labels 8 (2×4, 99.1×67.7 mm · Avery L7165)",
        "columns": 2,
        "cell_w_mm": 99.1,
        "cell_h_mm": 67.7,
    },
    "a4_1x10_189_9x25_4": {
        "label_de": "A4 Etiketten 10 (1×10, 189,9×25,4 mm · Avery L7651)",
        "label_en": "A4 labels 10 (1×10, 189.9×25.4 mm · Avery L7651)",
        "columns": 1,
        "cell_w_mm": 189.9,
        "cell_h_mm": 25.4,
    },
    "a4_2x2_105x74_25": {
        "label_de": "A4 Etiketten 4 (2×2, 105×74,25 mm)",
        "label_en": "A4 labels 4 (2×2, 105×74.25 mm)",
        "columns": 2,
        "cell_w_mm": 105.0,
        "cell_h_mm": 74.25,
    },
    "a4_4x12_48_5x25_4": {
        "label_de": "A4 Etiketten 48 (4×12, 48,5×25,4 mm)",
        "label_en": "A4 labels 48 (4×12, 48.5×25.4 mm)",
        "columns": 4,
        "cell_w_mm": 48.5,
        "cell_h_mm": 25.4,
    },
    "a4_4x16_48_5x16_9": {
        "label_de": "A4 Etiketten 64 (4×16, 48,5×16,9 mm · Avery L4732)",
        "label_en": "A4 labels 64 (4×16, 48.5×16.9 mm · Avery L4732)",
        "columns": 4,
        "cell_w_mm": 48.5,
        "cell_h_mm": 16.9,
    },
    "a4_cards_2x5": {
        "label_de": "A4 Karten (2 Spalten, flexibel)",
        "label_en": "A4 cards (2 columns, flexible)",
        "columns": 2,
        "cell_w_mm": 99.0,
        "cell_h_mm": 52.0,
    },
    "a4_full_page": {
        "label_de": "A4 Vollseite (1 Etikett · Avery J8167)",
        "label_en": "A4 full page (1 label · Avery J8167)",
        "columns": 1,
        "cell_w_mm": 190.0,
        "cell_h_mm": 277.0,
    },
}


def _format_decimal(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_weight_display(value: Optional[float]) -> dict[str, str]:
    grams = float(value or 0.0)
    sign = "-" if grams < 0 else ""
    grams_abs = abs(grams)
    kg = int(grams_abs // 1000)
    remainder_g = round(grams_abs - (kg * 1000), 1)

    if kg <= 0:
        return {"main": f"{sign}{_format_decimal(grams_abs)} g", "sub": ""}

    sub = f"{_format_decimal(remainder_g)} g" if remainder_g > 0 else ""
    return {"main": f"{sign}{kg} kg", "sub": sub}


def format_weight_text(value: Optional[float]) -> str:
    parts = format_weight_display(value)
    if parts["sub"]:
        return f"{parts['main']} {parts['sub']}"
    return parts["main"]


def format_length_display(value_m: Optional[float]) -> dict[str, str]:
    meters = float(value_m or 0.0)
    sign = "-" if meters < 0 else ""
    meters_abs = abs(meters)
    whole_m = int(meters_abs)
    remainder_mm = round((meters_abs - whole_m) * 1000, 1)

    if whole_m <= 0:
        return {"main": f"{sign}{_format_decimal(remainder_mm)} mm", "sub": ""}

    sub = f"{_format_decimal(remainder_mm)} mm" if remainder_mm > 0 else ""
    return {"main": f"{sign}{whole_m} m", "sub": sub}


def format_length_text(value_m: Optional[float]) -> str:
    parts = format_length_display(value_m)
    if parts["sub"]:
        return f"{parts['main']} {parts['sub']}"
    return parts["main"]


def format_number_compact(value: Optional[float], decimals: int = 2, lang: str = "de") -> str:
    if value is None:
        return "-"
    precision = max(0, int(decimals))
    formatted = f"{float(value):,.{precision}f}"
    if lang == "de":
        return formatted.replace(",", "#").replace(".", ",").replace("#", ".")
    return formatted


def format_currency_text(value: Optional[float], lang: str = "de") -> str:
    if value is None:
        return "-"
    return f"{format_number_compact(value, 2, lang)} €"


def format_length_compact(value_m: Optional[float], lang: str = "de") -> str:
    if value_m is None:
        return "-"
    return f"{format_number_compact(value_m, 2, lang)} m"


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

TRANSLATIONS = {
    "de": {
        "app_title": "Filament Datenbank",
        "app_subtitle": "Deine Filamentspulen im Blick",
        "footer_text": "Lokale Filament-Verwaltung",
        "stats_title": "Statistiken",
        "stat_total_spools": "Spulen gesamt",
        "stat_total_weight": "Gesamtgewicht",
        "stat_total_remaining": "Restmenge",
        "stat_total_value": "Warenwert",
        "stat_empty_spools": "Leere Spulen",
        "stat_low_stock_spools": "Niedriger Bestand",
        "kpi_month_usage": "Monatsverbrauch",
        "kpi_month_cost": "Kosten (Monat)",
        "kpi_usage_trend": "Verbrauch pro Monat",
        "kpi_cost_trend": "Kosten pro Monat",
        "kpi_top_material_trend": "Top-Material je Monat",
        "kpi_top_color_trend": "Top-Farbe je Monat",
        "kpi_last_months": "letzte 6 Monate",
        "kpi_no_usage_data": "Keine Verbrauchsdaten vorhanden.",
        "top5_materials": "Top 5 Materialien",
        "top5_colors": "Top 5 Farben",
        "top5_remaining_share": "Anteil Restmenge",
        "settings": "Einstellungen",
        "nav_booking": "Buchung",
        "nav_tracking": "Tracking",
        "nav_slot_status": "Slotstatus",
        "nav_storage_locations": "Lagerorte",
        "nav_help": "Hilfe",
        "nav_home": "Dashboard",
        "settings_language": "Sprache",
        "settings_theme": "Theme",
        "settings_project": "Projekt",
        "project_private": "Privat",
        "project_business": "Geschäftlich",
        "theme_light": "Hell",
        "theme_dark": "Dunkel",
        "theme_system": "System",
        "landing_title": "Willkommen bei der Filament Datenbank",
        "landing_subtitle": "Verwalte Bestand, Lagerorte und Verbrauch zentral.",
        "landing_auth_hint": "Die Authentifizierung ist vorbereitet und kann im nächsten Schritt mit echter Benutzerverwaltung verbunden werden.",
        "landing_cta_login": "Anmelden",
        "landing_cta_register": "Registrieren",
        "landing_cta_dashboard": "Zum Dashboard",
        "auth_email": "E-Mail",
        "auth_password": "Passwort",
        "auth_name": "Name",
        "login_title": "Anmeldung",
        "login_hint": "Melde dich mit deinem Konto an.",
        "register_title": "Registrierung",
        "register_hint": "Erstelle ein neues Konto.",
        "auth_submit_login": "Anmelden",
        "auth_submit_register": "Konto erstellen",
        "auth_coming_soon": "Authentifizierung ist vorbereitet. Backend-Anbindung folgt im nächsten Schritt.",
        "auth_logout": "Abmelden",
        "auth_login_success": "Erfolgreich angemeldet.",
        "auth_register_success": "Konto erstellt und angemeldet.",
        "auth_invalid_credentials": "Ungültige E-Mail oder Passwort.",
        "auth_email_exists": "Diese E-Mail ist bereits registriert.",
        "auth_password_too_short": "Passwort muss mindestens 8 Zeichen haben.",
        "auth_required": "Bitte zuerst anmelden.",
        "quick_actions": "Schnellzugriff",
        "data_section": "Daten",
        "backup_restore": "Backup / Restore",
        "analysis_tab": "Analyse",
        "thresholds_tab": "Schwellenwerte",
        "analysis_title": "Analyse",
        "thresholds_title": "Schwellenwerte",
        "thresholds_hint": "Übersicht aller gesetzten Low-Stock-Schwellenwerte.",
        "thresholds_material_defaults": "Material-Standardschwellen",
        "thresholds_spool_overrides": "Spulen-spezifische Schwellen",
        "thresholds_none_material": "Keine Material-Schwellenwerte gesetzt.",
        "thresholds_none_spool": "Keine Spulen-Schwellenwerte gesetzt.",
        "analysis_hint": "Bestände gruppiert nach Marke, Material, Farbe und Lagerort.",
        "analysis_by_brand": "Nach Marke",
        "analysis_by_material": "Nach Material",
        "analysis_by_color": "Nach Farbe",
        "analysis_by_location": "Nach Lagerort",
        "analysis_count": "Anzahl",
        "analysis_share": "Anteil Restmenge",
        "add_spool": "Neue Spule",
        "import_data": "Import",
        "export_csv": "CSV exportieren",
        "export_excel": "Excel exportieren",
        "spool_list": "Spulenliste",
        "search_placeholder": "Suchen nach Marke, Material, Farbe, Lagerort",
        "hide_empty_spools": "Leere Spulen ausblenden",
        "rows_per_page": "Zeilen pro Seite",
        "entries_label": "Einträge",
        "page_label": "Seite",
        "prev_page": "Zurück",
        "next_page": "Weiter",
        "all_colors": "Alle Farben",
        "search": "Suchen",
        "spool_index": "Index",
        "brand": "Marke",
        "material": "Material",
        "color": "Farbe",
        "bulk_add": "Mehrfach hinzufügen",
        "bulk_add_hint": "Mehrere Spulen in einem Schritt anlegen.",
        "add_row": "Zeile hinzufügen",
        "save_all": "Alle speichern",
        "quantity": "Anzahl",
        "quantity_hint": "Erstellt mehrere identische Spulen.",
        "manage_presets": "Vorgaben verwalten",
        "presets_title": "Vorgaben",
        "add_brand": "Hersteller hinzufügen",
        "add_material": "Material hinzufügen",
        "add_color": "Farbe hinzufügen",
        "add_color_map": "Farben zuweisen",
        "presets_basic_title": "Grunddaten hinzufügen",
        "presets_basic_hint": "Hersteller, Material oder Farben schnell ergänzen.",
        "assign_colors_title": "Farben zuordnen",
        "assign_colors_hint": "Wähle Hersteller und Material, dann Farben (kommagetrennt).",
        "import_colors": "Farben importieren",
        "import_colors_hint": "CSV/Excel mit Spalten: brand, material, color",
        "select_brand": "Hersteller wählen",
        "select_material": "Material wählen",
        "colors_list": "Farben (kommagetrennt)",
        "brand_hint": "Mehrere Hersteller mit Komma trennen",
        "material_hint": "Mehrere Materialien mit Komma trennen",
        "color_hint": "Mehrere Farben mit Komma trennen",
        "material_group": "Materialgruppe",
        "weight": "Gewicht",
        "remaining": "Restmenge",
        "threshold": "Schwelle",
        "price": "Preis",
        "location": "Lagerort",
        "storage_location": "Strukturierter Lagerort",
        "storage_location_none": "Kein strukturierter Lagerort",
        "storage_locations_title": "Lagerorte",
        "storage_locations_hint": "Verwalte Lagerbereiche und Fächer (z. B. REGAL1/FACH_A).",
        "storage_locations_add": "Lagerort hinzufügen",
        "storage_area_code": "Bereichscode",
        "storage_area_name": "Bereichsname",
        "storage_sub_code": "Fachcode",
        "storage_sub_name": "Fachname",
        "storage_path": "Pfad",
        "storage_usage": "Belegte Spulen",
        "storage_delete": "Lagerort löschen",
        "storage_none": "Keine Lagerorte vorhanden.",
        "storage_invalid_code": "Ungültiger Code. Erlaubt: Buchstaben/Zahlen sowie - und _.",
        "storage_location_exists": "Dieser Lagerort existiert bereits.",
        "storage_location_saved": "Lagerort wurde gespeichert.",
        "storage_location_deleted": "Lagerort wurde gelöscht.",
        "storage_location_in_use": "Lagerort ist noch Spulen zugewiesen und kann nicht gelöscht werden.",
        "storage_location_invalid": "Ausgewählter Lagerort ist ungültig.",
        "storage_filter": "Lagerort-Filter",
        "storage_filter_all": "Alle Lagerorte",
        "ams_printer": "AMS Drucker",
        "ams_slot": "AMS Slot",
        "status": "Status",
        "lifecycle_status": "Lebenszyklus",
        "lifecycle_filter_all": "Alle Lebenszyklus-Status",
        "lifecycle_new": "Neu",
        "lifecycle_opened": "Geöffnet",
        "lifecycle_dry_stored": "Trocken gelagert",
        "lifecycle_humidity_risk": "Feuchterisiko",
        "lifecycle_empty": "Leer",
        "lifecycle_archived": "Archiviert",
        "actions": "Aktionen",
        "in_use": "In Nutzung",
        "empty": "Leer",
        "low_stock": "Niedrig",
        "idle": "Inaktiv",
        "threshold_source_spool": "Spule",
        "threshold_source_material": "Material",
        "threshold_none": "-",
        "low_stock_threshold": "Low-Stock Schwelle",
        "material_thresholds_title": "Material-Schwellwerte",
        "material_thresholds_hint": "Standard-Schwellwert pro Spule nach Material in Gramm.",
        "material_total_threshold": "Material-Gesamtschwelle",
        "material_total_thresholds_title": "Material-Gesamtschwellen",
        "material_total_thresholds_hint": "Schwellwert auf Gesamtbestand pro Material in Gramm.",
        "reorder_list_title": "Nachbestellung erforderlich",
        "reorder_none": "Aktuell keine Nachbestellung nötig.",
        "reorder_missing": "Fehlmenge",
        "reorder_needed": "Nachbestellen",
        "reorder_ok": "OK",
        "toggle_use": "Toggle",
        "edit": "Bearbeiten",
        "delete": "Löschen",
        "qr": "QR",
        "confirm_delete": "Spule wirklich löschen?",
        "no_spools": "Keine Spulen vorhanden.",
        "save": "Speichern",
        "cancel": "Abbrechen",
        "usage_upload": "Buchung",
        "booking_area_title": "Buchung",
        "booking_section_book": "Buchen",
        "booking_section_tracking": "Tracking",
        "usage_hint": "Lade eine 3MF-Datei hoch. Falls Gramm nicht erkannt werden, gib sie manuell an.",
        "select_spools": "Spulen auswählen",
        "upload_3mf": "3MF-Datei",
        "manual_grams": "Manuelle Grammangabe",
        "manual_grams_hint": "Nur nötig, wenn die 3MF keine Grammangabe enthält.",
        "usage_no_grams": "In der 3MF wurde keine Grammangabe gefunden. Bitte manuell eingeben.",
        "usage_no_grams_bambu_unsliced": "In dieser Bambu-3MF sind keine Verbrauchsdaten enthalten (wahrscheinlich nicht gesliced). Bitte in Bambu Studio slicen und erneut speichern oder manuell eintragen.",
        "usage_no_match": "Keine passende Spule automatisch gefunden. Bitte manuell auswählen.",
        "usage_breakdown": "Erkannter Materialverbrauch",
        "usage_total": "Gesamt",
        "usage_total_length": "Gesamtlänge",
        "usage_filament_switches": "Filamentwechsel",
        "usage_estimated_cost": "Kosten",
        "usage_advanced_title": "Erweiterte Bambu-Statistik",
        "usage_history_title": "Verbrauchs-Historie",
        "usage_history_when": "Wann",
        "usage_history_who": "Wer",
        "usage_history_slicer": "Slicer",
        "usage_history_mode": "Modus",
        "usage_mode_auto": "Automatisch",
        "usage_mode_manual": "Manuell",
        "usage_mode_auto_slicer": "Automatisch (Slicer)",
        "usage_mode_auto_bambu": "Automatisch (Bambu Studio)",
        "usage_mode_upload_manual": "Datei-Upload (manuell)",
        "usage_mode_manual_entry": "Manuelle Eingabe",
        "usage_history_file": "3MF-Datei",
        "usage_history_spool": "Spule",
        "usage_history_spools": "Spulen",
        "usage_history_breakdown": "Aufteilung",
        "usage_history_spool_id": "Spulen-Index",
        "usage_history_amount": "Abzug",
        "usage_history_printer": "Drucker",
        "usage_history_ams_slots": "AMS-Slots",
        "ams_slot_conflict": "AMS Slot-Konflikt: Dieser Slot ist bereits einer anderen Spule zugeordnet.",
        "slot_status_title": "Soll/Ist Slotstatus",
        "slot_status_hint": "Vergleich zwischen gepflegter Spulenzuordnung und zuletzt gepolltem Gerätestatus.",
        "slot_status_printer": "Drucker",
        "slot_status_slot": "Slot",
        "slot_status_expected": "Soll (Spule)",
        "slot_status_observed": "Ist (Live)",
        "slot_status_state": "Status",
        "slot_status_seen": "Zuletzt gesehen",
        "slot_status_source": "Quelle",
        "slot_state_ok": "OK",
        "slot_state_mismatch": "Abweichung",
        "slot_state_missing": "Fehlt",
        "slot_state_stale": "Veraltet",
        "slot_state_unknown": "Unbekannt",
        "slot_status_no_mapped": "Keine gemappten Slots vorhanden.",
        "slot_status_no_live": "Keine Live-Slotdaten vorhanden.",
        "usage_history_empty": "Noch keine Verbrauchseinträge vorhanden.",
        "usage_undo_last": "Letzte Abbuchung rückgängig",
        "usage_undo_done": "Letzte Abbuchung wurde rückgängig gemacht.",
        "usage_undo_none": "Keine rückgängig machbare Abbuchung gefunden.",
        "usage_applied": "Verbrauch wurde erfolgreich abgezogen.",
        "usage_preview": "Verbrauchsvorschau",
        "usage_detected_spools": "Automatisch erkannte Spulen",
        "usage_apply_now": "Jetzt anwenden",
        "usage_manual_needed": "Bitte manuell Gramm und Spulen auswählen.",
        "usage_manual_mode": "Manueller Modus",
        "usage_no_file": "Bitte zuerst eine 3MF-Datei auswählen.",
        "usage_active_spools": "Aktive Spulen",
        "usage_deduction": "Abzug (g)",
        "usage_save_manual": "Manuell speichern",
        "usage_save_auto": "Automatisch speichern",
        "usage_preview_ready": "Automatische Vorschau bereit.",
        "apply_usage": "Verbrauch anwenden",
        "import": "Importieren",
        "import_hint": "Erlaubt: CSV oder Excel mit Spalten: brand, material, color, weight_g, remaining_g, price, location.",
        "backup_title": "Backup / Restore",
        "backup_hint": "Exportiere oder importiere die komplette Datenbank.",
        "backup_hint_sqlite": "SQLite-Modus: Export/Import als .db-Datei.",
        "backup_hint_postgres": "PostgreSQL-Modus: Export/Import als .dump-Datei (Custom Format).",
        "backup_export": "Backup exportieren",
        "backup_import": "Backup importieren",
        "backup_import_file": "Backup-Datei",
        "backup_import_done": "Backup wurde erfolgreich importiert.",
        "backup_invalid_file": "Ungültige Datei. Bitte eine SQLite-Backup-Datei (.db) hochladen.",
        "backup_invalid_file_postgres": "Ungültige Datei. Bitte eine PostgreSQL-Backup-Datei (.dump, Custom Format) hochladen.",
        "backup_import_failed": "Backup konnte nicht importiert werden.",
        "backup_import_failed_postgres": "PostgreSQL-Backup konnte nicht importiert werden.",
        "backup_export_failed_postgres": "PostgreSQL-Backup konnte nicht exportiert werden.",
        "backup_sqlite_only": "Backup/Restore in der Oberfläche ist aktuell nur mit SQLite verfügbar. Für PostgreSQL nutze bitte pg_dump/pg_restore.",
        "backup_pg_tools_missing": "PostgreSQL-Backup erfordert pg_dump und pg_restore im App-Container.",
        "backup_unsupported": "Backup/Restore wird für diesen Datenbanktyp nicht unterstützt.",
        "upload_too_large": "Datei ist zu groß. Maximum: {max_mb} MB.",
        "label_print": "Etikettendruck",
        "label_print_title": "Etikettendruck",
        "label_print_hint": "Wähle Spulen und ein Drucklayout (A4 oder Labelbogen).",
        "label_target": "Etikett-Typ",
        "label_target_spool": "Spulen",
        "label_target_location": "Lagerorte",
        "label_select_locations": "Lagerorte auswählen",
        "label_location_none_selected": "Bitte mindestens einen Lagerort auswählen.",
        "label_layout": "Layout",
        "label_custom_title": "Eigenes Label-Format",
        "label_custom_hint": "Eigenes Layout mit Spalten und Etikettgröße (mm) speichern.",
        "label_custom_name": "Name",
        "label_custom_columns": "Spalten",
        "label_custom_width": "Breite (mm)",
        "label_custom_height": "Höhe (mm)",
        "label_custom_columns_auto": "Spalten werden beim Generieren automatisch bestimmt.",
        "label_custom_add": "Format speichern",
        "label_custom_saved": "Eigenes Label-Format wurde gespeichert.",
        "label_custom_existing": "Gespeicherte Formate",
        "label_custom_error_name": "Bitte einen gültigen Namen angeben.",
        "label_custom_error_columns": "Spalten müssen zwischen 1 und 8 liegen.",
        "label_custom_error_size": "Breite und Höhe müssen größer als 0 sein.",
        "label_custom_error_exists": "Ein Label-Format mit diesem Namen existiert bereits.",
        "label_layout_a4": "A4 (Karten)",
        "label_layout_sheet": "Labelbogen (3×8)",
        "label_select_spools": "Spulen auswählen",
        "label_print_mode": "Druckmodus",
        "label_print_mode_sheet": "A4-Bogen",
        "label_print_mode_single": "Einzelnes Etikett (volle Größe)",
        "label_orientation": "Ausrichtung auf dem Etikett",
        "label_orientation_horizontal": "Horizontal",
        "label_orientation_vertical": "Vertikal",
        "label_content": "Inhalt",
        "label_field_spool_id": "Spulen-ID",
        "label_field_brand": "Marke",
        "label_field_material_color": "Material + Farbe",
        "label_field_weight": "Gewicht",
        "label_field_remaining": "Restmenge",
        "label_field_location": "Lagerort",
        "label_save_defaults": "Als Standard speichern",
        "label_defaults_saved": "Label-Einstellungen wurden als Standard gespeichert.",
        "label_generate": "Etiketten erzeugen",
        "label_none_selected": "Bitte mindestens eine Spule auswählen.",
        "label_print_now": "Drucken",
        "label_back": "Zurück",
        "qr_scan": "QR-Scan",
        "qr_scan_title": "QR-Scan (Etikett einlesen)",
        "qr_scan_hint": "Scanne den QR-Code, um Spuleninfos zu öffnen und den Status zu ändern.",
        "qr_scan_input": "QR-Inhalt",
        "qr_scan_lookup": "Spule laden",
        "qr_scan_start_camera": "Kamera starten",
        "qr_scan_stop_camera": "Kamera stoppen",
        "qr_scan_camera_unsupported": "Kamera-Scan wird auf diesem Gerät/Brower nicht unterstützt. Du kannst den QR-Inhalt manuell einfügen.",
        "qr_scan_camera_requires_https": "Kamera benötigt meist HTTPS (oder localhost). Nutze alternativ Bild-Upload.",
        "qr_scan_upload_image": "QR-Bild hochladen",
        "qr_scan_decode_image": "Bild einlesen",
        "qr_scan_image_no_qr": "Im Bild wurde kein QR-Code erkannt.",
        "qr_scan_invalid": "QR-Code konnte nicht gelesen werden.",
        "qr_scan_not_found": "Keine passende Spule gefunden.",
        "qr_scan_location_loaded": "Lagerort wurde geladen.",
        "qr_scan_loaded": "Spule wurde geladen.",
        "qr_scan_status": "Status",
        "qr_scan_action_empty": "Als leer markieren",
        "qr_scan_action_in_use": "Als in Nutzung markieren",
        "qr_scan_action_idle": "Als nicht in Nutzung markieren",
        "qr_scan_lifecycle_label": "Lebenszyklus",
        "qr_scan_action_set_lifecycle": "Lebenszyklus speichern",
        "qr_scan_action_done_empty": "Spule wurde als leer markiert.",
        "qr_scan_action_done_in_use": "Spule wurde als in Nutzung markiert.",
        "qr_scan_action_done_idle": "Spule wurde als nicht in Nutzung markiert.",
        "qr_scan_action_done_lifecycle": "Lebenszyklus wurde aktualisiert.",
        "qr_scan_action_invalid_lifecycle": "Ungültiger Lebenszyklus-Status.",
        "qr_scan_action_invalid": "Unbekannte Aktion.",
        "qr_scan_manage_title": "Spulenstatus setzen",
        "qr_scan_manage_hint": "Setze den Status direkt nach dem Scan.",
        "qr_scan_back_to_scan": "Zurück zum Scanner",
        "qr_scan_auto_back": "Nach Aktion automatisch zurück zum Scanner",
        "qr_scan_next_ready": "Status gespeichert. Scanner ist bereit für die nächste Spule.",
    },
    "en": {
        "app_title": "Filament Database",
        "app_subtitle": "Track your filament spools",
        "footer_text": "Local filament management",
        "stats_title": "Statistics",
        "stat_total_spools": "Total spools",
        "stat_total_weight": "Total weight",
        "stat_total_remaining": "Remaining",
        "stat_total_value": "Inventory value",
        "stat_empty_spools": "Empty spools",
        "stat_low_stock_spools": "Low stock",
        "kpi_month_usage": "Monthly usage",
        "kpi_month_cost": "Cost (month)",
        "kpi_usage_trend": "Usage per month",
        "kpi_cost_trend": "Cost per month",
        "kpi_top_material_trend": "Top material per month",
        "kpi_top_color_trend": "Top color per month",
        "kpi_last_months": "last 6 months",
        "kpi_no_usage_data": "No usage data available.",
        "top5_materials": "Top 5 materials",
        "top5_colors": "Top 5 colors",
        "top5_remaining_share": "Remaining share",
        "settings": "Settings",
        "nav_booking": "Booking",
        "nav_tracking": "Tracking",
        "nav_slot_status": "Slot status",
        "nav_storage_locations": "Storage locations",
        "nav_help": "Help",
        "nav_home": "Dashboard",
        "settings_language": "Language",
        "settings_theme": "Theme",
        "settings_project": "Project",
        "project_private": "Private",
        "project_business": "Business",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "theme_system": "System",
        "landing_title": "Welcome to Filament Database",
        "landing_subtitle": "Manage inventory, storage locations, and usage in one place.",
        "landing_auth_hint": "Authentication is prepared and can be connected to real user management in the next step.",
        "landing_cta_login": "Sign in",
        "landing_cta_register": "Register",
        "landing_cta_dashboard": "Open dashboard",
        "auth_email": "Email",
        "auth_password": "Password",
        "auth_name": "Name",
        "login_title": "Sign in",
        "login_hint": "Sign in with your account.",
        "register_title": "Register",
        "register_hint": "Create a new account.",
        "auth_submit_login": "Sign in",
        "auth_submit_register": "Create account",
        "auth_coming_soon": "Authentication scaffolding is in place. Backend integration follows in the next step.",
        "auth_logout": "Sign out",
        "auth_login_success": "Successfully signed in.",
        "auth_register_success": "Account created and signed in.",
        "auth_invalid_credentials": "Invalid email or password.",
        "auth_email_exists": "This email is already registered.",
        "auth_password_too_short": "Password must be at least 8 characters.",
        "auth_required": "Please sign in first.",
        "quick_actions": "Quick actions",
        "data_section": "Data",
        "backup_restore": "Backup / Restore",
        "analysis_tab": "Analysis",
        "thresholds_tab": "Thresholds",
        "analysis_title": "Analysis",
        "thresholds_title": "Thresholds",
        "thresholds_hint": "Overview of all configured low-stock thresholds.",
        "thresholds_material_defaults": "Material default thresholds",
        "thresholds_spool_overrides": "Spool-specific thresholds",
        "thresholds_none_material": "No material thresholds set.",
        "thresholds_none_spool": "No spool thresholds set.",
        "analysis_hint": "Inventory grouped by brand, material, color, and location.",
        "analysis_by_brand": "By brand",
        "analysis_by_material": "By material",
        "analysis_by_color": "By color",
        "analysis_by_location": "By location",
        "analysis_count": "Count",
        "analysis_share": "Remaining share",
        "add_spool": "Add spool",
        "import_data": "Import",
        "export_csv": "Export CSV",
        "export_excel": "Export Excel",
        "spool_list": "Spool list",
        "search_placeholder": "Search brand, material, color, location",
        "hide_empty_spools": "Hide empty spools",
        "rows_per_page": "Rows per page",
        "entries_label": "entries",
        "page_label": "Page",
        "prev_page": "Previous",
        "next_page": "Next",
        "all_colors": "All colors",
        "search": "Search",
        "spool_index": "Index",
        "brand": "Brand",
        "material": "Material",
        "color": "Color",
        "bulk_add": "Bulk add",
        "bulk_add_hint": "Create multiple spools in one step.",
        "add_row": "Add row",
        "save_all": "Save all",
        "quantity": "Quantity",
        "quantity_hint": "Creates multiple identical spools.",
        "manage_presets": "Manage presets",
        "presets_title": "Presets",
        "add_brand": "Add brand",
        "add_material": "Add material",
        "add_color": "Add color",
        "add_color_map": "Assign colors",
        "presets_basic_title": "Add basics",
        "presets_basic_hint": "Quickly add brands, materials, or colors.",
        "assign_colors_title": "Assign colors",
        "assign_colors_hint": "Choose brand and material, then add colors (comma-separated).",
        "import_colors": "Import colors",
        "import_colors_hint": "CSV/Excel with columns: brand, material, color",
        "select_brand": "Select brand",
        "select_material": "Select material",
        "colors_list": "Colors (comma-separated)",
        "brand_hint": "Separate multiple brands with commas",
        "material_hint": "Separate multiple materials with commas",
        "color_hint": "Separate multiple colors with commas",
        "material_group": "Material group",
        "weight": "Weight",
        "remaining": "Remaining",
        "threshold": "Threshold",
        "price": "Price",
        "location": "Location",
        "storage_location": "Structured location",
        "storage_location_none": "No structured location",
        "storage_locations_title": "Storage locations",
        "storage_locations_hint": "Manage storage areas and bins (e.g. RACK1/BIN_A).",
        "storage_locations_add": "Add storage location",
        "storage_area_code": "Area code",
        "storage_area_name": "Area name",
        "storage_sub_code": "Bin code",
        "storage_sub_name": "Bin name",
        "storage_path": "Path",
        "storage_usage": "Assigned spools",
        "storage_delete": "Delete location",
        "storage_none": "No storage locations available.",
        "storage_invalid_code": "Invalid code. Allowed: letters/numbers plus - and _.",
        "storage_location_exists": "This storage location already exists.",
        "storage_location_saved": "Storage location was saved.",
        "storage_location_deleted": "Storage location was deleted.",
        "storage_location_in_use": "Storage location is still assigned to spools and cannot be deleted.",
        "storage_location_invalid": "Selected storage location is invalid.",
        "storage_filter": "Location filter",
        "storage_filter_all": "All locations",
        "ams_printer": "AMS printer",
        "ams_slot": "AMS slot",
        "status": "Status",
        "lifecycle_status": "Lifecycle",
        "lifecycle_filter_all": "All lifecycle statuses",
        "lifecycle_new": "New",
        "lifecycle_opened": "Opened",
        "lifecycle_dry_stored": "Dry stored",
        "lifecycle_humidity_risk": "Humidity risk",
        "lifecycle_empty": "Empty",
        "lifecycle_archived": "Archived",
        "actions": "Actions",
        "in_use": "In use",
        "empty": "Empty",
        "low_stock": "Low stock",
        "idle": "Idle",
        "threshold_source_spool": "Spool",
        "threshold_source_material": "Material",
        "threshold_none": "-",
        "low_stock_threshold": "Low-stock threshold",
        "material_thresholds_title": "Material thresholds",
        "material_thresholds_hint": "Default per-spool threshold by material in grams.",
        "material_total_threshold": "Material total threshold",
        "material_total_thresholds_title": "Material total thresholds",
        "material_total_thresholds_hint": "Threshold on total inventory per material in grams.",
        "reorder_list_title": "Reorder needed",
        "reorder_none": "No reorder needed right now.",
        "reorder_missing": "Missing",
        "reorder_needed": "Reorder",
        "reorder_ok": "OK",
        "toggle_use": "Toggle",
        "edit": "Edit",
        "delete": "Delete",
        "qr": "QR",
        "confirm_delete": "Delete this spool?",
        "no_spools": "No spools found.",
        "save": "Save",
        "cancel": "Cancel",
        "usage_upload": "Booking",
        "booking_area_title": "Booking",
        "booking_section_book": "Book",
        "booking_section_tracking": "Tracking",
        "usage_hint": "Upload a 3MF file. If grams are missing, enter them manually.",
        "select_spools": "Select spools",
        "upload_3mf": "3MF file",
        "manual_grams": "Manual grams",
        "manual_grams_hint": "Only needed if the 3MF has no grams metadata.",
        "usage_no_grams": "No gram value was found in the 3MF. Please enter it manually.",
        "usage_no_grams_bambu_unsliced": "This Bambu 3MF does not contain usage data (likely not sliced). Slice in Bambu Studio and save again, or enter values manually.",
        "usage_no_match": "No matching spool was auto-detected. Please select one manually.",
        "usage_breakdown": "Detected material usage",
        "usage_total": "Total",
        "usage_total_length": "Total length",
        "usage_filament_switches": "Filament switches",
        "usage_estimated_cost": "Cost",
        "usage_advanced_title": "Advanced Bambu statistics",
        "usage_history_title": "Usage history",
        "usage_history_when": "When",
        "usage_history_who": "Who",
        "usage_history_slicer": "Slicer",
        "usage_history_mode": "Mode",
        "usage_mode_auto": "Automatic",
        "usage_mode_manual": "Manual",
        "usage_mode_auto_slicer": "Automatic (Slicer)",
        "usage_mode_auto_bambu": "Automatic (Bambu Studio)",
        "usage_mode_upload_manual": "File upload (manual)",
        "usage_mode_manual_entry": "Manual entry",
        "usage_history_file": "3MF file",
        "usage_history_spool": "Spool",
        "usage_history_spools": "Spools",
        "usage_history_breakdown": "Breakdown",
        "usage_history_spool_id": "Spool index",
        "usage_history_amount": "Deduction",
        "usage_history_printer": "Printer",
        "usage_history_ams_slots": "AMS slots",
        "ams_slot_conflict": "AMS slot conflict: this slot is already assigned to another spool.",
        "slot_status_title": "Expected/Live slot status",
        "slot_status_hint": "Compares configured spool mapping with the latest polled device state.",
        "slot_status_printer": "Printer",
        "slot_status_slot": "Slot",
        "slot_status_expected": "Expected (spool)",
        "slot_status_observed": "Live",
        "slot_status_state": "State",
        "slot_status_seen": "Last seen",
        "slot_status_source": "Source",
        "slot_state_ok": "OK",
        "slot_state_mismatch": "Mismatch",
        "slot_state_missing": "Missing",
        "slot_state_stale": "Stale",
        "slot_state_unknown": "Unknown",
        "slot_status_no_mapped": "No mapped slots found.",
        "slot_status_no_live": "No live slot data available.",
        "usage_history_empty": "No usage entries yet.",
        "usage_undo_last": "Undo last deduction",
        "usage_undo_done": "Last deduction was undone.",
        "usage_undo_none": "No undoable deduction found.",
        "usage_applied": "Usage was successfully deducted.",
        "usage_preview": "Usage preview",
        "usage_detected_spools": "Auto-detected spools",
        "usage_apply_now": "Apply now",
        "usage_manual_needed": "Please choose grams and spools manually.",
        "usage_manual_mode": "Manual mode",
        "usage_no_file": "Please select a 3MF file first.",
        "usage_active_spools": "Active spools",
        "usage_deduction": "Deduction (g)",
        "usage_save_manual": "Save manual",
        "usage_save_auto": "Save automatic",
        "usage_preview_ready": "Automatic preview is ready.",
        "apply_usage": "Apply usage",
        "import": "Import",
        "import_hint": "Allowed: CSV or Excel with columns: brand, material, color, weight_g, remaining_g, price, location.",
        "backup_title": "Backup / Restore",
        "backup_hint": "Export or import the complete database.",
        "backup_hint_sqlite": "SQLite mode: export/import as a .db file.",
        "backup_hint_postgres": "PostgreSQL mode: export/import as a .dump file (custom format).",
        "backup_export": "Export backup",
        "backup_import": "Import backup",
        "backup_import_file": "Backup file",
        "backup_import_done": "Backup imported successfully.",
        "backup_invalid_file": "Invalid file. Please upload an SQLite backup file (.db).",
        "backup_invalid_file_postgres": "Invalid file. Please upload a PostgreSQL backup file (.dump, custom format).",
        "backup_import_failed": "Backup could not be imported.",
        "backup_import_failed_postgres": "PostgreSQL backup could not be imported.",
        "backup_export_failed_postgres": "PostgreSQL backup could not be exported.",
        "backup_sqlite_only": "In-app backup/restore is currently available for SQLite only. For PostgreSQL, use pg_dump/pg_restore.",
        "backup_pg_tools_missing": "PostgreSQL backup requires pg_dump and pg_restore in the app container.",
        "backup_unsupported": "Backup/restore is not supported for this database type.",
        "upload_too_large": "File is too large. Maximum: {max_mb} MB.",
        "label_print": "Label printing",
        "label_print_title": "Label printing",
        "label_print_hint": "Select spools and a print layout (A4 or label sheet).",
        "label_target": "Label target",
        "label_target_spool": "Spools",
        "label_target_location": "Storage locations",
        "label_select_locations": "Select storage locations",
        "label_location_none_selected": "Please select at least one storage location.",
        "label_layout": "Layout",
        "label_custom_title": "Custom label format",
        "label_custom_hint": "Save your own layout with columns and label size (mm).",
        "label_custom_name": "Name",
        "label_custom_columns": "Columns",
        "label_custom_width": "Width (mm)",
        "label_custom_height": "Height (mm)",
        "label_custom_columns_auto": "Columns are determined automatically when generating labels.",
        "label_custom_add": "Save format",
        "label_custom_saved": "Custom label format was saved.",
        "label_custom_existing": "Saved formats",
        "label_custom_error_name": "Please enter a valid name.",
        "label_custom_error_columns": "Columns must be between 1 and 8.",
        "label_custom_error_size": "Width and height must be greater than 0.",
        "label_custom_error_exists": "A label format with this name already exists.",
        "label_layout_a4": "A4 (cards)",
        "label_layout_sheet": "Label sheet (3×8)",
        "label_select_spools": "Select spools",
        "label_print_mode": "Print mode",
        "label_print_mode_sheet": "A4 sheet",
        "label_print_mode_single": "Single label (full size)",
        "label_orientation": "Orientation on label",
        "label_orientation_horizontal": "Horizontal",
        "label_orientation_vertical": "Vertical",
        "label_content": "Content",
        "label_field_spool_id": "Spool ID",
        "label_field_brand": "Brand",
        "label_field_material_color": "Material + color",
        "label_field_weight": "Weight",
        "label_field_remaining": "Remaining",
        "label_field_location": "Location",
        "label_save_defaults": "Save as default",
        "label_defaults_saved": "Label preferences were saved as default.",
        "label_generate": "Generate labels",
        "label_none_selected": "Please select at least one spool.",
        "label_print_now": "Print",
        "label_back": "Back",
        "qr_scan": "QR scan",
        "qr_scan_title": "QR scan (read label)",
        "qr_scan_hint": "Scan the QR code to open spool details and update status.",
        "qr_scan_input": "QR payload",
        "qr_scan_lookup": "Load spool",
        "qr_scan_start_camera": "Start camera",
        "qr_scan_stop_camera": "Stop camera",
        "qr_scan_camera_unsupported": "Camera scanning is not supported on this device/browser. You can paste the QR payload manually.",
        "qr_scan_camera_requires_https": "Camera access usually requires HTTPS (or localhost). Use image upload as fallback.",
        "qr_scan_upload_image": "Upload QR image",
        "qr_scan_decode_image": "Read image",
        "qr_scan_image_no_qr": "No QR code detected in image.",
        "qr_scan_invalid": "Could not parse QR code.",
        "qr_scan_not_found": "No matching spool found.",
        "qr_scan_location_loaded": "Storage location loaded.",
        "qr_scan_loaded": "Spool loaded.",
        "qr_scan_status": "Status",
        "qr_scan_action_empty": "Mark as empty",
        "qr_scan_action_in_use": "Mark as in use",
        "qr_scan_action_idle": "Mark as not in use",
        "qr_scan_lifecycle_label": "Lifecycle",
        "qr_scan_action_set_lifecycle": "Save lifecycle",
        "qr_scan_action_done_empty": "Spool marked as empty.",
        "qr_scan_action_done_in_use": "Spool marked as in use.",
        "qr_scan_action_done_idle": "Spool marked as not in use.",
        "qr_scan_action_done_lifecycle": "Lifecycle was updated.",
        "qr_scan_action_invalid_lifecycle": "Invalid lifecycle status.",
        "qr_scan_action_invalid": "Unknown action.",
        "qr_scan_manage_title": "Set spool status",
        "qr_scan_manage_hint": "Adjust status directly after scanning.",
        "qr_scan_back_to_scan": "Back to scanner",
        "qr_scan_auto_back": "Automatically return to scanner after action",
        "qr_scan_next_ready": "Status saved. Scanner is ready for the next spool.",
    },
}


def _run_startup_tasks() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_default_admin_user()
    _migrate_legacy_projects_to_user_scope()
    if _is_postgresql_database():
        _sync_postgres_id_sequences()
        return

    if not _is_sqlite_database():
        return

    _apply_legacy_sqlite_schema_patches()


def _ensure_default_admin_user() -> None:
    email = _normalize_email(DEFAULT_ADMIN_EMAIL)
    password = str(DEFAULT_ADMIN_PASSWORD or "").strip()
    if not email or not password:
        return

    db = SessionLocal()
    try:
        user_count = db.query(func.count(User.id)).scalar() or 0
        existing = db.query(User).filter(User.email == email).first()
        if existing is None and int(user_count) == 0:
            user = User(
                email=email,
                display_name=DEFAULT_ADMIN_NAME,
                password_hash=_hash_password(password),
                is_active=True,
            )
            db.add(user)
            db.flush()

            api_token = str(DEFAULT_ADMIN_API_TOKEN or "").strip()
            if api_token:
                db.add(
                    UserApiToken(
                        user_id=user.id,
                        name="default",
                        token_hash=_hash_secret_value(api_token),
                        is_active=True,
                    )
                )
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _migrate_legacy_projects_to_user_scope() -> None:
    db = SessionLocal()
    try:
        first_user = db.query(User).order_by(User.id.asc()).first()
        if first_user is None:
            return

        from_private = "private"
        from_business = "business"
        to_private = _project_scope_for_user(first_user.id, "private")
        to_business = _project_scope_for_user(first_user.id, "business")

        table_column_pairs = [
            (Spool.__tablename__, "project"),
            (UsageHistory.__tablename__, "project"),
            (UsageBatchContext.__tablename__, "project"),
            (DeviceSlotState.__tablename__, "project"),
            (StorageArea.__tablename__, "project"),
            (StorageSubLocation.__tablename__, "project"),
        ]

        changed = False
        for table_name, column_name in table_column_pairs:
            private_count = db.execute(
                text(f"SELECT COUNT(1) FROM {table_name} WHERE {column_name} = :project"),
                {"project": from_private},
            ).scalar() or 0
            if int(private_count) > 0:
                db.execute(
                    text(f"UPDATE {table_name} SET {column_name} = :target WHERE {column_name} = :source"),
                    {"source": from_private, "target": to_private},
                )
                changed = True

            business_count = db.execute(
                text(f"SELECT COUNT(1) FROM {table_name} WHERE {column_name} = :project"),
                {"project": from_business},
            ).scalar() or 0
            if int(business_count) > 0:
                db.execute(
                    text(f"UPDATE {table_name} SET {column_name} = :target WHERE {column_name} = :source"),
                    {"source": from_business, "target": to_business},
                )
                changed = True

            try:
                rows = db.execute(
                    text(f"SELECT id, {column_name}, user_id FROM {table_name}")
                ).fetchall()
                for row_id, row_project, row_user_id in rows:
                    if row_user_id is not None:
                        continue
                    scoped_user_id = _extract_user_id_from_scope(row_project)
                    resolved_user_id = int(scoped_user_id) if scoped_user_id else int(first_user.id)
                    db.execute(
                        text(f"UPDATE {table_name} SET user_id = :user_id WHERE id = :row_id"),
                        {"user_id": resolved_user_id, "row_id": int(row_id)},
                    )
                    changed = True
            except Exception:
                pass

        if changed:
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


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


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        AUTH_PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${AUTH_PBKDF2_ITERATIONS}${salt}${digest}"


def _verify_password(password: str, encoded_hash: str) -> bool:
    raw = str(encoded_hash or "")
    try:
        scheme, iterations_raw, salt, digest = raw.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
    except Exception:
        return False

    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(candidate, digest)


def _is_scoped_project_key(project: Optional[str]) -> bool:
    return bool(re.match(r"^u\d+_(private|business)$", str(project or "").strip().lower()))


def _base_project_preference(request: Request) -> str:
    candidate = request.query_params.get("project") or request.cookies.get("project") or _load_setting_from_db("project")
    return _normalize_project(candidate)


def _project_scope_for_user(user_id: int, base_project: str) -> str:
    normalized = _normalize_project(base_project)
    return f"u{int(user_id)}_{normalized}"


def _extract_base_project_from_scope(project_scope: str) -> str:
    raw = str(project_scope or "").strip().lower()
    match = re.match(r"^u\d+_(private|business)$", raw)
    if match:
        return _normalize_project(match.group(1))
    return _normalize_project(raw)


def _extract_user_id_from_scope(project_scope: Optional[str]) -> Optional[int]:
    raw = str(project_scope or "").strip().lower()
    match = re.match(r"^u(?P<user_id>\d+)_(private|business)$", raw)
    if not match:
        return None
    try:
        return int(match.group("user_id"))
    except Exception:
        return None


def _current_user_id(request: Request) -> Optional[int]:
    user = get_current_user(request)
    if user is not None:
        try:
            return int(user.id)
        except Exception:
            return None
    return _extract_user_id_from_scope(get_project(request))


def _model_scope_filters(model, project: str, user_id: Optional[int]) -> list:
    filters = []
    if hasattr(model, "project"):
        filters.append(getattr(model, "project") == project)
    resolved_user_id = user_id if user_id is not None else _extract_user_id_from_scope(project)
    if resolved_user_id is not None and hasattr(model, "user_id"):
        filters.append(getattr(model, "user_id") == int(resolved_user_id))
    return filters


def _scoped_query(db: Session, model, project: str, user_id: Optional[int]):
    return db.query(model).filter(*_model_scope_filters(model, project, user_id))


def _resolve_user_by_session_token(token: Optional[str]) -> Optional[User]:
    raw_token = str(token or "").strip()
    if not raw_token:
        return None

    token_hash = _hash_secret_value(raw_token)
    now = _utcnow().replace(tzinfo=None)
    db = SessionLocal()
    try:
        session_row = (
            db.query(UserSession)
            .filter(
                UserSession.token_hash == token_hash,
                UserSession.expires_at > now,
            )
            .first()
        )
        if session_row is None:
            return None

        user = (
            db.query(User)
            .filter(User.id == session_row.user_id, User.is_active.is_(True))
            .first()
        )
        if user is None:
            return None

        session_row.updated_at = _utcnow()
        db.commit()
        db.refresh(user)
        return user
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()


def get_current_user(request: Request) -> Optional[User]:
    state_user = getattr(request.state, "current_user", None)
    if state_user is not None:
        return state_user

    token = request.cookies.get(AUTH_SESSION_COOKIE)
    user = _resolve_user_by_session_token(token)
    request.state.current_user = user
    return user


def get_theme(request: Request) -> str:
    theme = request.cookies.get("theme") or _load_setting_from_db("theme") or "system"
    if theme not in VALID_THEMES:
        return "system"
    return theme


def _normalize_project(project: Optional[str]) -> str:
    candidate = str(project or "").strip().lower()
    return candidate if candidate in PROJECT_OPTIONS else DEFAULT_PROJECT


def get_project(request: Request) -> str:
    base_project = _base_project_preference(request)
    user = get_current_user(request)
    if user is None:
        return base_project
    return _project_scope_for_user(user.id, base_project)


def _effective_project_for_request(request: Request, project_override: Optional[str] = None) -> str:
    user = get_current_user(request)
    base = _normalize_project(project_override) if project_override is not None else _base_project_preference(request)
    if user is None:
        return base
    return _project_scope_for_user(user.id, base)


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


def _build_label_content_settings(overrides: Optional[dict[str, bool]] = None) -> dict[str, bool]:
    settings = _default_label_content_settings()
    if overrides:
        for key, value in overrides.items():
            if key in settings:
                settings[key] = bool(value)

    text_fields = [
        "show_spool_id",
        "show_brand",
        "show_material_color",
        "show_weight",
        "show_remaining",
        "show_location",
    ]
    if not any(settings.get(field, False) for field in text_fields):
        settings["show_spool_id"] = True
        settings["show_material_color"] = True

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


def _is_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _set_cookie(response, key: str, value: str, max_age: int = SETTINGS_COOKIE_MAX_AGE) -> None:
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        samesite="lax",
        secure=COOKIE_SECURE,
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


def _is_auth_public_path(path: str) -> bool:
    normalized = str(path or "").strip() or "/"
    auth_public_prefixes = (
        "/",
        "/landing",
        "/auth/login",
        "/auth/register",
        "/help",
        "/healthz",
        "/static/",
    )
    for prefix in auth_public_prefixes:
        if prefix == "/":
            if normalized == "/":
                return True
            continue
        if normalized == prefix or normalized.startswith(prefix):
            return True
    return False


def _resolve_user_by_api_token(request: Request) -> Optional[User]:
    auth_header = str(request.headers.get("authorization") or "").strip()
    header_token = str(request.headers.get("x-api-token") or "").strip()
    token = header_token
    if not token and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None

    token_hash = _hash_secret_value(token)
    db = SessionLocal()
    try:
        token_row = (
            db.query(UserApiToken)
            .filter(
                UserApiToken.token_hash == token_hash,
                UserApiToken.is_active.is_(True),
            )
            .first()
        )
        if token_row is None:
            return None

        user = db.query(User).filter(User.id == token_row.user_id, User.is_active.is_(True)).first()
        if user is None:
            return None

        token_row.last_used_at = _utcnow()
        db.commit()
        db.refresh(user)
        return user
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()


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
    request.state.current_user = _resolve_user_by_session_token(request.cookies.get(AUTH_SESSION_COOKIE))

    if request.state.current_user is None and request.url.path.startswith("/api/"):
        request.state.current_user = _resolve_user_by_api_token(request)

    if not _is_auth_public_path(request.url.path):
        if request.state.current_user is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"ok": False, "error": "auth_required"}, status_code=401)
            next_url = str(request.url.path or "/")
            query = str(request.url.query or "").strip()
            if query:
                next_url = f"{next_url}?{query}"
            redirect_target = f"/auth/login?{urlencode({'next_url': next_url})}"
            return RedirectResponse(redirect_target, status_code=303)

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

    return await call_next(request)


def _normalize_next_url(next_url: Optional[str]) -> str:
    target = str(next_url or "").strip()
    if not target.startswith("/"):
        return "/"
    return target


def _extract_spool_id_from_qr_payload(payload: Optional[str]) -> Optional[int]:
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


def _extract_location_path_from_qr_payload(payload: Optional[str], project: str) -> Optional[str]:
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
    area_code = _normalize_storage_area_code(area_raw)
    sub_code = _normalize_storage_sub_code(sub_raw)
    if area_code is None or sub_code is None:
        return None
    return _storage_path_code(area_code, sub_code)


def _normalize_storage_code(value: Optional[str]) -> str:
    return str(value or "").strip().upper()


def _normalize_storage_area_code(value: Optional[str]) -> Optional[str]:
    code = _normalize_storage_code(value)
    return code if STORAGE_CODE_RE.match(code) else None


def _normalize_storage_sub_code(value: Optional[str]) -> Optional[str]:
    code = _normalize_storage_code(value)
    return code if STORAGE_CODE_RE.match(code) else None


def _normalize_storage_sub_location_id(value: Optional[str]) -> Optional[int]:
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


def _normalize_lifecycle_status(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return raw if raw in LIFECYCLE_STATUS_VALUES else "new"


def _lifecycle_status_options(lang: str) -> list[dict]:
    t = t_factory(lang)
    return [
        {
            "value": status,
            "label": t(f"lifecycle_{status}"),
        }
        for status in LIFECYCLE_STATUS_VALUES
    ]


def _storage_path_code(area_code: str, sub_code: str) -> str:
    return f"{area_code}/{sub_code}"


def _storage_location_options(db: Session, project: str, user_id: Optional[int] = None) -> list[dict]:
    resolved_user_id = user_id if user_id is not None else _extract_user_id_from_scope(project)
    location_filters = [StorageSubLocation.project == project, StorageArea.project == project]
    if resolved_user_id is not None:
        location_filters.extend([
            StorageSubLocation.user_id == int(resolved_user_id),
            StorageArea.user_id == int(resolved_user_id),
        ])

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


def _storage_location_map_by_id(db: Session, project: str, ids: list[int], user_id: Optional[int] = None) -> dict[int, str]:
    if not ids:
        return {}
    resolved_user_id = user_id if user_id is not None else _extract_user_id_from_scope(project)
    filters = [
        StorageSubLocation.project == project,
        StorageSubLocation.id.in_(ids),
    ]
    if resolved_user_id is not None:
        filters.append(StorageSubLocation.user_id == int(resolved_user_id))

    rows = (
        db.query(StorageSubLocation.id, StorageSubLocation.path_code)
        .filter(*filters)
        .all()
    )
    return {int(location_id): path_code for location_id, path_code in rows}


def _spool_location_display(spool: Spool, storage_path_map: dict[int, str]) -> str:
    if spool.storage_sub_location_id and spool.storage_sub_location_id in storage_path_map:
        return storage_path_map[spool.storage_sub_location_id]
    return str(spool.location or "").strip() or "-"


def _resolve_storage_sub_location(
    db: Session,
    project: str,
    storage_sub_location_id: Optional[str],
    user_id: Optional[int] = None,
) -> tuple[Optional[StorageSubLocation], Optional[str]]:
    normalized_id = _normalize_storage_sub_location_id(storage_sub_location_id)
    if normalized_id is None:
        return None, None

    resolved_user_id = user_id if user_id is not None else _extract_user_id_from_scope(project)
    filters = [
        StorageSubLocation.project == project,
        StorageSubLocation.id == normalized_id,
    ]
    if resolved_user_id is not None:
        filters.append(StorageSubLocation.user_id == int(resolved_user_id))

    sub_location = (
        db.query(StorageSubLocation)
        .filter(*filters)
        .first()
    )
    if sub_location is None:
        return None, "storage_location_invalid"
    return sub_location, None


def _spool_status_key(spool: Spool) -> str:
    remaining = float(spool.remaining_g or 0.0)
    if remaining <= 0:
        return "empty"
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


def _sqlite_db_path() -> Optional[Path]:
    database = getattr(engine.url, "database", None)
    if not database:
        return None
    return Path(database)


def _is_sqlite_database() -> bool:
    return str(getattr(engine.url, "drivername", "")).startswith("sqlite")


def _is_postgresql_database() -> bool:
    return str(getattr(engine.url, "drivername", "")).startswith("postgresql")


def _backup_mode() -> str:
    if _is_sqlite_database():
        return "sqlite"
    if _is_postgresql_database():
        return "postgresql"
    return "unsupported"


def _pg_tools_available() -> bool:
    return bool(shutil.which("pg_dump") and shutil.which("pg_restore"))


def _postgres_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    password = getattr(engine.url, "password", None)
    if password:
        env["PGPASSWORD"] = str(password)
    return env


def _postgres_connection_args() -> list[str]:
    args: list[str] = []
    host = getattr(engine.url, "host", None)
    port = getattr(engine.url, "port", None)
    username = getattr(engine.url, "username", None)
    database = getattr(engine.url, "database", None)

    if host:
        args.extend(["-h", str(host)])
    if port:
        args.extend(["-p", str(port)])
    if username:
        args.extend(["-U", str(username)])
    if database:
        args.extend(["-d", str(database)])
    return args


def _cleanup_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _build_backup_context(lang: str, **extra) -> dict:
    t = t_factory(lang)
    mode = _backup_mode()
    tools_ok = _pg_tools_available() if mode == "postgresql" else True

    if mode == "sqlite":
        context = {
            "backup_supported": True,
            "backup_notice": None,
            "backup_accept": ".db",
            "backup_hint_text": t("backup_hint_sqlite"),
        }
    elif mode == "postgresql":
        context = {
            "backup_supported": bool(tools_ok),
            "backup_notice": None if tools_ok else t("backup_pg_tools_missing"),
            "backup_accept": ".dump,.backup",
            "backup_hint_text": t("backup_hint_postgres") if tools_ok else t("backup_pg_tools_missing"),
        }
    else:
        context = {
            "backup_supported": False,
            "backup_notice": t("backup_unsupported"),
            "backup_accept": "",
            "backup_hint_text": t("backup_unsupported"),
        }

    context.update(extra)
    return context


def _parse_optional_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _parse_number_list(value: Optional[str]) -> list[float]:
    if value is None:
        return []
    matches = re.findall(r"[-+]?\d+(?:[.,]\d+)?", str(value))
    numbers: list[float] = []
    for token in matches:
        parsed = _parse_optional_float(token)
        if parsed is not None:
            numbers.append(float(parsed))
    return numbers


def _split_hint_values(value: Optional[str]) -> list[str]:
    if value is None:
        return []
    parts = re.split(r"[;,|]+", str(value))
    cleaned: list[str] = []
    seen = set()
    for raw in parts:
        item = raw.strip().strip('"').strip("'").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return cleaned


def _parse_gcode_filament_usage(file_bytes: bytes):
    text = file_bytes.decode("utf-8", errors="ignore")
    metadata: dict[str, str] = {}

    grams_values: list[float] = []
    mm_values: list[float] = []
    material_hints: list[str] = []
    color_hints: list[str] = []
    brand_hints: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(";"):
            line = line[1:].strip()
        if not line:
            continue

        match = re.match(r"^([^:=]{1,120})\s*[:=]\s*(.+)$", line)
        if not match:
            continue

        raw_key = re.sub(r"\s+", " ", match.group(1).strip().lower())
        raw_value = match.group(2).strip()
        if not raw_value:
            continue

        if "filament used [g]" in raw_key or "filament_used_g" in raw_key or "filament used (g)" in raw_key:
            values = _parse_number_list(raw_value)
            if values:
                grams_values.extend(values)
                metadata["filament used [g]"] = ";".join(str(v) for v in values)
            continue

        if "filament used [mm]" in raw_key or "filament_used_mm" in raw_key or "filament used (mm)" in raw_key:
            values = _parse_number_list(raw_value)
            if values:
                mm_values.extend(values)
                metadata["filament used [mm]"] = ";".join(str(v) for v in values)
            continue

        if raw_key in {"filament_type", "filament", "material", "filament_settings_id"}:
            material_hints.extend(_split_hint_values(raw_value))
            continue

        if raw_key in {"filament_colour", "filament_color", "color", "colour"}:
            color_hints.extend(_split_hint_values(raw_value))
            continue

        if raw_key in {"vendor", "filament_vendor", "brand"}:
            brand_hints.extend(_split_hint_values(raw_value))

    total_grams = round(sum(grams_values), 3) if grams_values else None
    total_mm = round(sum(mm_values), 3) if mm_values else None

    def _dedupe(values: list[str]) -> list[str]:
        out: list[str] = []
        seen = set()
        for item in values:
            key = item.lower().strip()
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(item.strip())
        return out

    material_hints = _dedupe(material_hints)
    color_hints = _dedupe(color_hints)
    brand_hints = _dedupe(brand_hints)

    usage_breakdown: list[dict] = []
    if grams_values and len(grams_values) > 1:
        for idx, grams in enumerate(grams_values):
            material = material_hints[idx] if idx < len(material_hints) else None
            usage_breakdown.append({"material": material, "grams": round(float(grams), 3)})
    elif total_grams is not None and material_hints:
        usage_breakdown = [{"material": material, "grams": None} for material in material_hints]

    filament_hints = {
        "materials": material_hints,
        "colors": color_hints,
        "brands": brand_hints,
    }
    return total_grams, total_mm, metadata, filament_hints, usage_breakdown


def _parse_usage_from_print_file(filename: Optional[str], file_bytes: bytes):
    lower_name = str(filename or "").lower()
    suffixes = [suffix.lower() for suffix in Path(lower_name).suffixes]

    if ".3mf" in suffixes:
        grams, millimeters, metadata, filament_hints, usage_breakdown = parse_3mf_filament_usage(file_bytes)
        return grams, millimeters, metadata, filament_hints, usage_breakdown, None

    if any(suffix in {".gcode", ".gco", ".bgcode"} for suffix in suffixes):
        grams, millimeters, metadata, filament_hints, usage_breakdown = _parse_gcode_filament_usage(file_bytes)
        return grams, millimeters, metadata, filament_hints, usage_breakdown, None

    if file_bytes.startswith(b"PK"):
        grams, millimeters, metadata, filament_hints, usage_breakdown = parse_3mf_filament_usage(file_bytes)
        return grams, millimeters, metadata, filament_hints, usage_breakdown, None

    return None, None, {}, {"materials": [], "colors": [], "brands": []}, [], "unsupported_file"


def _matches_any(value: Optional[str], candidates: list[str]) -> bool:
    if not value or not candidates:
        return False
    value_l = value.lower()
    for candidate in candidates:
        c = str(candidate).lower()
        if c and (c in value_l or value_l in c):
            return True
    return False


def _normalize_printer_name(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized[:120] if normalized else None


def _normalize_ams_slot(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = int(float(raw))
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _slot_scoped_spools(spools: list[Spool], slot: int, printer_name: Optional[str]) -> list[Spool]:
    normalized_printer = _normalize_printer_name(printer_name)
    if normalized_printer:
        exact = [
            spool for spool in spools
            if spool.ams_slot == slot and (spool.ams_printer or "").strip() == normalized_printer
        ]
        if exact:
            return exact
        fallback_global = [
            spool for spool in spools
            if spool.ams_slot == slot and not str(spool.ams_printer or "").strip()
        ]
        if fallback_global:
            return fallback_global
        return []

    return [spool for spool in spools if spool.ams_slot == slot]


def _find_ams_slot_conflict(
    db: Session,
    project: str,
    ams_printer: Optional[str],
    ams_slot: Optional[int],
    exclude_spool_id: Optional[int] = None,
) -> Optional[Spool]:
    if ams_slot is None:
        return None

    query = db.query(Spool).filter(Spool.project == project, Spool.ams_slot == ams_slot)
    normalized_printer = _normalize_printer_name(ams_printer)
    if normalized_printer:
        query = query.filter(Spool.ams_printer == normalized_printer)
    else:
        query = query.filter(or_(Spool.ams_printer.is_(None), Spool.ams_printer == ""))

    if exclude_spool_id is not None:
        query = query.filter(Spool.id != exclude_spool_id)

    return query.order_by(Spool.id.asc()).first()


def _parse_slot_tokens(raw: Optional[str]) -> list[int]:
    if raw is None:
        return []

    values: list[int] = []
    for token in re.split(r"[\s,;]+", str(raw).strip()):
        if not token:
            continue
        try:
            value = int(float(token))
        except ValueError:
            continue
        if value <= 0:
            continue
        values.append(value)

    seen: set[int] = set()
    unique_sorted: list[int] = []
    for value in sorted(values):
        if value in seen:
            continue
        seen.add(value)
        unique_sorted.append(value)
    return unique_sorted


def _resolve_ams_slots(raw: Optional[str], usage_breakdown: list[dict]) -> list[int]:
    from_payload = _parse_slot_tokens(raw)
    if from_payload:
        return from_payload

    detected: list[int] = []
    for item in usage_breakdown or []:
        slot = item.get("slot")
        if slot is None:
            continue
        try:
            value = int(float(slot))
        except (TypeError, ValueError):
            continue
        if value > 0:
            detected.append(value)

    seen: set[int] = set()
    unique_sorted: list[int] = []
    for value in sorted(detected):
        if value in seen:
            continue
        seen.add(value)
        unique_sorted.append(value)
    return unique_sorted


def _serialize_ams_slots(slots: list[int]) -> Optional[str]:
    if not slots:
        return None
    return ",".join(str(slot) for slot in slots)


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
) -> tuple[list[dict], dict[str, int]]:
    state_map: dict[tuple[str, int], DeviceSlotState] = {}
    for state in live_states:
        key = ((state.printer_name or "").strip(), int(state.slot or 0))
        if not key[0] or key[1] <= 0:
            continue
        state_map[key] = state

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_seconds = SLOT_STATE_STALE_MINUTES * 60
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

    ordered_spools = sorted(
        mapped_spools,
        key=lambda spool: ((spool.ams_printer or "").strip().lower(), int(spool.ams_slot or 0), int(spool.id or 0)),
    )

    for spool in ordered_spools:
        printer = str(spool.ams_printer or "").strip()
        slot = int(spool.ams_slot or 0)
        state = state_map.get((printer, slot))
        state_label = "missing"

        if state is not None:
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
                else:
                    matches = _same_text(spool.material, state.observed_material) and _same_text(spool.color, state.observed_color)
                    state_label = "ok" if matches else "mismatch"

        summary[state_label] += 1
        rows.append(
            {
                "printer": printer,
                "slot": slot,
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


def _extract_slot_state_entries(payload: object) -> list[dict]:
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

        printer_name = _normalize_printer_name(block.get("printer") or block.get("printer_name"))
        slots = block.get("slots")
        if not printer_name or not isinstance(slots, list):
            continue

        for row in slots:
            if not isinstance(row, dict):
                continue

            slot = _normalize_ams_slot(row.get("slot") or row.get("slot_id"))
            if slot is None:
                continue

            entries.append(
                {
                    "printer_name": printer_name,
                    "slot": slot,
                    "observed_brand": str(row.get("brand") or "").strip()[:120] or None,
                    "observed_material": str(row.get("material") or "").strip()[:80] or None,
                    "observed_color": str(row.get("color") or "").strip()[:80] or None,
                }
            )

    return entries


def _upsert_slot_state_entries(db: Session, project: str, source: str, entries: list[dict]) -> int:
    if not entries:
        return 0

    now = _utcnow().replace(tzinfo=None)
    resolved_user_id = _extract_user_id_from_scope(project)
    updated = 0

    for entry in entries:
        state_filters = [
            DeviceSlotState.project == project,
            DeviceSlotState.printer_name == entry["printer_name"],
            DeviceSlotState.slot == entry["slot"],
        ]
        if resolved_user_id is not None:
            state_filters.append(DeviceSlotState.user_id == int(resolved_user_id))

        state = (
            db.query(DeviceSlotState)
            .filter(*state_filters)
            .first()
        )
        if state is None:
            state = DeviceSlotState(
                user_id=_extract_user_id_from_scope(project),
                project=project,
                printer_name=entry["printer_name"],
                slot=entry["slot"],
            )
            db.add(state)
        elif state.user_id is None:
            state.user_id = _extract_user_id_from_scope(project)

        state.observed_brand = entry.get("observed_brand")
        state.observed_material = entry.get("observed_material")
        state.observed_color = entry.get("observed_color")
        state.source = source
        state.observed_at = now
        state.updated_at = now
        updated += 1

    return updated


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
    current_user = get_current_user(request)
    project_scope = get_project(request)
    project = _extract_base_project_from_scope(project_scope)

    response = templates.TemplateResponse(
        request,
        template,
        {
            "lang": lang,
            "theme": theme,
            "project": project,
            "project_options": PROJECT_OPTIONS,
            "is_authenticated": bool(current_user),
            "current_user_email": str(current_user.email) if current_user else None,
            "current_user_name": str(current_user.display_name) if current_user and current_user.display_name else None,
            "t": t_factory(lang),
            "lang_url_de": lang_url_de,
            "lang_url_en": lang_url_en,
            "lang_url_de_settings": lang_url_de_settings,
            "lang_url_en_settings": lang_url_en_settings,
            "settings_return_url": settings_return_url,
            **context,
        },
    )
    _set_cookie(response, "lang", lang)
    if not request.cookies.get("theme"):
        _set_cookie(response, "theme", theme)
    if not request.cookies.get("project"):
        _set_cookie(response, "project", project)
    return response


@app.post("/settings")
def save_settings(
    request: Request,
    lang: Optional[str] = Form(None),
    theme: Optional[str] = Form(None),
    project: Optional[str] = Form(None),
    persist_db: Optional[str] = Form("1"),
    next_url: Optional[str] = Form("/"),
):
    normalized_lang = lang if lang in TRANSLATIONS else None
    normalized_theme = theme if theme in VALID_THEMES else None
    normalized_project = _normalize_project(project) if project is not None else None
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

    if not normalized_lang and not normalized_theme and not normalized_project:
        _set_cookie(response, "lang", get_lang(request))
        _set_cookie(response, "theme", get_theme(request))
        _set_cookie(response, "project", get_project(request))

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
    location_id: Optional[int] = None,
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
    user_id = _current_user_id(request)
    t = t_factory(lang)
    notice_key = str(request.query_params.get("notice") or "").strip()
    presets = load_presets()
    material_thresholds = _load_material_thresholds(presets)
    sort_key = (sort or "spool_index").strip().lower()
    sort_dir = "desc" if (dir or "desc").strip().lower() == "desc" else "asc"
    page_size_options = [10, 25, 50, 100]
    page_size = page_size if page_size in page_size_options else 25
    page = max(1, int(page or 1))

    spool_scope_filters = _model_scope_filters(Spool, project, user_id)
    usage_scope_filters = _model_scope_filters(UsageHistory, project, user_id)

    query = db.query(Spool).filter(*spool_scope_filters)
    normalized_location_id = int(location_id) if location_id and int(location_id) > 0 else None
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
        query = query.filter(Spool.remaining_g > 0)

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
        user_id,
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
            "show_stats": show_stats,
            "show_spool_list": show_spool_list,
            "list_base_path": "/spools",
            "message": t(notice_key) if notice_key in {"qr_scan_location_loaded"} else None,
            "q": q,
            "location_id": normalized_location_id,
            "lifecycle_status": normalized_lifecycle_status,
            "lifecycle_status_options": _lifecycle_status_options(lang),
            "storage_location_options": _storage_location_options(db, project, user_id),
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


@app.get("/")
def landing_page(request: Request):
    lang = get_lang(request)
    return render(
        request,
        "landing.html",
        {
            "title": t_factory(lang)("landing_title"),
        },
        lang,
    )


@app.get("/landing")
def landing_page_alias(request: Request):
    return landing_page(request)


@app.get("/dashboard")
def index(
    request: Request,
    q: Optional[str] = None,
    location_id: Optional[int] = None,
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


@app.get("/auth/login")
def auth_login_page(request: Request, next_url: Optional[str] = None):
    lang = get_lang(request)
    if get_current_user(request) is not None:
        return RedirectResponse(_normalize_next_url(next_url or "/dashboard"), status_code=303)
    return render(
        request,
        "auth_login.html",
        {
            "title": t_factory(lang)("login_title"),
            "next_url": _normalize_next_url(next_url or "/dashboard"),
        },
        lang,
    )


@app.get("/auth/register")
def auth_register_page(request: Request, next_url: Optional[str] = None):
    lang = get_lang(request)
    if get_current_user(request) is not None:
        return RedirectResponse(_normalize_next_url(next_url or "/dashboard"), status_code=303)
    return render(
        request,
        "auth_register.html",
        {
            "title": t_factory(lang)("register_title"),
            "next_url": _normalize_next_url(next_url or "/dashboard"),
        },
        lang,
    )


@app.post("/auth/login")
def auth_login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    normalized_email = _normalize_email(email)
    plain_password = str(password or "")
    user = db.query(User).filter(User.email == normalized_email, User.is_active.is_(True)).first()

    if user is None or not _verify_password(plain_password, user.password_hash):
        return render(
            request,
            "auth_login.html",
            {
                "title": t("login_title"),
                "error": t("auth_invalid_credentials"),
                "prefill_email": normalized_email,
                "next_url": _normalize_next_url(next_url or "/dashboard"),
            },
            lang,
        )

    session_token = secrets.token_urlsafe(48)
    session = UserSession(
        user_id=user.id,
        token_hash=_hash_secret_value(session_token),
        user_agent=str(request.headers.get("user-agent") or "")[:255] or None,
        ip_address=str(request.client.host if request.client else "")[:120] or None,
        expires_at=_utcnow().replace(tzinfo=None) + timedelta(days=AUTH_SESSION_DAYS),
    )
    db.add(session)
    db.commit()

    response = RedirectResponse(_normalize_next_url(next_url or "/dashboard"), status_code=303)
    _set_cookie(response, AUTH_SESSION_COOKIE, session_token, max_age=AUTH_SESSION_MAX_AGE)
    return response


@app.post("/auth/register")
def auth_register_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    next_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    t = t_factory(lang)
    normalized_email = _normalize_email(email)
    display_name = str(name or "").strip() or None
    plain_password = str(password or "")

    if len(plain_password) < 8:
        return render(
            request,
            "auth_register.html",
            {
                "title": t("register_title"),
                "error": t("auth_password_too_short"),
                "prefill_name": display_name,
                "prefill_email": normalized_email,
                "next_url": _normalize_next_url(next_url or "/dashboard"),
            },
            lang,
        )

    existing = db.query(User).filter(User.email == normalized_email).first()
    if existing is not None:
        return render(
            request,
            "auth_register.html",
            {
                "title": t("register_title"),
                "error": t("auth_email_exists"),
                "prefill_name": display_name,
                "prefill_email": normalized_email,
                "next_url": _normalize_next_url(next_url or "/dashboard"),
            },
            lang,
        )

    user = User(
        email=normalized_email,
        display_name=display_name,
        password_hash=_hash_password(plain_password),
        is_active=True,
    )
    db.add(user)
    db.flush()

    session_token = secrets.token_urlsafe(48)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=_hash_secret_value(session_token),
            user_agent=str(request.headers.get("user-agent") or "")[:255] or None,
            ip_address=str(request.client.host if request.client else "")[:120] or None,
            expires_at=_utcnow().replace(tzinfo=None) + timedelta(days=AUTH_SESSION_DAYS),
        )
    )
    db.commit()

    response = RedirectResponse(_normalize_next_url(next_url or "/dashboard"), status_code=303)
    _set_cookie(response, AUTH_SESSION_COOKIE, session_token, max_age=AUTH_SESSION_MAX_AGE)
    return response


@app.post("/auth/logout")
def auth_logout(request: Request, next_url: Optional[str] = Form("/"), db: Session = Depends(get_db)):
    token = str(request.cookies.get(AUTH_SESSION_COOKIE) or "").strip()
    if token:
        token_hash = _hash_secret_value(token)
        session_row = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
        if session_row is not None:
            db.delete(session_row)
            db.commit()

    response = RedirectResponse(_normalize_next_url(next_url or "/"), status_code=303)
    response.delete_cookie(AUTH_SESSION_COOKIE)
    return response


@app.get("/spools")
def spool_list_page(
    request: Request,
    q: Optional[str] = None,
    location_id: Optional[int] = None,
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
def analysis(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)

    def grouped(column):
        rows = (
            db.query(
                column.label("name"),
                func.count(Spool.id).label("count"),
                func.sum(Spool.weight_g).label("weight_g"),
                func.sum(Spool.remaining_g).label("remaining_g"),
                func.sum(Spool.price).label("value"),
            )
            .filter(Spool.project == project)
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

    total_remaining = db.query(func.sum(Spool.remaining_g)).filter(Spool.project == project).scalar() or 0

    grouped_data = {
        "brand": grouped(Spool.brand),
        "material": grouped(Spool.material),
        "color": grouped(Spool.color),
        "location": grouped(Spool.location),
    }

    for key in grouped_data:
        for row in grouped_data[key]:
            row["share_pct"] = round((row["remaining_g"] / total_remaining * 100), 1) if total_remaining else 0.0

    return render(
        request,
        "analysis.html",
        {
            "groups": grouped_data,
            "total_remaining": round(float(total_remaining), 1),
        },
        lang,
    )


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

    slot_rows, slot_summary = _build_slot_status_rows(mapped_spools, live_states)

    return render(
        request,
        "slot_status.html",
        {
            "slot_rows": slot_rows,
            "slot_summary": slot_summary,
            "has_live_data": len(live_states) > 0,
            "stale_minutes": SLOT_STATE_STALE_MINUTES,
        },
        lang,
    )


def _help_topics(lang: str) -> list[dict[str, str]]:
    is_de = lang == "de"
    return [
        {
            "slug": "inventory",
            "path": "/help/inventory",
            "title": "Inventar & Spulenpflege" if is_de else "Inventory & spool maintenance",
            "summary": "Spulen erfassen, bearbeiten und sauber strukturieren." if is_de else "Create, edit, and organize spools cleanly.",
            "image": "/static/help/screenshots/functions/spools_list_hd.png",
        },
        {
            "slug": "booking",
            "path": "/help/booking",
            "title": "Buchung & Auto-Verbrauch" if is_de else "Booking & automatic usage",
            "summary": "Manuelle Buchung und Slicer-Upload korrekt nutzen." if is_de else "Use manual booking and slicer uploads correctly.",
            "image": "/static/help/screenshots/functions/booking_hd.png",
        },
        {
            "slug": "slot-status",
            "path": "/help/slot-status",
            "title": "AMS Slotstatus & Mapping" if is_de else "AMS slot status & mapping",
            "summary": "Soll/Ist-Status lesen und Slotkonflikte vermeiden." if is_de else "Read expected/live state and avoid slot conflicts.",
            "image": "/static/help/screenshots/functions/slot_status_hd.png",
        },
        {
            "slug": "labels-qr",
            "path": "/help/labels-qr",
            "title": "Labels & QR-Workflow" if is_de else "Labels & QR workflow",
            "summary": "Etikettendruck und Scan-Prozess im Alltag." if is_de else "Daily label printing and scan workflow.",
            "image": "/static/help/screenshots/functions/labels_hd.png",
        },
        {
            "slug": "backup",
            "path": "/help/backup",
            "title": "Backup, Restore & Betrieb" if is_de else "Backup, restore & operations",
            "summary": "Sicherer Betrieb auf externem Server (Hostinger VPS)." if is_de else "Safe operations on an external server (Hostinger VPS).",
            "image": "/static/help/screenshots/functions/backup_hd.png",
        },
    ]


@app.get("/help")
def help_index_page(request: Request):
    lang = get_lang(request)
    topics = _help_topics(lang)
    return render(
        request,
        "help_index.html",
        {
            "help_topics": topics,
        },
        lang,
    )


@app.get("/help/{topic}")
def help_topic_page(request: Request, topic: str):
    lang = get_lang(request)
    template_map = {
        "inventory": "help_inventory.html",
        "booking": "help_booking.html",
        "slot-status": "help_slot_status.html",
        "labels-qr": "help_labels_qr.html",
        "backup": "help_backup.html",
    }
    template = template_map.get(str(topic or "").strip().lower())
    if template is None:
        return RedirectResponse("/help", status_code=303)

    topics = _help_topics(lang)
    return render(
        request,
        template,
        {
            "help_topics": topics,
            "current_help_topic": str(topic or "").strip().lower(),
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

    reorder_rows = [row for row in material_total_rows if row["needs_reorder"]]

    return render(
        request,
        "thresholds.html",
        {
            "material_rows": material_rows,
            "spool_rows": spool_rows,
            "spool_threshold_rows": spool_threshold_rows,
            "material_total_rows": material_total_rows,
            "reorder_rows": reorder_rows,
            "materials": sorted(presets.get("materials", []), key=lambda x: str(x).lower()),
            "material_groups": presets.get("material_groups", []),
            "brands": sorted(presets.get("brands", []), key=lambda x: str(x).lower()),
            "colors": sorted(presets.get("colors", []), key=lambda x: str(x).lower()),
            "color_map": load_color_map(),
        },
        lang,
    )


@app.post("/thresholds/spool")
def set_spool_threshold(
    request: Request,
    spool_id: int = Form(...),
    threshold_g: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        parsed = _parse_optional_float(threshold_g)
        spool.low_stock_threshold_g = None if parsed is None or parsed < 0 else round(float(parsed), 3)
        spool.updated_at = _utcnow()
        db.commit()
    return RedirectResponse("/thresholds", status_code=303)


@app.post("/thresholds/spool/delete")
def delete_spool_threshold(
    request: Request,
    spool_id: int = Form(...),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        spool.low_stock_threshold_g = None
        spool.updated_at = _utcnow()
        db.commit()
    return RedirectResponse("/thresholds", status_code=303)


@app.post("/thresholds/material-default")
def set_material_default_threshold(
    material: str = Form(...),
    threshold_g: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("low_stock_thresholds", {})
    key = material.strip()
    if not key:
        return RedirectResponse("/thresholds", status_code=303)

    parsed = _parse_optional_float(threshold_g)
    if parsed is None or parsed < 0:
        thresholds.pop(key, None)
    else:
        thresholds[key] = round(float(parsed), 3)

    save_presets(presets)
    return RedirectResponse("/thresholds", status_code=303)


@app.post("/thresholds/material-default/delete")
def delete_material_default_threshold(material: str = Form(...)):
    presets = load_presets()
    thresholds = presets.setdefault("low_stock_thresholds", {})
    key = material.strip()
    if key:
        thresholds.pop(key, None)
        save_presets(presets)
    return RedirectResponse("/thresholds", status_code=303)


def _render_storage_locations_page(
    request: Request,
    db: Session,
    lang: str,
    message: Optional[str] = None,
    error: Optional[str] = None,
    form_data: Optional[dict] = None,
):
    project = get_project(request)
    user_id = _current_user_id(request)
    spool_scope_filters = _model_scope_filters(Spool, project, user_id)
    usage_rows = (
        db.query(Spool.storage_sub_location_id, func.count(Spool.id).label("count"))
        .filter(*spool_scope_filters, Spool.storage_sub_location_id.is_not(None))
        .group_by(Spool.storage_sub_location_id)
        .all()
    )
    usage_map = {int(location_id): int(count) for location_id, count in usage_rows if location_id}
    locations = _storage_location_options(db, project, user_id)
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
    user_id = _current_user_id(request)

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
            user_id=user_id,
            project=project,
            code=normalized_area_code,
            name=str(area_name or "").strip() or None,
        )
        db.add(area)
        db.flush()
    elif area_name is not None and str(area_name).strip():
        area.name = str(area_name).strip()
        area.updated_at = _utcnow()
        if area.user_id is None:
            area.user_id = user_id
    elif area.user_id is None:
        area.user_id = user_id

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
        user_id=user_id,
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
    user_id = _current_user_id(request)
    t = t_factory(lang)
    normalized_lifecycle_status = _normalize_lifecycle_status(lifecycle_status)
    normalized_ams_printer = _normalize_printer_name(ams_printer)
    normalized_ams_slot = _normalize_ams_slot(ams_slot)
    storage_sub_location, storage_error_key = _resolve_storage_sub_location(
        db,
        project,
        storage_sub_location_id,
        user_id,
    )

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
        user_id=user_id,
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
    db.add(spool)
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
    price: list[Optional[float]] = Form([]),
    location: list[Optional[str]] = Form([]),
    storage_sub_location_id: list[Optional[str]] = Form([]),
    quantity: list[int] = Form([]),
    next_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    user_id = _current_user_id(request)
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

        for _ in range(qty):
            spool = Spool(
                user_id=user_id,
                brand=brand[i],
                material=material[i],
                color=color[i],
                weight_g=float(weight_g[i]),
                remaining_g=float(remaining_g[i]),
                price=float(price[i]) if i < len(price) and price[i] not in (None, "") else None,
                location=normalized_location_value,
                storage_sub_location_id=resolved_storage.id if resolved_storage else None,
                project=project,
            )
            db.add(spool)
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
    material: str = Form(...),
    color: Optional[str] = Form(None),
    threshold_g: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("material_total_thresholds", {})
    material_key = material.strip()
    if not material_key:
        return RedirectResponse("/thresholds", status_code=303)

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
    return RedirectResponse("/thresholds", status_code=303)


@app.post("/thresholds/material-total/delete")
def delete_material_total_threshold(
    material: str = Form(...),
    color: Optional[str] = Form(None),
):
    presets = load_presets()
    thresholds = presets.setdefault("material_total_thresholds", {})
    material_key = material.strip()
    if not material_key:
        return RedirectResponse("/thresholds", status_code=303)

    color_key = (color or "").strip() or "*"
    thresholds.pop(_material_color_key(material_key, color_key), None)
    if color_key == "*":
        thresholds.pop(material_key, None)
    save_presets(presets)
    return RedirectResponse("/thresholds", status_code=303)


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
    location: Optional[str] = Form(None),
    storage_sub_location_id: Optional[str] = Form(None),
    lifecycle_status: Optional[str] = Form(None),
    ams_printer: Optional[str] = Form(None),
    ams_slot: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    t = t_factory(lang)
    spool = db.query(Spool).filter(Spool.id == spool_id, Spool.project == project).first()
    if spool:
        normalized_lifecycle_status = _normalize_lifecycle_status(lifecycle_status)
        normalized_ams_printer = _normalize_printer_name(ams_printer)
        normalized_ams_slot = _normalize_ams_slot(ams_slot)
        storage_sub_location, storage_error_key = _resolve_storage_sub_location(
            db,
            project,
            storage_sub_location_id,
            _current_user_id(request),
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

        conflict = _find_ams_slot_conflict(
            db,
            project=project,
            ams_printer=normalized_ams_printer,
            ams_slot=normalized_ams_slot,
            exclude_spool_id=spool.id,
        )
        if conflict is not None:
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
                    "title": t_factory(lang)("edit"),
                    "spool": spool_data,
                    "presets": presets,
                    "lifecycle_status_options": _lifecycle_status_options(lang),
                    "storage_location_options": _storage_location_options(db, project),
                    "error": t_factory(lang)("ams_slot_conflict"),
                    "next_url": _normalize_next_url(request.query_params.get("next_url") or "/spools"),
                },
                lang,
            )

        location_value = str(location or "").strip() or None
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
        spool.ams_printer = normalized_ams_printer
        spool.ams_slot = normalized_ams_slot
        spool.updated_at = _utcnow()
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
        spool.updated_at = _utcnow()
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


@app.get("/qr-scan")
def qr_scan_page(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    t = t_factory(lang)
    notice_key = str(request.query_params.get("notice") or "").strip()
    notice_message = t(notice_key) if notice_key in {"qr_scan_next_ready", "qr_scan_location_loaded"} else None
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

    return render(
        request,
        "qr_scan_manage.html",
        {
            "spool": spool,
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

    action_key = str(action or "").strip().lower()
    message_key: Optional[str] = None
    if action_key == "set_empty":
        spool.remaining_g = 0.0
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
            return render(
                request,
                "qr_scan_manage.html",
                {
                    "spool": spool,
                    "spool_status_key": _spool_status_key(spool),
                    "lifecycle_status_options": _lifecycle_status_options(lang),
                    "error": t("qr_scan_action_invalid_lifecycle"),
                },
                lang,
            )
        spool.lifecycle_status = lifecycle_candidate
        message_key = "qr_scan_action_done_lifecycle"
    else:
        return render(
            request,
            "qr_scan_manage.html",
            {
                "spool": spool,
                "spool_status_key": _spool_status_key(spool),
                "lifecycle_status_options": _lifecycle_status_options(lang),
                "error": t("qr_scan_action_invalid"),
            },
            lang,
        )

    spool.updated_at = _utcnow()
    db.commit()
    db.refresh(spool)

    if _is_truthy(return_to_scan):
        return RedirectResponse(f"/qr-scan?{urlencode({'notice': 'qr_scan_next_ready'})}", status_code=303)

    return render(
        request,
        "qr_scan_manage.html",
        {
            "spool": spool,
            "spool_status_key": _spool_status_key(spool),
            "lifecycle_status_options": _lifecycle_status_options(lang),
            "message": t(message_key) if message_key else None,
        },
        lang,
    )


@app.get("/labels")
def labels_form(request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    project = get_project(request)
    spools = (
        db.query(Spool)
        .filter(Spool.project == project)
        .order_by(Spool.id.asc())
        .all()
    )
    layouts_map = _all_label_layouts()
    prefs = _load_label_print_preferences(request)
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
            "print_mode": prefs["print_mode"],
            "label_orientation": prefs["label_orientation"],
            "label_content": prefs["label_content"],
            "layout_choices": _get_label_layout_choices(lang, layouts_map),
            "custom_layouts": [item for item in _get_label_layout_choices(lang, layouts_map) if item.get("is_custom")],
        },
        lang,
    )


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
        save_presets(presets)
        _save_setting_to_db(
            f"{CUSTOM_LABEL_LAYOUT_SETTING_PREFIX}{layout_key}",
            json.dumps(layout_payload, ensure_ascii=False),
        )

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
            "layout_choices": layout_choices,
            "custom_layouts": [item for item in layout_choices if item.get("is_custom")],
            "message": t("label_custom_saved") if not error_key else None,
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
    layout: str = Form(DEFAULT_LABEL_LAYOUT),
    print_mode: str = Form(DEFAULT_LABEL_PRINT_MODE),
    label_orientation: str = Form(DEFAULT_LABEL_ORIENTATION),
    show_spool_id: Optional[str] = Form(None),
    show_brand: Optional[str] = Form(None),
    show_material_color: Optional[str] = Form(None),
    show_weight: Optional[str] = Form(None),
    show_remaining: Optional[str] = Form(None),
    show_location: Optional[str] = Form(None),
    save_defaults: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    lang = get_lang(request)
    project = get_project(request)
    selected_ids = [int(value) for value in spool_ids if value]
    layouts_map = _all_label_layouts()
    valid_layout = _normalize_label_layout(layout, layouts_map)
    valid_print_mode = _normalize_label_print_mode(print_mode)
    valid_label_orientation = _normalize_label_orientation(label_orientation)
    normalized_label_target = "location" if str(label_target or "").strip().lower() == "location" else "spool"
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
                "label_target": normalized_label_target,
                "selected_ids": [],
                "selected_location_ids": selected_location_ids,
                "layout": valid_layout,
                "print_mode": valid_print_mode,
                "label_orientation": valid_label_orientation,
                "label_content": label_content,
                "layout_choices": _get_label_layout_choices(lang, layouts_map),
                "custom_layouts": [item for item in _get_label_layout_choices(lang, layouts_map) if item.get("is_custom")],
                "error": t_factory(lang)("label_none_selected"),
                "message": t_factory(lang)("label_defaults_saved") if _is_truthy(save_defaults) else None,
            },
            lang,
        )
        if _is_truthy(save_defaults):
            _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
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
                "label_target": normalized_label_target,
                "selected_ids": selected_ids,
                "selected_location_ids": [],
                "layout": valid_layout,
                "print_mode": valid_print_mode,
                "label_orientation": valid_label_orientation,
                "label_content": label_content,
                "layout_choices": _get_label_layout_choices(lang, layouts_map),
                "custom_layouts": [item for item in _get_label_layout_choices(lang, layouts_map) if item.get("is_custom")],
                "error": t_factory(lang)("label_location_none_selected"),
                "message": t_factory(lang)("label_defaults_saved") if _is_truthy(save_defaults) else None,
            },
            lang,
        )
        if _is_truthy(save_defaults):
            _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
        return response

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

    response = render(
        request,
        "labels_print.html",
        {
            "label_items": label_items,
            "label_target": normalized_label_target,
            "layout": valid_layout,
            "print_mode": valid_print_mode,
            "label_orientation": valid_label_orientation,
            "label_content": label_content,
            "layout_config": _resolve_label_layout_for_print(layouts_map[valid_layout]),
        },
        lang,
    )
    if _is_truthy(save_defaults):
        _save_label_print_preferences(response, valid_print_mode, valid_label_orientation, label_content)
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
    user_id = _current_user_id(request)
    t = t_factory(lang)
    spool_scope_filters = _model_scope_filters(Spool, project, user_id)
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
    user_id = _current_user_id(request)
    t = t_factory(lang)
    history_scope_filters = _model_scope_filters(UsageHistory, project, user_id)
    batch_scope_filters = _model_scope_filters(UsageBatchContext, project, user_id)

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
    user_id = _current_user_id(request)
    spool_scope_filters = _model_scope_filters(Spool, project, user_id)
    history_scope_filters = _model_scope_filters(UsageHistory, project, user_id)
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

                if slot_required is not None:
                    targets = _slot_scoped_spools(selected_spools, slot_required, None)
                    if not targets:
                        return []
                else:
                    candidate_pool = selected_spools
                    support_spools = [s for s in selected_spools if is_support_spool(s)]
                    model_spools = [s for s in selected_spools if not is_support_spool(s)]

                    if support_required and support_spools:
                        candidate_pool = support_spools
                    elif (not support_required) and model_spools:
                        candidate_pool = model_spools

                    if support_required and not support_spools:
                        return []

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
            spool.updated_at = _utcnow()
            changed += 1

            history_rows.append(
                UsageHistory(
                    user_id=user_id,
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
    current_user_id = _current_user_id(request)
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

    spool_scope_filters = _model_scope_filters(Spool, effective_project, current_user_id)
    usage_scope_filters = _model_scope_filters(UsageHistory, effective_project, current_user_id)
    batch_scope_filters = _model_scope_filters(UsageBatchContext, effective_project, current_user_id)

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

                if slot_required is not None:
                    targets = _slot_scoped_spools(selected_spools, slot_required, printer_name)
                    if not targets:
                        return []
                else:
                    candidate_pool = selected_spools
                    support_spools = [s for s in selected_spools if is_support_spool(s)]
                    model_spools = [s for s in selected_spools if not is_support_spool(s)]

                    if support_required and support_spools:
                        candidate_pool = support_spools
                    elif (not support_required) and model_spools:
                        candidate_pool = model_spools

                    if support_required and not support_spools:
                        return []

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
        spool.updated_at = now
        changed += 1

        history_rows.append(
            UsageHistory(
                user_id=current_user_id,
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
                        user_id=current_user_id,
                        project=effective_project,
                        batch_id=batch_id,
                        printer_name=printer_name,
                        ams_slots=serialized_ams_slots,
                    )
                )
            else:
                if printer_name and not existing_context.printer_name:
                    existing_context.printer_name = printer_name
                if serialized_ams_slots and not existing_context.ams_slots:
                    existing_context.ams_slots = serialized_ams_slots
        db.add_all(history_rows)
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
    db.commit()

    return {
        "ok": True,
        "project": effective_project,
        "source": source_value,
        "entries": len(entries),
        "updated": updated,
    }


@app.get("/import")
def import_form(request: Request):
    lang = get_lang(request)
    return render(request, "import.html", {}, lang)


@app.get("/backup")
def backup_page(request: Request):
    lang = get_lang(request)
    context = _build_backup_context(lang)
    return render(request, "backup.html", context, lang)


@app.get("/backup/export")
def backup_export(request: Request):
    lang = get_lang(request)
    t = t_factory(lang)

    mode = _backup_mode()
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

    return render(
        request,
        "backup.html",
        _build_backup_context(lang, message=t("backup_import_done")),
        lang,
    )


@app.post("/import")
def import_data(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    project = get_project(request)
    user_id = _current_user_id(request)

    import pandas as pd

    content, too_large = _read_upload_limited(file)
    if too_large:
        lang = get_lang(request)
        t = t_factory(lang)
        return render(request, "import.html", {"error": t("upload_too_large").format(max_mb=MAX_UPLOAD_MB)}, lang)
    if content is None:
        return RedirectResponse("/import", status_code=303)

    name = (file.filename or "").lower()
    if name.endswith(".csv"):
        df = pd.read_csv(BytesIO(content))
    elif name.endswith(".xlsx"):
        df = pd.read_excel(BytesIO(content))
    else:
        return RedirectResponse("/import", status_code=303)

    column_map = {
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

    df = df.rename(columns={c: column_map.get(c.strip().lower(), c) for c in df.columns})

    for _, row in df.iterrows():
        spool = Spool(
            user_id=user_id,
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
        if spool.brand and spool.material and spool.color:
            db.add(spool)
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
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=filament_spools.xlsx"},
    )
