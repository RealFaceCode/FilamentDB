from __future__ import annotations

from typing import Optional


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
