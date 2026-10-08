"""Mail: mehrere Zugänge je Dienst (`label`) und verschlüsselter Mailinhalt im Verlauf

`user_secrets` bekommt die Spalte `label`; bestehende Zeilen (Asana) heißen `standard`. Damit
kann eine Person mehrere Postfächer verbinden. `messages` bekommt `inhalt_verschluesselt` für
Mailinhalt, der nur für kurze Zeit und nur verschlüsselt im Verlauf liegt.

Die Row Level Security beider Tabellen bleibt unverändert bestehen.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_secrets",
        sa.Column("label", sa.String(60), nullable=False, server_default="standard"),
    )
    # Der Name der bisherigen Eindeutigkeit stammt von PostgreSQL; IF EXISTS hält die
    # Migration robust, falls er auf einem Server anders lautet.
    op.execute("ALTER TABLE user_secrets DROP CONSTRAINT IF EXISTS user_secrets_user_id_dienst_key")
    op.create_unique_constraint(
        "uq_user_secrets_user_dienst_label", "user_secrets", ["user_id", "dienst", "label"]
    )
    op.add_column("messages", sa.Column("inhalt_verschluesselt", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "inhalt_verschluesselt")
    op.drop_constraint("uq_user_secrets_user_dienst_label", "user_secrets", type_="unique")
    # Zurück geht es nur mit einem Zugang je Dienst; weitere Postfächer entfallen.
    op.execute("DELETE FROM user_secrets WHERE label <> 'standard'")
    op.create_unique_constraint(
        "user_secrets_user_id_dienst_key", "user_secrets", ["user_id", "dienst"]
    )
    op.drop_column("user_secrets", "label")
