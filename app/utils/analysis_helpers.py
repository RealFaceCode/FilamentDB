from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Spool, UsageBatchContext, UsageHistory


def bounded_int(value: Optional[int], default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def month_key_expr_for_db(db: Session, column):
    dialect_name = ""
    if db.bind is not None and getattr(db.bind, "dialect", None) is not None:
        dialect_name = str(db.bind.dialect.name or "").lower()
    if dialect_name == "postgresql":
        return func.to_char(column, "YYYY-MM")
    if dialect_name in {"mysql", "mariadb"}:
        return func.date_format(column, "%Y-%m")
    return func.strftime("%Y-%m", column)


def analysis_month_keys(now: datetime, months: int) -> list[str]:
    keys: list[str] = []
    year = int(now.year)
    month = int(now.month)
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    keys.reverse()
    return keys


def analysis_usage_and_cost_in_period(
    db: Session,
    usage_scope_filters: list,
    period_start: datetime,
    period_end: datetime,
) -> tuple[float, float]:
    usage_g = (
        db.query(func.sum(UsageHistory.deducted_g))
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= period_start)
        .filter(UsageHistory.created_at < period_end)
        .scalar()
        or 0.0
    )
    cost_eur = (
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
        .filter(UsageHistory.created_at >= period_start)
        .filter(UsageHistory.created_at < period_end)
        .scalar()
        or 0.0
    )
    return round(float(usage_g), 1), round(float(cost_eur), 2)


def analysis_usage_cost_trend(
    db: Session,
    usage_scope_filters: list,
    months: int,
) -> list[dict]:
    now = datetime.now(timezone.utc)
    month_keys = analysis_month_keys(now, months)
    first_year, first_month = month_keys[0].split("-")
    trend_start = datetime(int(first_year), int(first_month), 1)
    month_expr = month_key_expr_for_db(db, UsageHistory.created_at)

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
        row.month_key: round(float(row.usage_g or 0.0), 1)
        for row in usage_by_month_rows
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
        row.month_key: round(float(row.cost_eur or 0.0), 2)
        for row in cost_by_month_rows
    }

    trend: list[dict] = []
    for month_key in month_keys:
        year_str, month_str = month_key.split("-")
        trend.append(
            {
                "month_key": month_key,
                "label": f"{month_str}/{year_str}",
                "usage_g": usage_by_month.get(month_key, 0.0),
                "cost_eur": cost_by_month.get(month_key, 0.0),
            }
        )
    return trend


def analysis_top_usage(
    db: Session,
    usage_scope_filters: list,
    period_start: datetime,
    period_end: datetime,
    group_by: str,
    limit: int,
) -> list[dict]:
    if group_by == "color":
        name_expr = func.coalesce(UsageHistory.spool_color, "-")
    else:
        name_expr = func.coalesce(UsageHistory.spool_material, "-")

    rows = (
        db.query(
            name_expr.label("name"),
            func.sum(UsageHistory.deducted_g).label("usage_g"),
        )
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.created_at >= period_start)
        .filter(UsageHistory.created_at < period_end)
        .group_by(name_expr)
        .order_by(func.sum(UsageHistory.deducted_g).desc())
        .limit(limit)
        .all()
    )
    total_usage = sum(float(row.usage_g or 0.0) for row in rows)
    payload: list[dict] = []
    for row in rows:
        usage_g = round(float(row.usage_g or 0.0), 1)
        payload.append(
            {
                "name": row.name if row.name not in (None, "") else "-",
                "usage_g": usage_g,
                "share_pct": round((usage_g / total_usage * 100), 1) if total_usage > 0 else 0.0,
            }
        )
    return payload


def analysis_printer_slot_usage(
    db: Session,
    usage_scope_filters: list,
    period_start: datetime,
    period_end: datetime,
    limit: int,
) -> list[dict]:
    batch_printer_expr = func.coalesce(UsageBatchContext.printer_name, Spool.ams_printer, "-")
    slot_expr = Spool.ams_slot
    rows = (
        db.query(
            batch_printer_expr.label("printer"),
            slot_expr.label("slot"),
            func.sum(UsageHistory.deducted_g).label("usage_g"),
        )
        .outerjoin(
            UsageBatchContext,
            (UsageBatchContext.project == UsageHistory.project)
            & (UsageBatchContext.batch_id == UsageHistory.batch_id),
        )
        .outerjoin(Spool, Spool.id == UsageHistory.spool_id)
        .filter(*usage_scope_filters)
        .filter(UsageHistory.undone.is_(False))
        .filter(UsageHistory.batch_id.is_not(None))
        .filter(UsageBatchContext.id.is_not(None))
        .filter(UsageHistory.created_at >= period_start)
        .filter(UsageHistory.created_at < period_end)
        .group_by(batch_printer_expr, slot_expr)
        .order_by(func.sum(UsageHistory.deducted_g).desc())
        .limit(limit)
        .all()
    )
    payload: list[dict] = []
    for row in rows:
        slot = int(row.slot) if row.slot is not None else None
        payload.append(
            {
                "printer": str(row.printer or "-").strip() or "-",
                "slot": slot,
                "slot_label": f"Slot {slot}" if slot is not None else "-",
                "usage_g": round(float(row.usage_g or 0.0), 1),
            }
        )
    return payload
