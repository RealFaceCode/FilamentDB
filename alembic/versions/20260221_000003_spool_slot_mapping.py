"""Add spool AMS slot mapping fields

Revision ID: 20260221_000003
Revises: 20260221_000002
Create Date: 2026-02-21 00:00:03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260221_000003"
down_revision: Union[str, None] = "20260221_000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column.get("name") for column in inspector.get_columns("spools")}

    if "ams_printer" not in columns:
        op.add_column("spools", sa.Column("ams_printer", sa.String(length=120), nullable=True))
    if "ams_slot" not in columns:
        op.add_column("spools", sa.Column("ams_slot", sa.Integer(), nullable=True))

    existing_indexes = {idx.get("name") for idx in inspector.get_indexes("spools")}
    for index_name, columns_for_index in [
        (op.f("ix_spools_ams_printer"), ["ams_printer"]),
        (op.f("ix_spools_ams_slot"), ["ams_slot"]),
    ]:
        if index_name not in existing_indexes:
            op.create_index(index_name, "spools", columns_for_index, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = {idx.get("name") for idx in inspector.get_indexes("spools")}

    for index_name in [op.f("ix_spools_ams_slot"), op.f("ix_spools_ams_printer")]:
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="spools")

    columns = {column.get("name") for column in inspector.get_columns("spools")}
    if "ams_slot" in columns:
        op.drop_column("spools", "ams_slot")
    if "ams_printer" in columns:
        op.drop_column("spools", "ams_printer")
