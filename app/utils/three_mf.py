import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET

METADATA_KEYS_G = [
    "filament used [g]",
    "filament_used_g",
    "filament used (g)",
    "filament used",
    "filament_used",
]

METADATA_KEYS_MM = [
    "filament used [mm]",
    "filament_used_mm",
    "filament used (mm)",
]

NUMBER_RE = re.compile(r"([-+]?\d+(?:[.,]\d+)?)")
TEXT_EXTENSIONS = (
    ".gcode",
    ".xml",
    ".json",
    ".cfg",
    ".ini",
    ".txt",
    ".config",
)

SAFE_HINT_RE = re.compile(r"^[\w\s\-+./#()]+$", flags=re.UNICODE)

GRAM_KEYWORDS = (
    "filament_used_g",
    "filamentusedg",
    "filament_weight_g",
    "filamentusageg",
    "material_usage_g",
    "material_used_g",
    "used_filament_g",
    "total_filament_g",
    "total_material_g",
)

MM_KEYWORDS = (
    "filament_used_mm",
    "filamentusedmm",
    "filament_length_mm",
    "used_filament_mm",
    "total_filament_mm",
)

TEXT_KEY_ALIASES = {
    "filament used [g]": "filament used [g]",
    "filament used (g)": "filament used [g]",
    "total filament weight [g]": "filament used [g]",
    "total filament weight (g)": "filament used [g]",
    "filament used [mm]": "filament used [mm]",
    "filament used (mm)": "filament used [mm]",
    "total filament length [mm]": "filament used [mm]",
    "total filament length (mm)": "filament used [mm]",
    "filament_type": "filament_type",
    "filament": "filament",
    "filament_is_support": "filament_is_support",
    "filament_cost": "filament_cost",
    "filament_colour": "filament_colour",
    "filament_color": "filament_colour",
    "filament_vendor": "vendor",
    "filament_settings_id": "filament_settings_id",
    "vendor": "vendor",
}


def _clean_scalar(value: str) -> str:
    text = (value or "").strip().strip('"').strip("'").strip()
    return text


def _looks_like_gcode_or_template(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    if len(text) > 120:
        return True
    if "{" in text or "}" in text:
        return True
    if "[" in text and "]" in text:
        return True
    upper = text.upper()
    if "_START" in upper or "_END" in upper:
        return True
    if text.startswith("-"):
        return True
    if re.match(r"^[MGV]\d", upper):
        return True
    return False


def _is_plausible_hint_token(value: str) -> bool:
    token = _clean_scalar(value)
    if not token:
        return False
    if _looks_like_gcode_or_template(token):
        return False
    if not SAFE_HINT_RE.match(token):
        return False
    if not any(ch.isalpha() for ch in token):
        return False
    return True


def _filter_hint_tokens(values: list[str]) -> list[str]:
    return [v for v in values if _is_plausible_hint_token(v)]


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (key or "").strip().lower()).strip("_")


