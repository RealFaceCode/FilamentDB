"""Add external spool telemetry flag to printers

Revision ID: 20260301_000012
Revises: 20260224_000011
Create Date: 2026-03-01 00:00:12

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260301_000012"
down_revision: Union[str, None] = "20260224_000011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    printer_columns = {col.get("name") for col in inspector.get_columns("printers")}
    if "telemetry_external_spool_active" not in printer_columns:
        with op.batch_alter_table("printers") as batch_op:
            batch_op.add_column(sa.Column("telemetry_external_spool_active", sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    printer_columns = {col.get("name") for col in inspector.get_columns("printers")}
    if "telemetry_external_spool_active" in printer_columns:
        with op.batch_alter_table("printers") as batch_op:
            batch_op.drop_column("telemetry_external_spool_active")
