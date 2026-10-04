import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base

target_metadata = Base.metadata


def _migrationen_ausfuehren(verbindung: Connection) -> None:
    context.configure(connection=verbindung, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as verbindung:
        await verbindung.run_sync(_migrationen_ausfuehren)
    await engine.dispose()


def _offline() -> None:
    context.configure(
        url=os.environ["DATABASE_URL"], target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _offline()
else:
    asyncio.run(_online())
