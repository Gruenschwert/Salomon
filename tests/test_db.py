from datetime import UTC

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.db.models import Approval, Base, User
from app.db.session import db_check


async def test_db_check(engine):
    await db_check(engine)


async def test_zeitstempel_sind_utc(session_fabrik):
    async with session_fabrik() as session:
        user = User(telegram_id=1, name="Test")
        session.add(user)
        await session.flush()
        session.add(Approval(user_id=user.id, tool_name="t", parameter={}, vorschau_text="v"))
        await session.commit()
    async with session_fabrik() as session:
        approval = (await session.execute(select(Approval))).scalar_one()
    assert approval.status == "offen"
    assert approval.erstellt_am.tzinfo == UTC


def test_migration_entspricht_modellen(tmp_path, monkeypatch):
    db_datei = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_datei.as_posix()}")
    command.upgrade(Config("alembic.ini"), "head")

    engine = sa.create_engine(f"sqlite:///{db_datei.as_posix()}")
    inspektor = sa.inspect(engine)
    tabellen = set(inspektor.get_table_names()) - {"alembic_version"}
    assert tabellen == set(Base.metadata.tables)
    for name, tabelle in Base.metadata.tables.items():
        spalten = {spalte["name"] for spalte in inspektor.get_columns(name)}
        assert spalten == set(tabelle.columns.keys()), name
    engine.dispose()
