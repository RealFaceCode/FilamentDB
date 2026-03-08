from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import text


def load_backup_auto_settings(
    load_setting_fn: Callable[[str], Optional[str]],
    is_truthy_fn: Callable[[Optional[str]], bool],
    clamp_int_fn: Callable[[object, int, int, int], int],
    enabled_key: str,
    interval_key: str,
    retention_key: str,
    last_run_key: str,
    min_interval_hours: int,
    max_interval_hours: int,
    min_retention_days: int,
    max_retention_days: int,
) -> dict[str, object]:
    enabled = is_truthy_fn(load_setting_fn(enabled_key))
    interval_hours = clamp_int_fn(
        load_setting_fn(interval_key),
        min_interval_hours,
        max_interval_hours,
        24,
    )
    retention_days = clamp_int_fn(
        load_setting_fn(retention_key),
        min_retention_days,
        max_retention_days,
        14,
    )
    last_run_raw = str(load_setting_fn(last_run_key) or "").strip()
    last_run_at: Optional[datetime] = None
    if last_run_raw:
        try:
            parsed = datetime.fromisoformat(last_run_raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            last_run_at = parsed.astimezone(timezone.utc)
        except Exception:
            last_run_at = None
    return {
        "enabled": enabled,
        "interval_hours": interval_hours,
        "retention_days": retention_days,
        "last_run_at": last_run_at,
    }


def save_backup_auto_settings(
    save_setting_fn: Callable[[str, str], None],
    enabled_key: str,
    interval_key: str,
    retention_key: str,
    enabled: bool,
    interval_hours: int,
    retention_days: int,
) -> None:
    save_setting_fn(enabled_key, "1" if enabled else "0")
    save_setting_fn(interval_key, str(interval_hours))
    save_setting_fn(retention_key, str(retention_days))


def prune_old_backup_files(
    list_files_fn: Callable[[str], list[dict[str, object]]],
    resolve_file_fn: Callable[[str, str], Optional[object]],
    utcnow_fn: Callable[[], datetime],
    mode: str,
    retention_days: int,
    min_retention_days: int,
) -> int:
    removed = 0
    cutoff = utcnow_fn() - timedelta(days=max(min_retention_days, int(retention_days)))
    for item in list_files_fn(mode):
        modified_at = item.get("modified_at")
        if not isinstance(modified_at, datetime):
            continue
        if modified_at >= cutoff:
            continue
        target = resolve_file_fn(mode, str(item.get("name") or ""))
        if target is None or not target.exists():
            continue
        try:
            target.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


def build_backup_context(
    translate: Callable[[str], str],
    backup_mode: str,
    tools_ok: bool,
    auto_settings: dict[str, object],
    backup_files: list[dict[str, object]],
    storage_dir_display: str,
    reset_confirm_phrase: str,
    **extra,
) -> dict:
    if backup_mode == "sqlite":
        context = {
            "backup_supported": True,
            "backup_notice": None,
            "backup_accept": ".db",
            "backup_hint_text": translate("backup_hint_sqlite"),
        }
    elif backup_mode == "postgresql":
        context = {
            "backup_supported": bool(tools_ok),
            "backup_notice": None if tools_ok else translate("backup_pg_tools_missing"),
            "backup_accept": ".dump,.backup",
            "backup_hint_text": translate("backup_hint_postgres") if tools_ok else translate("backup_pg_tools_missing"),
        }
    else:
        context = {
            "backup_supported": False,
            "backup_notice": translate("backup_unsupported"),
            "backup_accept": "",
            "backup_hint_text": translate("backup_unsupported"),
        }

    context.update(extra)
    context.setdefault("backup_files", backup_files)
    context.setdefault("backup_storage_dir", storage_dir_display)
    context.setdefault("backup_auto_enabled", bool(auto_settings.get("enabled")))
    context.setdefault("backup_auto_interval_hours", int(auto_settings.get("interval_hours") or 24))
    context.setdefault("backup_auto_retention_days", int(auto_settings.get("retention_days") or 14))
    context.setdefault("backup_auto_last_run_at", auto_settings.get("last_run_at"))
    context.setdefault("backup_active_tab", "manual")
    context.setdefault("backup_reset_confirm_phrase", reset_confirm_phrase)
    return context


def delete_all_database_rows(engine, sorted_tables: list) -> int:
    deleted_rows = 0
    with engine.begin() as connection:
        is_sqlite = str(getattr(engine.dialect, "name", "")).lower() == "sqlite"
        if is_sqlite:
            connection.execute(text("PRAGMA foreign_keys=OFF"))
        try:
            for table in reversed(sorted_tables):
                result = connection.execute(table.delete())
                rowcount = int(result.rowcount or 0)
                if rowcount > 0:
                    deleted_rows += rowcount
        finally:
            if is_sqlite:
                connection.execute(text("PRAGMA foreign_keys=ON"))
    return deleted_rows
