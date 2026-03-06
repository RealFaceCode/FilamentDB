"""Add per-printer AMS name mapping column

Revision ID: 20260302_000014
Revises: 20260302_000013
Create Date: 2026-03-02 00:00:14

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260302_000014"
down_revision: Union[str, None] = "20260302_000013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col.get("name") for col in inspector.get_columns("printers")}
    with op.batch_alter_table("printers") as batch_op:
        if "ams_name_map" not in columns:
            batch_op.add_column(sa.Column("ams_name_map", sa.String(length=500), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col.get("name") for col in inspector.get_columns("printers")}
    with op.batch_alter_table("printers") as batch_op:
        if "ams_name_map" in columns:
            batch_op.drop_column("ams_name_map")
