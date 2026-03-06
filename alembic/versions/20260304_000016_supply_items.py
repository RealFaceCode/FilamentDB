"""Add supply items table

Revision ID: 20260304_000016
Revises: 20260303_000015
Create Date: 2026-03-04 00:00:16

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260304_000016"
down_revision: Union[str, None] = "20260303_000015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "supply_items" not in existing_tables:
        op.create_table(
            "supply_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("category", sa.String(length=80), nullable=False),
            sa.Column("quantity", sa.Float(), nullable=False),
            sa.Column("unit", sa.String(length=32), nullable=False),
            sa.Column("min_quantity", sa.Float(), nullable=True),
            sa.Column("location", sa.String(length=120), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_supply_items_id", "supply_items", ["id"], unique=False)
        op.create_index("ix_supply_items_project", "supply_items", ["project"], unique=False)
        op.create_index("ix_supply_items_name", "supply_items", ["name"], unique=False)
        op.create_index("ix_supply_items_category", "supply_items", ["category"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "supply_items" in existing_tables:
        for index_name in (
            "ix_supply_items_category",
            "ix_supply_items_name",
            "ix_supply_items_project",
            "ix_supply_items_id",
        ):
            op.drop_index(index_name, table_name="supply_items")
        op.drop_table("supply_items")
