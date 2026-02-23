"""Add usage batch context table

Revision ID: 20260221_000002
Revises: 20260220_000001
Create Date: 2026-02-21 00:00:02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260221_000002"
down_revision: Union[str, None] = "20260220_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "usage_batch_context" not in existing_tables:
        op.create_table(
            "usage_batch_context",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("batch_id", sa.String(length=64), nullable=False),
            sa.Column("printer_name", sa.String(length=120), nullable=True),
            sa.Column("ams_slots", sa.String(length=255), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project", "batch_id", name="uq_usage_batch_context_project_batch"),
        )

    existing_indexes = {idx.get("name") for idx in inspector.get_indexes("usage_batch_context")}
    for index_name, columns in [
        (op.f("ix_usage_batch_context_id"), ["id"]),
        (op.f("ix_usage_batch_context_created_at"), ["created_at"]),
        (op.f("ix_usage_batch_context_project"), ["project"]),
        (op.f("ix_usage_batch_context_batch_id"), ["batch_id"]),
    ]:
        if index_name not in existing_indexes:
            op.create_index(index_name, "usage_batch_context", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "usage_batch_context" in existing_tables:
        existing_indexes = {idx.get("name") for idx in inspector.get_indexes("usage_batch_context")}
        for index_name in [
            op.f("ix_usage_batch_context_batch_id"),
            op.f("ix_usage_batch_context_project"),
            op.f("ix_usage_batch_context_created_at"),
            op.f("ix_usage_batch_context_id"),
        ]:
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="usage_batch_context")

        op.drop_table("usage_batch_context")
