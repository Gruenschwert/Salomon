"""Mehrbenutzer: Rollen, Zugangsdaten, Notizen, Verbrauch je Modellantwort

Bestehende Daten bleiben erhalten: `users.rolle` wandert nach `user_roles`, die bisherigen
Tagessummen in `usage` werden als je eine Zeile übernommen.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ROLLEN = {
    "admin": "Verwaltung von Nutzern, Rollen und Kosten",
    "mitarbeiter": "Standardrolle für Mitarbeiter",
    "buchhaltung": "Buchhaltung",
    "apotheken_updates": "Apotheken-Scan und Sortenabgleich",
}


def upgrade() -> None:
    roles = op.create_table(
        "roles",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("beschreibung", sa.String(200), nullable=False),
    )
    op.bulk_insert(roles, [{"name": n, "beschreibung": b} for n, b in ROLLEN.items()])
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role_name", sa.String(40), sa.ForeignKey("roles.name"), primary_key=True),
        sa.Column("vergeben_von", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("vergeben_am", sa.DateTime(timezone=True), nullable=False),
    )

    op.alter_column("users", "name", new_column_name="anzeigename")
    op.add_column(
        "users",
        sa.Column("zeitzone", sa.String(60), nullable=False, server_default="Europe/Berlin"),
    )
    op.add_column("users", sa.Column("ton", sa.String(10), nullable=False, server_default="du"))
    op.add_column("users", sa.Column("tageslimit_eur", sa.Numeric(12, 2), nullable=True))
    op.add_column("users", sa.Column("gesperrt_am", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("users", "zeitzone", server_default=None)
    op.alter_column("users", "ton", server_default=None)
    # Die bisherige Rolle jedes Nutzers übernehmen: admin bleibt admin, alle anderen werden
    # Mitarbeiter.
    op.execute(
        """
        INSERT INTO user_roles (user_id, role_name, vergeben_am)
        SELECT id, CASE WHEN rolle = 'admin' THEN 'admin' ELSE 'mitarbeiter' END, now()
        FROM users
        """
    )
    op.drop_column("users", "rolle")

    op.create_table(
        "user_secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("dienst", sa.String(40), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("schluessel_version", sa.Integer(), nullable=False),
        sa.Column("erstellt_am", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aktualisiert_am", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "dienst"),
    )
    op.create_index("ix_user_secrets_user_id", "user_secrets", ["user_id"])
    op.create_table(
        "user_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("inhalt", sa.Text(), nullable=False),
        sa.Column("erstellt_am", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_memory_user_id", "user_memory", ["user_id"])
    op.create_table(
        "system_einstellungen",
        sa.Column("schluessel", sa.String(60), primary_key=True),
        sa.Column("wert", sa.Text(), nullable=False),
    )

    # usage: bisher eine Summe je Tag und Nutzer, künftig eine Zeile je Modellantwort.
    op.rename_table("usage", "usage_alt")
    op.create_table(
        "usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("zeit", sa.DateTime(timezone=True), nullable=False),
        sa.Column("datum", sa.Date(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("modell", sa.String(80), nullable=False),
        sa.Column("eingabe_tokens", sa.BigInteger(), nullable=False),
        sa.Column("ausgabe_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_lese_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_schreib_tokens", sa.BigInteger(), nullable=False),
        sa.Column("kosten_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("kosten_eur", sa.Numeric(12, 6), nullable=False),
        sa.Column("grund_modellwahl", sa.String(200), nullable=False),
    )
    op.create_index("ix_usage_datum", "usage", ["datum"])
    op.create_index("ix_usage_user_id", "usage", ["user_id"])
    op.execute(
        """
        INSERT INTO usage (zeit, datum, user_id, modell, eingabe_tokens, ausgabe_tokens,
                           cache_lese_tokens, cache_schreib_tokens, kosten_usd, kosten_eur,
                           grund_modellwahl)
        SELECT datum::timestamptz, datum, user_id, '', input_tokens, output_tokens,
               0, 0, 0, kosten_eur, 'übernommen (Tagessumme)'
        FROM usage_alt
        """
    )
    op.drop_table("usage_alt")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE usage_alt AS
        SELECT datum, user_id, sum(eingabe_tokens)::bigint AS input_tokens,
               sum(ausgabe_tokens)::bigint AS output_tokens, sum(kosten_eur) AS kosten_eur
        FROM usage GROUP BY datum, user_id
        """
    )
    op.drop_index("ix_usage_user_id", table_name="usage")
    op.drop_index("ix_usage_datum", table_name="usage")
    op.drop_table("usage")
    op.rename_table("usage_alt", "usage")
    op.execute("ALTER TABLE usage ADD PRIMARY KEY (datum, user_id)")
    op.drop_table("system_einstellungen")
    op.drop_index("ix_user_memory_user_id", table_name="user_memory")
    op.drop_table("user_memory")
    op.drop_index("ix_user_secrets_user_id", table_name="user_secrets")
    op.drop_table("user_secrets")
    op.add_column("users", sa.Column("rolle", sa.String(20), nullable=False, server_default="user"))
    op.execute(
        "UPDATE users SET rolle = 'admin' WHERE id IN "
        "(SELECT user_id FROM user_roles WHERE role_name = 'admin')"
    )
    op.drop_column("users", "gesperrt_am")
    op.drop_column("users", "tageslimit_eur")
    op.drop_column("users", "ton")
    op.drop_column("users", "zeitzone")
    op.alter_column("users", "anzeigename", new_column_name="name")
    op.drop_table("user_roles")
    op.drop_table("roles")
