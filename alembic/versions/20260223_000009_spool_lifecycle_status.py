"""Add lifecycle status to spools

Revision ID: 20260223_000009
Revises: 20260223_000008
Create Date: 2026-02-23 00:00:09

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260223_000009"
down_revision: Union[str, None] = "20260223_000008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("spools") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_status",
                sa.String(length=32),
                nullable=False,
                server_default="new",
            )
        )
        batch_op.create_index("ix_spools_lifecycle_status", ["lifecycle_status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("spools") as batch_op:
        batch_op.drop_index("ix_spools_lifecycle_status")
        batch_op.drop_column("lifecycle_status")
