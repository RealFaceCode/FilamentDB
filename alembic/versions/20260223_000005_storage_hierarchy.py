"""Add hierarchical storage tables and spool location reference

Revision ID: 20260223_000005
Revises: 20260221_000004
Create Date: 2026-02-23 00:00:05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260223_000005"
down_revision: Union[str, None] = "20260221_000004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    try:
        return {idx.get("name") for idx in inspector.get_indexes(table_name)}
    except Exception:
        return set()


def _foreign_key_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    try:
        return {fk.get("name") for fk in inspector.get_foreign_keys(table_name) if fk.get("name")}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "storage_areas" not in existing_tables:
        op.create_table(
            "storage_areas",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project", "code", name="uq_storage_areas_project_code"),
        )

    area_indexes = _index_names(inspector, "storage_areas")
    for index_name, columns in [
        (op.f("ix_storage_areas_id"), ["id"]),
        (op.f("ix_storage_areas_project"), ["project"]),
        (op.f("ix_storage_areas_code"), ["code"]),
    ]:
        if index_name not in area_indexes:
            op.create_index(index_name, "storage_areas", columns, unique=False)

    if "storage_sub_locations" not in existing_tables:
        op.create_table(
            "storage_sub_locations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("area_id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("path_code", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["area_id"], ["storage_areas.id"], name="fk_storage_sub_locations_area_id"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project", "area_id", "code", name="uq_storage_sub_locations_project_area_code"),
            sa.UniqueConstraint("project", "path_code", name="uq_storage_sub_locations_project_path"),
        )

    sub_indexes = _index_names(inspector, "storage_sub_locations")
    for index_name, columns in [
        (op.f("ix_storage_sub_locations_id"), ["id"]),
        (op.f("ix_storage_sub_locations_project"), ["project"]),
        (op.f("ix_storage_sub_locations_area_id"), ["area_id"]),
        (op.f("ix_storage_sub_locations_code"), ["code"]),
        (op.f("ix_storage_sub_locations_path_code"), ["path_code"]),
    ]:
        if index_name not in sub_indexes:
            op.create_index(index_name, "storage_sub_locations", columns, unique=False)

    spool_columns = {column.get("name") for column in inspector.get_columns("spools")}
    if "storage_sub_location_id" not in spool_columns:
        with op.batch_alter_table("spools") as batch_op:
            batch_op.add_column(sa.Column("storage_sub_location_id", sa.Integer(), nullable=True))

    spool_indexes = _index_names(inspector, "spools")
    spool_location_index = op.f("ix_spools_storage_sub_location_id")
    if spool_location_index not in spool_indexes:
        op.create_index(spool_location_index, "spools", ["storage_sub_location_id"], unique=False)

    spool_foreign_keys = _foreign_key_names(inspector, "spools")
    fk_name = "fk_spools_storage_sub_location_id"
    if fk_name not in spool_foreign_keys:
        with op.batch_alter_table("spools") as batch_op:
            batch_op.create_foreign_key(
                fk_name,
                "storage_sub_locations",
                ["storage_sub_location_id"],
                ["id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "spools" in existing_tables:
        spool_indexes = _index_names(inspector, "spools")
        spool_location_index = op.f("ix_spools_storage_sub_location_id")
        if spool_location_index in spool_indexes:
            op.drop_index(spool_location_index, table_name="spools")

        spool_columns = {column.get("name") for column in inspector.get_columns("spools")}
        spool_foreign_keys = _foreign_key_names(inspector, "spools")
        fk_name = "fk_spools_storage_sub_location_id"
        with op.batch_alter_table("spools") as batch_op:
            if fk_name in spool_foreign_keys:
                batch_op.drop_constraint(fk_name, type_="foreignkey")
            if "storage_sub_location_id" in spool_columns:
                batch_op.drop_column("storage_sub_location_id")

    if "storage_sub_locations" in existing_tables:
        sub_indexes = _index_names(inspector, "storage_sub_locations")
        for index_name in [
            op.f("ix_storage_sub_locations_path_code"),
            op.f("ix_storage_sub_locations_code"),
            op.f("ix_storage_sub_locations_area_id"),
            op.f("ix_storage_sub_locations_project"),
            op.f("ix_storage_sub_locations_id"),
        ]:
            if index_name in sub_indexes:
                op.drop_index(index_name, table_name="storage_sub_locations")
        op.drop_table("storage_sub_locations")

    if "storage_areas" in existing_tables:
        area_indexes = _index_names(inspector, "storage_areas")
        for index_name in [
            op.f("ix_storage_areas_code"),
            op.f("ix_storage_areas_project"),
            op.f("ix_storage_areas_id"),
        ]:
            if index_name in area_indexes:
                op.drop_index(index_name, table_name="storage_areas")
        op.drop_table("storage_areas")
