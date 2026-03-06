"""Add supply categories table

Revision ID: 20260304_000017
Revises: 20260304_000016
Create Date: 2026-03-04 00:00:17

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260304_000017"
down_revision: Union[str, None] = "20260304_000016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "supply_categories" not in existing_tables:
        op.create_table(
            "supply_categories",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project", "name", name="uq_supply_categories_project_name"),
        )
        op.create_index("ix_supply_categories_id", "supply_categories", ["id"], unique=False)
        op.create_index("ix_supply_categories_project", "supply_categories", ["project"], unique=False)
        op.create_index("ix_supply_categories_name", "supply_categories", ["name"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "supply_categories" in existing_tables:
        for index_name in (
            "ix_supply_categories_name",
            "ix_supply_categories_project",
            "ix_supply_categories_id",
        ):
            op.drop_index(index_name, table_name="supply_categories")
        op.drop_table("supply_categories")
