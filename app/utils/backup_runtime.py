from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from typing import Optional


_ENGINE = None
_BACKUP_STORAGE_DIR: Optional[Path] = None


def configure_backup_runtime(engine, backup_storage_dir: Path) -> None:
    global _ENGINE, _BACKUP_STORAGE_DIR
    _ENGINE = engine
    _BACKUP_STORAGE_DIR = backup_storage_dir


def _require_engine():
    if _ENGINE is None:
        raise RuntimeError("backup runtime engine is not configured")
    return _ENGINE


def _require_storage_dir() -> Path:
    if _BACKUP_STORAGE_DIR is None:
        raise RuntimeError("backup runtime storage dir is not configured")
    return _BACKUP_STORAGE_DIR


def sqlite_db_path() -> Optional[Path]:
    engine = _require_engine()
    database = getattr(engine.url, "database", None)
    if not database:
        return None
    return Path(database)


def is_sqlite_database() -> bool:
    engine = _require_engine()
    return str(getattr(engine.url, "drivername", "")).startswith("sqlite")


def is_postgresql_database() -> bool:
    engine = _require_engine()
    return str(getattr(engine.url, "drivername", "")).startswith("postgresql")


def backup_mode() -> str:
    if is_sqlite_database():
        return "sqlite"
    if is_postgresql_database():
        return "postgresql"
    return "unsupported"


def pg_tools_available() -> bool:
    return bool(shutil.which("pg_dump") and shutil.which("pg_restore"))


def postgres_subprocess_env() -> dict[str, str]:
    engine = _require_engine()
    env = os.environ.copy()
    password = getattr(engine.url, "password", None)
    if password:
        env["PGPASSWORD"] = str(password)
    return env


def postgres_connection_args() -> list[str]:
    engine = _require_engine()
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


def cleanup_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_backup_storage_dir() -> Optional[Path]:
    storage_dir = _require_storage_dir()
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        return storage_dir
    except OSError:
        return None


def backup_file_extension(mode: str) -> str:
    return ".db" if mode == "sqlite" else ".dump"


def clamp_int(value: object, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def build_backup_filename(mode: str, source: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    normalized_source = "auto" if str(source).strip().lower() == "auto" else "manual"
    return f"filament_{mode}_{normalized_source}_{timestamp}{backup_file_extension(mode)}"


def resolve_backup_file_path(mode: str, filename: str) -> Optional[Path]:
    storage_dir = ensure_backup_storage_dir()
    if storage_dir is None:
        return None

    raw_name = str(filename or "").strip()
    safe_name = Path(raw_name).name
    if not raw_name or safe_name != raw_name:
        return None
    if not re.match(r"^[A-Za-z0-9._-]+$", safe_name):
        return None
    if not safe_name.endswith(backup_file_extension(mode)):
        return None

    candidate = (storage_dir / safe_name).resolve()
    if candidate.parent != storage_dir.resolve():
        return None
    return candidate


def list_backup_files(mode: str) -> list[dict[str, object]]:
    storage_dir = ensure_backup_storage_dir()
    if storage_dir is None:
        return []

    extension = backup_file_extension(mode)
    entries: list[dict[str, object]] = []
    for path in storage_dir.glob(f"*{extension}"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        source = "auto" if "_auto_" in path.name else "manual"
        entries.append(
            {
                "name": path.name,
                "size_bytes": int(stat.st_size),
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                "source": source,
            }
        )
    entries.sort(key=lambda item: item["modified_at"], reverse=True)
    return entries


def run_sqlite_backup_to_path(target_path: Path) -> bool:
    engine = _require_engine()
    source_path = sqlite_db_path()
    if source_path is None or not source_path.exists():
        return False
    engine.dispose()
    with sqlite3.connect(str(source_path)) as source_conn, sqlite3.connect(str(target_path)) as target_conn:
        source_conn.backup(target_conn)
    return bool(target_path.exists() and target_path.stat().st_size > 0)


def run_postgres_backup_to_path(target_path: Path) -> bool:
    if not pg_tools_available():
        return False
    cmd = ["pg_dump", "-Fc", "--no-owner", "--no-privileges", *postgres_connection_args(), "-f", str(target_path)]
    result = subprocess.run(cmd, env=postgres_subprocess_env(), capture_output=True, text=True)
    return result.returncode == 0 and bool(target_path.exists() and target_path.stat().st_size > 0)


def create_backup_snapshot(mode: str, source: str = "manual") -> tuple[Optional[Path], Optional[str]]:
    storage_dir = ensure_backup_storage_dir()
    if storage_dir is None:
        return None, "backup_storage_unavailable"

    backup_path = storage_dir / build_backup_filename(mode, source)
    success = False
    try:
        if mode == "sqlite":
            success = run_sqlite_backup_to_path(backup_path)
        elif mode == "postgresql":
            success = run_postgres_backup_to_path(backup_path)
        else:
            return None, "backup_unsupported"
    except Exception:
        success = False

    if not success:
        cleanup_temp_file(backup_path)
        if mode == "postgresql" and not pg_tools_available():
            return None, "backup_pg_tools_missing"
        return None, "backup_create_failed"

    return backup_path, None


def restore_from_backup_path(mode: str, backup_path: Path) -> bool:
    engine = _require_engine()
    if mode == "sqlite":
        db_path = sqlite_db_path()
        if db_path is None or not backup_path.exists():
            return False
        engine.dispose()
        with sqlite3.connect(str(backup_path)) as source_conn, sqlite3.connect(str(db_path)) as target_conn:
            source_conn.backup(target_conn)
        return True

    if mode == "postgresql":
        if not pg_tools_available():
            return False
        engine.dispose()
        cmd = [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            *postgres_connection_args(),
            str(backup_path),
        ]
        result = subprocess.run(cmd, env=postgres_subprocess_env(), capture_output=True, text=True)
        return result.returncode == 0

    return False
