"""Enforce not-null user ownership on core tables

Revision ID: 20260223_000008
Revises: 20260223_000007
Create Date: 2026-02-23 00:00:08

"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260223_000008"
down_revision: Union[str, None] = "20260223_000007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCOPE_RE = re.compile(r"^u(?P<user_id>\d+)_(private|business)$", flags=re.IGNORECASE)
_TABLES = [
    "spools",
    "usage_history",
    "usage_batch_context",
    "device_slot_state",
    "storage_areas",
    "storage_sub_locations",
]


def _ensure_fallback_user_id(bind) -> int | None:
    fallback_user_id = bind.execute(sa.text("SELECT MIN(id) FROM users")).scalar()
    if fallback_user_id is not None:
        return int(fallback_user_id)

    bind.execute(
        sa.text(
            """
            INSERT INTO users (email, display_name, password_hash, is_active, created_at, updated_at)
            VALUES (:email, :display_name, :password_hash, :is_active, :created_at, :updated_at)
            """
        ),
        {
            "email": "migration-owner@local",
            "display_name": "Migration Owner",
            "password_hash": "migration_seed_not_for_login",
            "is_active": True,
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        },
    )
    fallback_user_id = bind.execute(sa.text("SELECT MIN(id) FROM users")).scalar()
    return int(fallback_user_id) if fallback_user_id is not None else None


def _extract_user_id(project_value: object, fallback_user_id: int | None) -> int | None:
    raw = str(project_value or "").strip()
    match = _SCOPE_RE.match(raw)
    if match:
        try:
            return int(match.group("user_id"))
        except Exception:
            return fallback_user_id
    return fallback_user_id


def _ensure_no_null_user_id(bind, table_name: str) -> None:
    null_count = bind.execute(
        sa.text(f"SELECT COUNT(1) FROM {table_name} WHERE user_id IS NULL")
    ).scalar() or 0
    if int(null_count) > 0:
        raise RuntimeError(f"Cannot enforce NOT NULL on {table_name}.user_id; {null_count} rows still NULL")


def _backfill_missing_user_ids(bind, table_name: str, fallback_user_id: int | None) -> None:
    rows = bind.execute(
        sa.text(f"SELECT id, project, user_id FROM {table_name} WHERE user_id IS NULL")
    ).fetchall()
    for row_id, project, _user_id in rows:
        resolved_user_id = _extract_user_id(project, fallback_user_id)
        if resolved_user_id is None:
            continue
        bind.execute(
            sa.text(f"UPDATE {table_name} SET user_id = :user_id WHERE id = :row_id"),
            {"user_id": int(resolved_user_id), "row_id": int(row_id)},
        )


def upgrade() -> None:
    bind = op.get_bind()
    fallback_user_id = _ensure_fallback_user_id(bind)

    for table_name in _TABLES:
        _backfill_missing_user_ids(bind, table_name, fallback_user_id)
        _ensure_no_null_user_id(bind, table_name)

    for table_name in _TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    for table_name in _TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
