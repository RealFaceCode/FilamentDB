"""Add audit logs and import mapping profiles

Revision ID: 20260303_000015
Revises: 20260302_000014
Create Date: 2026-03-03 00:00:15

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260303_000015"
down_revision: Union[str, None] = "20260302_000014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "audit_logs" not in existing_tables:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("actor", sa.String(length=120), nullable=True),
            sa.Column("action", sa.String(length=80), nullable=False),
            sa.Column("entity_type", sa.String(length=80), nullable=True),
            sa.Column("entity_id", sa.String(length=120), nullable=True),
            sa.Column("details_json", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_audit_logs_id", "audit_logs", ["id"], unique=False)
        op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)
        op.create_index("ix_audit_logs_project", "audit_logs", ["project"], unique=False)
        op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
        op.create_index("ix_audit_logs_entity_type", "audit_logs", ["entity_type"], unique=False)
        op.create_index("ix_audit_logs_entity_id", "audit_logs", ["entity_id"], unique=False)

    if "import_mapping_profiles" not in existing_tables:
        op.create_table(
            "import_mapping_profiles",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("mapping_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project", "name", name="uq_import_mapping_profiles_project_name"),
        )
        op.create_index("ix_import_mapping_profiles_id", "import_mapping_profiles", ["id"], unique=False)
        op.create_index("ix_import_mapping_profiles_project", "import_mapping_profiles", ["project"], unique=False)
        op.create_index("ix_import_mapping_profiles_name", "import_mapping_profiles", ["name"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "import_mapping_profiles" in existing_tables:
        for index_name in (
            "ix_import_mapping_profiles_name",
            "ix_import_mapping_profiles_project",
            "ix_import_mapping_profiles_id",
        ):
            op.drop_index(index_name, table_name="import_mapping_profiles")
        op.drop_table("import_mapping_profiles")

    if "audit_logs" in existing_tables:
        for index_name in (
            "ix_audit_logs_entity_id",
            "ix_audit_logs_entity_type",
            "ix_audit_logs_action",
            "ix_audit_logs_project",
            "ix_audit_logs_created_at",
            "ix_audit_logs_id",
        ):
            op.drop_index(index_name, table_name="audit_logs")
        op.drop_table("audit_logs")
