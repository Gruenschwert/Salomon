"""Verweise auf Dateien aus dem Chat: telegram_dateien

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_dateien",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("file_id", sa.String(300), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("medientyp", sa.String(100), nullable=False),
        sa.Column("groesse", sa.BigInteger(), nullable=True),
        sa.Column("erstellt_am", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_telegram_dateien_user_id", "telegram_dateien", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_telegram_dateien_user_id", table_name="telegram_dateien")
    op.drop_table("telegram_dateien")
