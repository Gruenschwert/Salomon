"""Engine, Session-Fabrik und DB-Check."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SessionFabrik = async_sessionmaker[AsyncSession]


def erstelle_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def erstelle_session_fabrik(engine: AsyncEngine) -> SessionFabrik:
    return async_sessionmaker(engine, expire_on_commit=False)


async def db_check(engine: AsyncEngine) -> None:
    """Wirft eine Ausnahme, wenn die Datenbank nicht erreichbar ist."""
    async with engine.connect() as verbindung:
        await verbindung.execute(text("SELECT 1"))
