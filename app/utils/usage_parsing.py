from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .three_mf import parse_3mf_filament_usage


def parse_optional_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def parse_optional_bool(value: object) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if float(value) == 1:
            return True
        if float(value) == 0:
            return False
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on", "active"}:
        return True
    if normalized in {"0", "false", "no", "off", "inactive"}:
        return False
    return None


def parse_number_list(value: Optional[str]) -> list[float]:
    if value is None:
        return []
    matches = re.findall(r"[-+]?\d+(?:[.,]\d+)?", str(value))
    numbers: list[float] = []
    for token in matches:
        parsed = parse_optional_float(token)
        if parsed is not None:
            numbers.append(float(parsed))
    return numbers


def split_hint_values(value: Optional[str]) -> list[str]:
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


def parse_gcode_filament_usage(file_bytes: bytes):
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
            values = parse_number_list(raw_value)
            if values:
                grams_values.extend(values)
                metadata["filament used [g]"] = ";".join(str(v) for v in values)
            continue

        if "filament used [mm]" in raw_key or "filament_used_mm" in raw_key or "filament used (mm)" in raw_key:
            values = parse_number_list(raw_value)
            if values:
                mm_values.extend(values)
                metadata["filament used [mm]"] = ";".join(str(v) for v in values)
            continue

        if raw_key in {"filament_type", "filament", "material", "filament_settings_id"}:
            material_hints.extend(split_hint_values(raw_value))
            continue

        if raw_key in {"filament_colour", "filament_color", "color", "colour"}:
            color_hints.extend(split_hint_values(raw_value))
            continue

        if raw_key in {"vendor", "filament_vendor", "brand"}:
            brand_hints.extend(split_hint_values(raw_value))

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


def parse_usage_from_print_file(filename: Optional[str], file_bytes: bytes):
    lower_name = str(filename or "").lower()
    suffixes = [suffix.lower() for suffix in Path(lower_name).suffixes]

    if ".3mf" in suffixes:
        grams, millimeters, metadata, filament_hints, usage_breakdown = parse_3mf_filament_usage(file_bytes)
        return grams, millimeters, metadata, filament_hints, usage_breakdown, None

    if any(suffix in {".gcode", ".gco", ".bgcode"} for suffix in suffixes):
        grams, millimeters, metadata, filament_hints, usage_breakdown = parse_gcode_filament_usage(file_bytes)
        return grams, millimeters, metadata, filament_hints, usage_breakdown, None

    if file_bytes.startswith(b"PK"):
        grams, millimeters, metadata, filament_hints, usage_breakdown = parse_3mf_filament_usage(file_bytes)
        return grams, millimeters, metadata, filament_hints, usage_breakdown, None

    return None, None, {}, {"materials": [], "colors": [], "brands": []}, [], "unsupported_file"


def matches_any(value: Optional[str], candidates: list[str]) -> bool:
    if not value or not candidates:
        return False
    value_l = value.lower()
    for candidate in candidates:
        c = str(candidate).lower()
        if c and (c in value_l or value_l in c):
            return True
    return False
