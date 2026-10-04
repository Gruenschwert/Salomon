"""Erste Migration: users, messages, approvals, audit_log, usage, notizen

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("rolle", sa.String(20), nullable=False),
        sa.Column("aktiv", sa.Boolean(), nullable=False),
        sa.Column("erstellt_am", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("rolle", sa.String(20), nullable=False),
        sa.Column("inhalt", sa.JSON(), nullable=False),
        sa.Column("zeit", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("parameter", sa.JSON(), nullable=False),
        sa.Column("vorschau_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("erstellt_am", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entschieden_am", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("zeit", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("parameter", sa.JSON(), nullable=False),
        sa.Column("ergebnis_kurz", sa.Text(), nullable=False),
        sa.Column("dauer_ms", sa.Integer(), nullable=False),
        sa.Column("fehler", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_log_zeit", "audit_log", ["zeit"])
    op.create_table(
        "usage",
        sa.Column("datum", sa.Date(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("kosten_eur", sa.Numeric(12, 6), nullable=False),
    )
    op.create_table(
        "notizen",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("erstellt_am", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notizen")
    op.drop_table("usage")
    op.drop_index("ix_audit_log_zeit", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_approvals_status", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_messages_chat_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("users")
