"""Add user authentication tables

Revision ID: 20260223_000006
Revises: 20260223_000005
Create Date: 2026-02-23 00:00:06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260223_000006"
down_revision: Union[str, None] = "20260223_000005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    try:
        return {idx.get("name") for idx in inspector.get_indexes(table_name)}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "users" not in existing_tables:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("display_name", sa.String(length=120), nullable=True),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email", name="uq_users_email"),
        )

    user_indexes = _index_names(inspector, "users")
    for index_name, columns in [
        (op.f("ix_users_id"), ["id"]),
        (op.f("ix_users_email"), ["email"]),
    ]:
        if index_name not in user_indexes:
            op.create_index(index_name, "users", columns, unique=False)

    if "user_sessions" not in existing_tables:
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("user_agent", sa.String(length=255), nullable=True),
            sa.Column("ip_address", sa.String(length=120), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_sessions_user_id"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        )

    session_indexes = _index_names(inspector, "user_sessions")
    for index_name, columns in [
        (op.f("ix_user_sessions_id"), ["id"]),
        (op.f("ix_user_sessions_user_id"), ["user_id"]),
        (op.f("ix_user_sessions_token_hash"), ["token_hash"]),
        (op.f("ix_user_sessions_expires_at"), ["expires_at"]),
    ]:
        if index_name not in session_indexes:
            op.create_index(index_name, "user_sessions", columns, unique=False)

    if "user_api_tokens" not in existing_tables:
        op.create_table(
            "user_api_tokens",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False, server_default="default"),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_api_tokens_user_id"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_user_api_tokens_token_hash"),
        )

    token_indexes = _index_names(inspector, "user_api_tokens")
    for index_name, columns in [
        (op.f("ix_user_api_tokens_id"), ["id"]),
        (op.f("ix_user_api_tokens_user_id"), ["user_id"]),
        (op.f("ix_user_api_tokens_token_hash"), ["token_hash"]),
    ]:
        if index_name not in token_indexes:
            op.create_index(index_name, "user_api_tokens", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "user_api_tokens" in existing_tables:
        token_indexes = _index_names(inspector, "user_api_tokens")
        for index_name in [
            op.f("ix_user_api_tokens_token_hash"),
            op.f("ix_user_api_tokens_user_id"),
            op.f("ix_user_api_tokens_id"),
        ]:
            if index_name in token_indexes:
                op.drop_index(index_name, table_name="user_api_tokens")
        op.drop_table("user_api_tokens")

    if "user_sessions" in existing_tables:
        session_indexes = _index_names(inspector, "user_sessions")
        for index_name in [
            op.f("ix_user_sessions_expires_at"),
            op.f("ix_user_sessions_token_hash"),
            op.f("ix_user_sessions_user_id"),
            op.f("ix_user_sessions_id"),
        ]:
            if index_name in session_indexes:
                op.drop_index(index_name, table_name="user_sessions")
        op.drop_table("user_sessions")

    if "users" in existing_tables:
        user_indexes = _index_names(inspector, "users")
        for index_name in [
            op.f("ix_users_email"),
            op.f("ix_users_id"),
        ]:
            if index_name in user_indexes:
                op.drop_index(index_name, table_name="users")
        op.drop_table("users")
