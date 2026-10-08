from datetime import UTC

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Approval, Base, User
from app.db.session import db_check


async def test_db_check(engine):
    await db_check(engine)


async def test_tests_laufen_gegen_echtes_postgresql(engine):
    async with engine.connect() as verbindung:
        version = await verbindung.scalar(sa.text("select version()"))
    assert version.startswith("PostgreSQL")


async def test_zeitstempel_sind_utc(session_fabrik):
    async with session_fabrik() as session:
        user = User(telegram_id=1, anzeigename="Test")
        session.add(user)
        await session.flush()
        session.add(Approval(user_id=user.id, tool_name="t", parameter={}, vorschau_text="v"))
        await session.commit()
    async with session_fabrik() as session:
        approval = (await session.execute(select(Approval))).scalar_one()
    assert approval.status == "offen"
    assert approval.erstellt_am.tzinfo == UTC


async def test_migration_entspricht_modellen(pg_url):
    """Die über Alembic aufgebaute Datenbank hat genau die Tabellen und Spalten der Modelle."""
    engine = create_async_engine(pg_url)
    async with engine.connect() as verbindung:
        tabellen, spalten = await verbindung.run_sync(
            lambda sync: (
                set(sa.inspect(sync).get_table_names()),
                {
                    name: {s["name"] for s in sa.inspect(sync).get_columns(name)}
                    for name in sa.inspect(sync).get_table_names()
                },
            )
        )
    await engine.dispose()
    assert tabellen - {"alembic_version"} == set(Base.metadata.tables)
    for name, tabelle in Base.metadata.tables.items():
        assert spalten[name] == set(tabelle.columns.keys()), name
