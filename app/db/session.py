"""Engine, Session-Fabrik, DB-Check und die Sitzung mit Nutzerkontext."""

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

SessionFabrik = async_sessionmaker[AsyncSession]

# Datenbank-Rolle des laufenden Bots: kein Superuser, kein BYPASSRLS, kein Tabellenbesitz.
LAUFZEITROLLE = "app_laufzeit"
_INFO_SCHLUESSEL = "nutzer_id"


def erstelle_engine(database_url: str, laufzeitrolle: bool = True, **optionen) -> AsyncEngine:
    """Engine des Bots. Jede Verbindung wechselt beim Aufbau in die Laufzeitrolle, damit Row
    Level Security greift, auch wenn die Zugangsdaten dem Eigentümer der Tabellen gehören."""
    if laufzeitrolle:
        optionen["connect_args"] = {"server_settings": {"role": LAUFZEITROLLE}}
    optionen.setdefault("pool_pre_ping", True)
    return create_async_engine(database_url, **optionen)


def erstelle_session_fabrik(engine: AsyncEngine) -> SessionFabrik:
    return async_sessionmaker(engine, expire_on_commit=False)


def db_sitzung(session_fabrik: SessionFabrik, nutzer: object) -> AsyncSession:
    """Die einzige Stelle, über die persönliche Daten gelesen und geschrieben werden.

    `nutzer` ist der NutzerKontext (oder dessen ID). Jede Transaktion dieser Sitzung setzt als
    Erstes `app.current_user_id`; die Richtlinien der Datenbank lassen dann nur die Zeilen
    dieser Person durch. Benutzung: `async with db_sitzung(fabrik, kontext) as session:`.
    """
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    if not isinstance(nutzer_id, int) or isinstance(nutzer_id, bool):
        raise TypeError("db_sitzung braucht einen NutzerKontext oder eine Nutzer-ID")
    return session_fabrik(info={_INFO_SCHLUESSEL: nutzer_id})


@event.listens_for(Session, "after_begin")
def _setze_nutzer(
    session: Session, transaktion: SessionTransaction, verbindung: Connection
) -> None:
    """Entspricht `SET LOCAL app.current_user_id = <id>` zu Beginn jeder Transaktion."""
    nutzer_id = session.info.get(_INFO_SCHLUESSEL)
    if nutzer_id is not None:
        verbindung.execute(
            text("SELECT set_config('app.current_user_id', :nutzer_id, true)"),
            {"nutzer_id": str(nutzer_id)},
        )


async def db_check(engine: AsyncEngine) -> None:
    """Wirft eine Ausnahme, wenn die Datenbank nicht erreichbar ist."""
    async with engine.connect() as verbindung:
        await verbindung.execute(text("SELECT 1"))
