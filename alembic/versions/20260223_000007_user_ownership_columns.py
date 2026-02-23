"""Add user ownership columns to core tables

Revision ID: 20260223_000007
Revises: 20260223_000006
Create Date: 2026-02-23 00:00:07

"""
from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260223_000007"
down_revision: Union[str, None] = "20260223_000006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCOPE_RE = re.compile(r"^u(?P<user_id>\d+)_(private|business)$", flags=re.IGNORECASE)


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    try:
        return {idx.get("name") for idx in inspector.get_indexes(table_name)}
    except Exception:
        return set()


def _foreign_key_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    try:
        return {fk.get("name") for fk in inspector.get_foreign_keys(table_name) if fk.get("name")}
    except Exception:
        return set()


def _extract_user_id(project_value: object, fallback_user_id: int | None) -> int | None:
    raw = str(project_value or "").strip()
    match = _SCOPE_RE.match(raw)
    if match:
        try:
            return int(match.group("user_id"))
        except Exception:
            return fallback_user_id
    return fallback_user_id


def _backfill_user_ids(bind, table_name: str, fallback_user_id: int | None) -> None:
    rows = bind.execute(sa.text(f"SELECT id, project, user_id FROM {table_name}")).fetchall()
    for row in rows:
        row_id = int(row[0])
        project = row[1]
        existing_user_id = row[2]
        if existing_user_id is not None:
            continue
        resolved_user_id = _extract_user_id(project, fallback_user_id)
        if resolved_user_id is None:
            continue
        bind.execute(
            sa.text(f"UPDATE {table_name} SET user_id = :user_id WHERE id = :row_id"),
            {"user_id": int(resolved_user_id), "row_id": row_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = [
        "spools",
        "usage_history",
        "usage_batch_context",
        "device_slot_state",
        "storage_areas",
        "storage_sub_locations",
    ]

    for table_name in table_names:
        columns = {column.get("name") for column in inspector.get_columns(table_name)}
        if "user_id" not in columns:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    for table_name in table_names:
        index_name = f"ix_{table_name}_user_id"
        indexes = _index_names(inspector, table_name)
        if index_name not in indexes:
            op.create_index(index_name, table_name, ["user_id"], unique=False)

        fk_name = f"fk_{table_name}_user_id"
        fks = _foreign_key_names(inspector, table_name)
        if fk_name not in fks:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.create_foreign_key(fk_name, "users", ["user_id"], ["id"])

    fallback_user_id = bind.execute(sa.text("SELECT MIN(id) FROM users")).scalar()
    if fallback_user_id is not None:
        fallback_user_id = int(fallback_user_id)

    for table_name in table_names:
        _backfill_user_ids(bind, table_name, fallback_user_id)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = [
        "spools",
        "usage_history",
        "usage_batch_context",
        "device_slot_state",
        "storage_areas",
        "storage_sub_locations",
    ]

    for table_name in table_names:
        indexes = _index_names(inspector, table_name)
        index_name = f"ix_{table_name}_user_id"
        if index_name in indexes:
            op.drop_index(index_name, table_name=table_name)

        fks = _foreign_key_names(inspector, table_name)
        fk_name = f"fk_{table_name}_user_id"
        columns = {column.get("name") for column in inspector.get_columns(table_name)}
        with op.batch_alter_table(table_name) as batch_op:
            if fk_name in fks:
                batch_op.drop_constraint(fk_name, type_="foreignkey")
            if "user_id" in columns:
                batch_op.drop_column("user_id")
