"""Add AMS metadata columns to device_slot_state

Revision ID: 20260302_000013
Revises: 20260301_000012
Create Date: 2026-03-02 00:00:13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260302_000013"
down_revision: Union[str, None] = "20260301_000012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col.get("name") for col in inspector.get_columns("device_slot_state")}
    with op.batch_alter_table("device_slot_state") as batch_op:
        if "ams_unit" not in columns:
            batch_op.add_column(sa.Column("ams_unit", sa.Integer(), nullable=True))
        if "slot_local" not in columns:
            batch_op.add_column(sa.Column("slot_local", sa.Integer(), nullable=True))
        if "ams_name" not in columns:
            batch_op.add_column(sa.Column("ams_name", sa.String(length=120), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col.get("name") for col in inspector.get_columns("device_slot_state")}
    with op.batch_alter_table("device_slot_state") as batch_op:
        if "ams_name" in columns:
            batch_op.drop_column("ams_name")
        if "slot_local" in columns:
            batch_op.drop_column("slot_local")
        if "ams_unit" in columns:
            batch_op.drop_column("ams_unit")
