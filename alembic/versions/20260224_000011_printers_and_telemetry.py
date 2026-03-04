"""Add printers table and printer linkage columns

Revision ID: 20260224_000011
Revises: 20260223_000010
Create Date: 2026-02-24 00:00:11

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260224_000011"
down_revision: Union[str, None] = "20260223_000010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("printers"):
        op.create_table(
            "printers",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("serial", sa.String(length=120), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("host", sa.String(length=255), nullable=True),
            sa.Column("access_code", sa.String(length=120), nullable=True),
            sa.Column("port", sa.Integer(), nullable=False, server_default="8883"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("status", sa.String(length=32), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("last_source", sa.String(length=120), nullable=True),
            sa.Column("telemetry_job_name", sa.String(length=255), nullable=True),
            sa.Column("telemetry_job_status", sa.String(length=80), nullable=True),
            sa.Column("telemetry_progress", sa.Float(), nullable=True),
            sa.Column("telemetry_nozzle_temp", sa.Float(), nullable=True),
            sa.Column("telemetry_bed_temp", sa.Float(), nullable=True),
            sa.Column("telemetry_chamber_temp", sa.Float(), nullable=True),
            sa.Column("telemetry_firmware", sa.String(length=120), nullable=True),
            sa.Column("telemetry_error", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project", "name", name="uq_printers_project_name"),
            sa.UniqueConstraint("project", "serial", name="uq_printers_project_serial"),
        )
        inspector = sa.inspect(bind)

    printer_indexes = {item.get("name") for item in inspector.get_indexes("printers")}
    if "ix_printers_id" not in printer_indexes:
        op.create_index("ix_printers_id", "printers", ["id"], unique=False)
    if "ix_printers_project" not in printer_indexes:
        op.create_index("ix_printers_project", "printers", ["project"], unique=False)
    if "ix_printers_serial" not in printer_indexes:
        op.create_index("ix_printers_serial", "printers", ["serial"], unique=False)
    if "ix_printers_name" not in printer_indexes:
        op.create_index("ix_printers_name", "printers", ["name"], unique=False)
    if "ix_printers_is_active" not in printer_indexes:
        op.create_index("ix_printers_is_active", "printers", ["is_active"], unique=False)
    if "ix_printers_status" not in printer_indexes:
        op.create_index("ix_printers_status", "printers", ["status"], unique=False)
    if "ix_printers_last_seen_at" not in printer_indexes:
        op.create_index("ix_printers_last_seen_at", "printers", ["last_seen_at"], unique=False)

    slot_columns = {col.get("name") for col in inspector.get_columns("device_slot_state")}
    if "printer_serial" not in slot_columns:
        with op.batch_alter_table("device_slot_state") as batch_op:
            batch_op.add_column(sa.Column("printer_serial", sa.String(length=120), nullable=True))

    slot_indexes = {item.get("name") for item in inspector.get_indexes("device_slot_state")}
    if "ix_device_slot_state_printer_serial" not in slot_indexes:
        with op.batch_alter_table("device_slot_state") as batch_op:
            batch_op.create_index("ix_device_slot_state_printer_serial", ["printer_serial"], unique=False)

    usage_columns = {col.get("name") for col in inspector.get_columns("usage_batch_context")}
    if "printer_serial" not in usage_columns:
        with op.batch_alter_table("usage_batch_context") as batch_op:
            batch_op.add_column(sa.Column("printer_serial", sa.String(length=120), nullable=True))

    usage_indexes = {item.get("name") for item in inspector.get_indexes("usage_batch_context")}
    if "ix_usage_batch_context_printer_serial" not in usage_indexes:
        with op.batch_alter_table("usage_batch_context") as batch_op:
            batch_op.create_index("ix_usage_batch_context_printer_serial", ["printer_serial"], unique=False)

    op.execute(
        """
        DELETE FROM printers
        WHERE serial IS NULL OR TRIM(serial) = '' OR name IS NULL OR TRIM(name) = ''
        """
    )
    op.execute("UPDATE printers SET port = 8883 WHERE port IS NULL")
    op.execute("UPDATE printers SET is_active = true WHERE is_active IS NULL")
    op.execute("UPDATE printers SET status = 'unknown' WHERE status IS NULL OR TRIM(status) = ''")

    op.execute(
        """
        INSERT INTO printers (project, serial, name, port, is_active, status, created_at, updated_at)
        SELECT DISTINCT s.project,
               SUBSTRING(TRIM(s.ams_printer) FROM 1 FOR 120),
               TRIM(s.ams_printer),
               8883,
               true,
               'unknown',
               NOW(),
               NOW()
        FROM spools s
        WHERE s.ams_printer IS NOT NULL
          AND TRIM(s.ams_printer) <> ''
          AND NOT EXISTS (
              SELECT 1 FROM printers p
              WHERE p.project = s.project
                AND p.name = TRIM(s.ams_printer)
          )
        """
    )

    op.execute(
        """
         INSERT INTO printers (project, serial, name, port, is_active, status, created_at, updated_at)
        SELECT DISTINCT d.project,
               SUBSTRING(TRIM(d.printer_name) FROM 1 FOR 120),
               TRIM(d.printer_name),
             8883,
               true,
               'unknown',
               NOW(),
               NOW()
        FROM device_slot_state d
        WHERE d.printer_name IS NOT NULL
          AND TRIM(d.printer_name) <> ''
          AND NOT EXISTS (
              SELECT 1 FROM printers p
              WHERE p.project = d.project
                AND p.name = TRIM(d.printer_name)
          )
        """
    )

    op.execute(
        """
        UPDATE device_slot_state d
           SET printer_serial = p.serial
          FROM printers p
         WHERE p.project = d.project
           AND p.name = d.printer_name
           AND (d.printer_serial IS NULL OR TRIM(d.printer_serial) = '')
        """
    )

    op.execute(
        """
        UPDATE usage_batch_context u
           SET printer_serial = p.serial
          FROM printers p
         WHERE p.project = u.project
           AND p.name = u.printer_name
           AND (u.printer_serial IS NULL OR TRIM(u.printer_serial) = '')
        """
    )

    with op.batch_alter_table("printers") as batch_op:
        batch_op.alter_column("port", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("usage_batch_context") as batch_op:
        batch_op.drop_index("ix_usage_batch_context_printer_serial")
        batch_op.drop_column("printer_serial")

    with op.batch_alter_table("device_slot_state") as batch_op:
        batch_op.drop_index("ix_device_slot_state_printer_serial")
        batch_op.drop_column("printer_serial")

    op.drop_index("ix_printers_last_seen_at", table_name="printers")
    op.drop_index("ix_printers_status", table_name="printers")
    op.drop_index("ix_printers_is_active", table_name="printers")
    op.drop_index("ix_printers_name", table_name="printers")
    op.drop_index("ix_printers_serial", table_name="printers")
    op.drop_index("ix_printers_project", table_name="printers")
    op.drop_index("ix_printers_id", table_name="printers")
    op.drop_table("printers")
