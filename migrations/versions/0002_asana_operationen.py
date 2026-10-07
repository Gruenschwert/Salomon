"""Ausführungsstand von Asana-Änderungssätzen: asana_operationen

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asana_operationen",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("approval_id", sa.Integer(), sa.ForeignKey("approvals.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("art", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("gid", sa.String(40), nullable=True),
        sa.Column("fehler", sa.Text(), nullable=True),
        sa.Column("ausgefuehrt_am", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("approval_id", "position"),
    )
    op.create_index("ix_asana_operationen_approval_id", "asana_operationen", ["approval_id"])


def downgrade() -> None:
    op.drop_index("ix_asana_operationen_approval_id", table_name="asana_operationen")
    op.drop_table("asana_operationen")
