"""Initial schema

Revision ID: 20260220_000001
Revises:
Create Date: 2026-02-20 00:00:01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260220_000001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    def ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
        existing_indexes = {idx.get("name") for idx in inspector.get_indexes(table_name)}
        if index_name not in existing_indexes:
            op.create_index(index_name, table_name, columns, unique=False)

    if "spools" not in existing_tables:
        op.create_table(
            "spools",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("brand", sa.String(length=120), nullable=False),
            sa.Column("material", sa.String(length=80), nullable=False),
            sa.Column("color", sa.String(length=80), nullable=False),
            sa.Column("weight_g", sa.Float(), nullable=False),
            sa.Column("remaining_g", sa.Float(), nullable=False),
            sa.Column("low_stock_threshold_g", sa.Float(), nullable=True),
            sa.Column("price", sa.Float(), nullable=True),
            sa.Column("location", sa.String(length=120), nullable=True),
            sa.Column("in_use", sa.Boolean(), nullable=True),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        existing_tables.add("spools")

    ensure_index("spools", op.f("ix_spools_id"), ["id"])
    ensure_index("spools", op.f("ix_spools_project"), ["project"])

    if "usage_history" not in existing_tables:
        op.create_table(
            "usage_history",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("actor", sa.String(length=120), nullable=True),
            sa.Column("mode", sa.String(length=20), nullable=False),
            sa.Column("source_app", sa.String(length=120), nullable=True),
            sa.Column("batch_id", sa.String(length=64), nullable=True),
            sa.Column("source_file", sa.String(length=255), nullable=True),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("spool_id", sa.Integer(), nullable=True),
            sa.Column("spool_brand", sa.String(length=120), nullable=True),
            sa.Column("spool_material", sa.String(length=80), nullable=True),
            sa.Column("spool_color", sa.String(length=80), nullable=True),
            sa.Column("deducted_g", sa.Float(), nullable=False),
            sa.Column("remaining_before_g", sa.Float(), nullable=False),
            sa.Column("remaining_after_g", sa.Float(), nullable=False),
            sa.Column("undone", sa.Boolean(), nullable=True),
            sa.Column("undone_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        existing_tables.add("usage_history")

    ensure_index("usage_history", op.f("ix_usage_history_batch_id"), ["batch_id"])
    ensure_index("usage_history", op.f("ix_usage_history_created_at"), ["created_at"])
    ensure_index("usage_history", op.f("ix_usage_history_id"), ["id"])
    ensure_index("usage_history", op.f("ix_usage_history_project"), ["project"])
    ensure_index("usage_history", op.f("ix_usage_history_spool_id"), ["spool_id"])

    if "app_settings" not in existing_tables:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(length=80), nullable=False),
            sa.Column("value", sa.String(length=255), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("key"),
        )
        existing_tables.add("app_settings")

    ensure_index("app_settings", op.f("ix_app_settings_key"), ["key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "app_settings" in existing_tables:
        existing_indexes = {idx.get("name") for idx in inspector.get_indexes("app_settings")}
        index_name = op.f("ix_app_settings_key")
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="app_settings")
        op.drop_table("app_settings")

    if "usage_history" in existing_tables:
        existing_indexes = {idx.get("name") for idx in inspector.get_indexes("usage_history")}
        for index_name in [
            op.f("ix_usage_history_spool_id"),
            op.f("ix_usage_history_project"),
            op.f("ix_usage_history_id"),
            op.f("ix_usage_history_created_at"),
            op.f("ix_usage_history_batch_id"),
        ]:
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="usage_history")
        op.drop_table("usage_history")

    if "spools" in existing_tables:
        existing_indexes = {idx.get("name") for idx in inspector.get_indexes("spools")}
        for index_name in [op.f("ix_spools_project"), op.f("ix_spools_id")]:
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="spools")
        op.drop_table("spools")
