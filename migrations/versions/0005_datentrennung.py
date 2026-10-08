"""Datentrennung in der Datenbank: Laufzeitrolle und Row Level Security

Der Bot arbeitet als `app_laufzeit` (kein Superuser, kein BYPASSRLS, kein Tabellenbesitz).
Auf allen Tabellen mit persönlichen Daten lässt die Datenbank nur die Zeilen der Person durch,
deren ID in `app.current_user_id` steht. Ohne gesetzten Wert sind es null Zeilen.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

LAUFZEITROLLE = "app_laufzeit"
# Tabellen mit persönlichen Daten und einer Spalte user_id
PERSOENLICH = (
    "messages",
    "user_secrets",
    "user_memory",
    "approvals",
    "notizen",
    "telegram_dateien",
)
# NULLIF: Nach dem Ende einer Transaktion ist der Wert leer, nicht NULL. Beides ergibt NULL
# und damit keine Zeile, statt eines Fehlers.
AKTUELLER_NUTZER = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"


def upgrade() -> None:
    # Rollen gelten für den ganzen Server; die Rolle kann also schon da sein.
    op.execute(
        f"""
        DO $$
        BEGIN
            CREATE ROLE {LAUFZEITROLLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE {LAUFZEITROLLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        END
        $$
        """
    )
    # Der Eigentümer darf in die Laufzeitrolle wechseln (SET ROLE beim Verbindungsaufbau).
    op.execute(f"GRANT {LAUFZEITROLLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {LAUFZEITROLLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {LAUFZEITROLLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {LAUFZEITROLLE}")
    op.execute(f"REVOKE ALL ON alembic_version FROM {LAUFZEITROLLE}")
    # Stammdaten der Rollen ändert nur eine Migration.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON roles FROM {LAUFZEITROLLE}")
    # Künftige Tabellen bekommen dieselben Rechte; ihre Richtlinie legt die jeweilige Migration an.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {LAUFZEITROLLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {LAUFZEITROLLE}"
    )

    for tabelle in PERSOENLICH:
        op.execute(f"ALTER TABLE {tabelle} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabelle} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY nutzer_trennung ON {tabelle} "
            f"USING (user_id = {AKTUELLER_NUTZER}) WITH CHECK (user_id = {AKTUELLER_NUTZER})"
        )

    # Der Ausführungsstand eines Änderungssatzes gehört zur Freigabe und damit zu deren Person.
    op.execute("ALTER TABLE asana_operationen ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE asana_operationen FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY nutzer_trennung ON asana_operationen "
        "USING (approval_id IN (SELECT id FROM approvals)) "
        "WITH CHECK (approval_id IN (SELECT id FROM approvals))"
    )

    # Audit-Log: Jede Person liest nur ihre Einträge. Einträge ohne Person (unbekannte
    # Telegram-Nutzer) lassen sich schreiben, aber von niemandem über den Bot lesen.
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY nutzer_lesen ON audit_log FOR SELECT USING (user_id = {AKTUELLER_NUTZER})"
    )
    op.execute(
        "CREATE POLICY nutzer_schreiben ON audit_log FOR INSERT "
        f"WITH CHECK (user_id IS NULL OR user_id = {AKTUELLER_NUTZER})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY nutzer_schreiben ON audit_log")
    op.execute("DROP POLICY nutzer_lesen ON audit_log")
    for tabelle in ("audit_log", "asana_operationen", *PERSOENLICH):
        if tabelle not in ("audit_log",):
            op.execute(f"DROP POLICY nutzer_trennung ON {tabelle}")
        op.execute(f"ALTER TABLE {tabelle} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabelle} DISABLE ROW LEVEL SECURITY")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {LAUFZEITROLLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {LAUFZEITROLLE}"
    )
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {LAUFZEITROLLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {LAUFZEITROLLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {LAUFZEITROLLE}")