def _flatten_json_values(obj, parent_key: str = ""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_key = f"{parent_key}.{key}" if parent_key else str(key)
            yield from _flatten_json_values(value, next_key)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            next_key = f"{parent_key}[{index}]"
            yield from _flatten_json_values(value, next_key)
    else:
        yield parent_key, obj


def _json_scalar_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def _extract_numbers_from_json_scalar(value) -> list[float]:
    text = _json_scalar_to_text(value)
    if not text:
        return []
    return _parse_number_list(text)


def _collect_from_json_blob(obj, metadata: dict, grams_candidates: list[float], mm_candidates: list[float]):
    try:
        if isinstance(obj, dict) and "filament_ids" in obj:
            filament_ids = obj.get("filament_ids")
            if isinstance(filament_ids, list) and len(filament_ids) == 0:
                metadata.setdefault("__bambu_unsliced__", "1")
    except Exception:
        pass

    for raw_key, raw_value in _flatten_json_values(obj):
        normalized = _normalize_key(raw_key)
        if not normalized:
            continue

        scalar_text = _json_scalar_to_text(raw_value)
        if not scalar_text:
            continue

        if any(keyword in normalized for keyword in GRAM_KEYWORDS):
            numbers = _extract_numbers_from_json_scalar(raw_value)
            if numbers:
                grams_candidates.extend(numbers)
                metadata.setdefault("filament used [g]", ";".join(str(n) for n in numbers))
            continue

        if any(keyword in normalized for keyword in MM_KEYWORDS):
            numbers = _extract_numbers_from_json_scalar(raw_value)
            if numbers:
                mm_candidates.extend(numbers)
                metadata.setdefault("filament used [mm]", ";".join(str(n) for n in numbers))
            continue

        if "filament_type" in normalized or normalized.endswith("material"):
            material_tokens = _filter_hint_tokens(_split_values(scalar_text))
            if material_tokens:
                metadata.setdefault("filament_type", ";".join(material_tokens))
            continue

        if "filament_colour" in normalized or "filament_color" in normalized or normalized.endswith("colour") or normalized.endswith("color"):
            color_tokens = _filter_hint_tokens(_split_values(scalar_text))
            if color_tokens:
                metadata.setdefault("filament_colour", ";".join(color_tokens))
            continue

        if "filament_vendor" in normalized or "vendor" in normalized or "brand" in normalized or "filament_settings_id" in normalized:
            brand_tokens = _filter_hint_tokens(_split_values(scalar_text))
            if brand_tokens:
                metadata.setdefault("vendor", ";".join(brand_tokens))


def _split_values(value: str):
    if not value:
        return []
    parts = re.split(r"[;,\n\r\t|]+", value)
    cleaned = [p.strip() for p in parts if p and p.strip()]
    seen = set()
    unique = []
    for item in cleaned:
        if not _is_plausible_hint_token(item):
            continue
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _split_token_list(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[;,\n\r\t|]+", value)
    cleaned = []
    for part in parts:
        item = str(part).strip().strip('"').strip("'").strip()
        if item:
            cleaned.append(item)
    return cleaned


def _extract_bambu_switch_count(gcode_text: str) -> int:
    if not gcode_text:
        return 0
    matches = re.findall(r"(?m)^M620\s+S(\d+)A\s*$", gcode_text)
    return len(matches)


def _parse_number(value: str):
    if not value:
        return None
    match = NUMBER_RE.search(value)
    if not match:
        return None
    token = match.group(1).strip()
    if not token:
        return None

    if "," in token:
        token = token.replace(",", ".")

    try:
        return float(token)
    except ValueError:
        return None


def _parse_number_list(value: str):
    if not value:
        return []

    chunks = [chunk.strip() for chunk in re.split(r"[;\n\r\t|]+", value) if chunk and chunk.strip()]
    matches = []

    for chunk in chunks:
        subparts = [chunk]
        if "," in chunk and "." in chunk:
            subparts = [part.strip() for part in chunk.split(",") if part and part.strip()]
        elif chunk.count(",") > 1 and "." not in chunk:
            subparts = [part.strip() for part in chunk.split(",") if part and part.strip()]

        for part in subparts:
            match = NUMBER_RE.search(part)
            if match:
                matches.append(match.group(1))

    if not matches:
        matches = NUMBER_RE.findall(value)

    numbers = []
    for item in matches:
        parsed = _parse_number(item)
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def _collect_from_text_blob(text: str, metadata: dict):
    if not text:
        return

    pair_pattern = re.compile(r"^\s*([^:=]{1,120})\s*[:=]\s*(.*?)\s*$", flags=re.IGNORECASE)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(";"):
            line = line.lstrip(";").strip()
        if not line:
            continue

        match = pair_pattern.match(line)
        if not match:
            continue

        raw_key = re.sub(r"\s+", " ", match.group(1).strip().lower())
        key = TEXT_KEY_ALIASES.get(raw_key)
        if not key:
            continue

        value = _clean_scalar(match.group(2))
        if not value:
            continue

        if key == "filament used [g]":
            if not _parse_number_list(value):
                continue
        elif key == "filament used [mm]":
            if not _parse_number_list(value):
                continue
        elif key in ("filament_type", "filament", "filament_is_support", "filament_cost", "filament_colour", "filament_settings_id", "vendor"):
            if not _split_token_list(value):
                continue
        else:
            candidates = _filter_hint_tokens(_split_values(value))
            if not candidates:
                continue
            value = ";".join(candidates)

        if key not in metadata:
            metadata[key] = value
            continue

        if key in ("filament_type", "filament", "filament_is_support", "filament_cost", "filament_colour", "filament_settings_id", "vendor"):
            existing_count = len(_split_token_list(metadata.get(key, "")))
            new_count = len(_split_token_list(value))
            if new_count > existing_count:
                metadata[key] = value


def parse_3mf_filament_usage(file_bytes: bytes):
    """
    Returns tuple (grams, millimeters, raw_metadata, filament_hints, usage_breakdown)
    grams or millimeters can be None if not found.

    filament_hints format:
    {
      "brands": ["..."],
      "materials": ["..."],
      "colors": ["..."]
    }
    """
    grams = None
    millimeters = None
    metadata = {}
    filament_hints = {"brands": [], "materials": [], "colors": []}
    usage_breakdown = []
    grams_candidates = []
    mm_candidates = []

    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        model_files = [
            name
            for name in zf.namelist()
            if name.lower().endswith("3d/3dmodel.model")
        ]
        if model_files:
            with zf.open(model_files[0]) as model_file:
                tree = ET.parse(model_file)
                root = tree.getroot()

            for meta in root.findall(".//{*}metadata"):
                name = meta.attrib.get("name", "").strip().lower()
                value = (meta.text or "").strip()
                metadata[name] = value

        for name in zf.namelist():
            lowered_name = name.lower()
            if not lowered_name.endswith(TEXT_EXTENSIONS):
                continue
            try:
                with zf.open(name) as handle:
                    raw = handle.read()
                try:
                    text = raw.decode("utf-8", errors="ignore")
                except Exception:
                    text = raw.decode("latin-1", errors="ignore")

                if lowered_name.endswith((".json", ".config")):
                    try:
                        parsed_json = json.loads(text)
                        _collect_from_json_blob(parsed_json, metadata, grams_candidates, mm_candidates)
                    except Exception:
                        pass

                if lowered_name.endswith(".gcode"):
                    switches = _extract_bambu_switch_count(text)
                    if switches > 0:
                        metadata["__bambu_filament_switches__"] = str(switches)

                _collect_from_text_blob(text, metadata)
            except Exception:
                continue

    for key in METADATA_KEYS_G:
        if key in metadata:
            if grams is None:
                grams = _parse_number(metadata[key])
            if not grams_candidates:
                numbers = _parse_number_list(metadata[key])
                if numbers:
                    grams_candidates.extend(numbers)
            if grams is not None:
                break

    for key in METADATA_KEYS_MM:
        if key in metadata:
            if millimeters is None:
                millimeters = _parse_number(metadata[key])
            if not mm_candidates:
                numbers = _parse_number_list(metadata[key])
                if numbers:
                    mm_candidates.extend(numbers)
            if millimeters is not None:
                break

    if mm_candidates:
        if len(mm_candidates) > 1:
            millimeters = round(sum(mm_candidates), 3)
        elif millimeters is None:
            millimeters = mm_candidates[0]

    for key, value in metadata.items():
        if grams is None and "filament" in key and "g" in key:
            grams = _parse_number(value)
            numbers = _parse_number_list(value)
            if numbers:
                grams_candidates.extend(numbers)
            if grams is not None:
                break

    for key, value in metadata.items():
        key_l = key.lower()
        if "filament_type" in key_l or "material" in key_l:
            filament_hints["materials"].extend(_filter_hint_tokens(_split_values(value)))
        if (
            "filament_colour" in key_l
            or "filament_color" in key_l
            or key_l.endswith("colour")
            or key_l.endswith("color")
        ):
            filament_hints["colors"].extend(_filter_hint_tokens(_split_values(value)))
        if "vendor" in key_l or "brand" in key_l or "filament_settings_id" in key_l:
            filament_hints["brands"].extend(_filter_hint_tokens(_split_values(value)))

    for hint_key in ("brands", "materials", "colors"):
        seen = set()
        unique = []
        for item in filament_hints[hint_key]:
            k = item.lower()
            if k not in seen:
                seen.add(k)
                unique.append(item)
        filament_hints[hint_key] = unique

    if not grams_candidates:
        for key, value in metadata.items():
            key_l = key.lower()
            if "filament" in key_l and ("used" in key_l or "weight" in key_l) and "g" in key_l:
                grams_candidates.extend(_parse_number_list(value))

    if grams_candidates:
        if len(grams_candidates) > 1:
            grams = round(sum(grams_candidates), 3)
        elif grams is None:
            grams = grams_candidates[0]

        materials = filament_hints.get("materials", [])
        if materials and len(materials) == len(grams_candidates):
            usage_breakdown = [
                {"material": materials[i], "grams": round(grams_candidates[i], 3)}
                for i in range(len(grams_candidates))
            ]
        elif len(grams_candidates) > 1:
            filament_slots = []
            for token in _split_token_list(metadata.get("filament", "")):
                try:
                    filament_slots.append(int(float(token)))
                except ValueError:
                    continue

            filament_types = _split_token_list(metadata.get("filament_type", ""))
            support_flags = [int(v) for v in _parse_number_list(metadata.get("filament_is_support", ""))]

            def _label_for_index(index: int) -> str:
                slot = filament_slots[index] if index < len(filament_slots) else (index + 1)
                material = ""
                is_support = False
                slot_index = max(0, slot - 1)

                if slot_index < len(filament_types):
                    material = filament_types[slot_index]
                if slot_index < len(support_flags):
                    is_support = support_flags[slot_index] == 1

                if material and is_support:
                    return f"Filament {slot} ({material} Support)"
                if material:
                    return f"Filament {slot} ({material})"
                if is_support:
                    return f"Filament {slot} (Support)"
                return f"Filament {slot}"

            usage_breakdown = [
                {"material": _label_for_index(i), "grams": round(val, 3)}
                for i, val in enumerate(grams_candidates)
            ]
        elif len(grams_candidates) == 1:
            label = materials[0] if materials else "Filament"
            usage_breakdown = [{"material": label, "grams": round(grams_candidates[0], 3)}]

    if usage_breakdown:
        filament_slots = []
        for token in _split_token_list(metadata.get("filament", "")):
            try:
                filament_slots.append(int(float(token)))
            except ValueError:
                continue

        filament_types = _split_token_list(metadata.get("filament_type", ""))
        support_flags = [int(v) for v in _parse_number_list(metadata.get("filament_is_support", ""))]

        per_filament_mm = _parse_number_list(metadata.get("filament used [mm]", ""))
        filament_costs = _parse_number_list(metadata.get("filament_cost", ""))

        total_cost = 0.0
        total_cost_counted = False
        for index, row in enumerate(usage_breakdown):
            slot = filament_slots[index] if index < len(filament_slots) else None
            if slot is not None:
                row["slot"] = slot
                slot_index = max(0, slot - 1)
                if slot_index < len(filament_types):
                    row["parsed_material"] = filament_types[slot_index]
                if slot_index < len(support_flags):
                    row["is_support"] = support_flags[slot_index] == 1

            if index < len(per_filament_mm):
                row["length_mm"] = round(float(per_filament_mm[index]), 3)
                row["length_m"] = round(float(per_filament_mm[index]) / 1000.0, 2)

            if slot is not None and row.get("grams") is not None:
                slot_index = slot - 1
                if 0 <= slot_index < len(filament_costs):
                    cost_per_kg = float(filament_costs[slot_index])
                    row_cost = float(row["grams"]) * cost_per_kg / 1000.0
                    row["estimated_cost"] = round(row_cost, 2)
                    total_cost += row_cost
                    total_cost_counted = True

        if total_cost_counted:
            metadata["__bambu_total_cost__"] = str(round(total_cost, 2))

    return grams, millimeters, metadata, filament_hints, usage_breakdown
