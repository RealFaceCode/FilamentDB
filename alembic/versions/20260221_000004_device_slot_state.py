"""Add device slot state table

Revision ID: 20260221_000004
Revises: 20260221_000003
Create Date: 2026-02-21 00:00:04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260221_000004"
down_revision: Union[str, None] = "20260221_000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "device_slot_state" not in existing_tables:
        op.create_table(
            "device_slot_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("printer_name", sa.String(length=120), nullable=False),
            sa.Column("slot", sa.Integer(), nullable=False),
            sa.Column("observed_brand", sa.String(length=120), nullable=True),
            sa.Column("observed_material", sa.String(length=80), nullable=True),
            sa.Column("observed_color", sa.String(length=80), nullable=True),
            sa.Column("source", sa.String(length=120), nullable=True),
            sa.Column("observed_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project", "printer_name", "slot", name="uq_device_slot_state_project_printer_slot"),
        )

    existing_indexes = {idx.get("name") for idx in inspector.get_indexes("device_slot_state")}
    for index_name, columns in [
        (op.f("ix_device_slot_state_id"), ["id"]),
        (op.f("ix_device_slot_state_project"), ["project"]),
        (op.f("ix_device_slot_state_printer_name"), ["printer_name"]),
        (op.f("ix_device_slot_state_slot"), ["slot"]),
        (op.f("ix_device_slot_state_observed_at"), ["observed_at"]),
    ]:
        if index_name not in existing_indexes:
            op.create_index(index_name, "device_slot_state", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "device_slot_state" in existing_tables:
        existing_indexes = {idx.get("name") for idx in inspector.get_indexes("device_slot_state")}
        for index_name in [
            op.f("ix_device_slot_state_observed_at"),
            op.f("ix_device_slot_state_slot"),
            op.f("ix_device_slot_state_printer_name"),
            op.f("ix_device_slot_state_project"),
            op.f("ix_device_slot_state_id"),
        ]:
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="device_slot_state")
        op.drop_table("device_slot_state")
