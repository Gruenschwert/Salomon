"""Übernahme des Bestands: Migration ohne Datenverlust und Whitelist aus der .env."""

import asyncio

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.users import aktive_admins, finde_erlaubten_nutzer, rollen_von, uebernehme_bestand
from app.db.models import ROLLEN, Role, SystemEinstellung, User, UserRole
from tests.conftest import ADMIN_ID, ERLAUBT_ID, migriere


async def _rollen(session_fabrik) -> dict[int, set[str]]:
    async with session_fabrik() as session:
        zeilen = await session.execute(
            select(User.telegram_id, UserRole.role_name).join(UserRole, UserRole.user_id == User.id)
        )
    ergebnis: dict[int, set[str]] = {}
    for telegram_id, rolle in zeilen:
        ergebnis.setdefault(telegram_id, set()).add(rolle)
    return ergebnis


async def test_rollen_sind_als_stammdaten_angelegt(session_fabrik):
    async with session_fabrik() as session:
        rollen = {r.name: r.beschreibung for r in await session.scalars(select(Role))}
    assert rollen == ROLLEN
    assert set(rollen) == {"admin", "mitarbeiter", "buchhaltung", "apotheken_updates"}


async def test_erste_uebernahme_legt_admin_und_mitarbeiter_an(settings, session_fabrik):
    await uebernehme_bestand(session_fabrik, settings)
    assert await _rollen(session_fabrik) == {ADMIN_ID: {"admin"}, ERLAUBT_ID: {"mitarbeiter"}}
    admin = await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID, "Theis")
    assert admin.anzeigename == "Theis"
    assert (admin.zeitzone, admin.ton, admin.aktiv, admin.gesperrt_am) == (
        "Europe/Berlin",
        "du",
        True,
        None,
    )
    assert [a.telegram_id for a in await aktive_admins(session_fabrik)] == [ADMIN_ID]


async def test_zweiter_start_aendert_nichts(settings, session_fabrik):
    await uebernehme_bestand(session_fabrik, settings)
    await uebernehme_bestand(session_fabrik, settings)
    async with session_fabrik() as session:
        assert len(list(await session.scalars(select(User)))) == 2
        assert len(list(await session.scalars(select(UserRole)))) == 2
        assert len(list(await session.scalars(select(SystemEinstellung)))) == 1


async def test_danach_ist_die_datenbank_die_wahrheit(settings, session_fabrik):
    await uebernehme_bestand(session_fabrik, settings)
    # In der Datenbank geändert: Rolle dazu, Name gesetzt, eine Person gesperrt.
    async with session_fabrik() as session:
        mitarbeiter = await session.scalar(select(User).where(User.telegram_id == ERLAUBT_ID))
        mitarbeiter.aktiv = False
        mitarbeiter.anzeigename = "Lea"
        session.add(UserRole(user_id=mitarbeiter.id, role_name="buchhaltung"))
        await session.commit()

    # Neustart mit geänderter .env: neue Whitelist-ID, die alte fehlt, ein neuer Admin.
    neu = settings.model_copy(
        update={
            "telegram_allowed_user_ids": frozenset({ADMIN_ID, 333, 444}),
            "telegram_admin_user_ids": frozenset({ADMIN_ID, 444}),
        }
    )
    await uebernehme_bestand(session_fabrik, neu)

    rollen = await _rollen(session_fabrik)
    # Nur der fehlende Admin kommt dazu; die neue Nicht-Admin-ID 333 wird nicht mehr angelegt.
    assert rollen == {
        ADMIN_ID: {"admin"},
        ERLAUBT_ID: {"mitarbeiter", "buchhaltung"},
        444: {"admin"},
    }
    # Sperre und Name bleiben, obwohl die ID nicht mehr in der .env steht bzw. wieder stünde.
    assert await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID) is None
    async with session_fabrik() as session:
        mitarbeiter = await session.scalar(select(User).where(User.telegram_id == ERLAUBT_ID))
    assert (mitarbeiter.aktiv, mitarbeiter.anzeigename) == (False, "Lea")
    assert await rollen_von(session_fabrik, mitarbeiter.id) == {"mitarbeiter", "buchhaltung"}


async def test_name_aus_telegram_ist_nur_der_startwert(settings, session_fabrik):
    await uebernehme_bestand(session_fabrik, settings)
    await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID, "Lea aus Telegram")
    user = await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID, "Anderer Name")
    assert user.anzeigename == "Lea aus Telegram"


async def test_migration_uebernimmt_bestehende_nutzer_und_kosten_ohne_verlust(pg_server):
    """Eine Datenbank im Stand vor dem Update (Revision 0003) mit echten Daten wird migriert."""
    basis = make_url(pg_server)
    verwaltung = create_async_engine(pg_server, isolation_level="AUTOCOMMIT")
    async with verwaltung.connect() as verbindung:
        await verbindung.execute(sa.text("DROP DATABASE IF EXISTS migrationstest"))
        await verbindung.execute(sa.text("CREATE DATABASE migrationstest"))
    await verwaltung.dispose()
    url = basis.set(database="migrationstest").render_as_string(False)

    # Alembic startet eine eigene Ereignisschleife, deshalb in einem eigenen Thread.
    await asyncio.to_thread(migriere, url, "0003")
    engine = create_async_engine(url)
    async with engine.begin() as verbindung:
        for anweisung in """
                INSERT INTO users (id, telegram_id, name, rolle, aktiv, erstellt_am) VALUES
                    (1, 222, 'Theis', 'admin', true, now()),
                    (2, 111, 'Lea', 'user', true, now()),
                    (3, 555, 'Alt', 'user', false, now());
                INSERT INTO messages (chat_id, user_id, rolle, inhalt, zeit)
                    VALUES (5, 1, 'user', '"Hallo"', now());
                INSERT INTO approvals (user_id, tool_name, parameter, vorschau_text, status,
                                       erstellt_am)
                    VALUES (1, 'demo_notiz', '{}', 'Notiz', 'offen', now());
                INSERT INTO usage (datum, user_id, input_tokens, output_tokens, kosten_eur) VALUES
                    ('2026-10-07', 1, 1000, 200, 1.25),
                    ('2026-10-08', 1, 3000, 500, 2.50),
                    ('2026-10-08', 2, 10, 20, 0.01);
                """.split(";"):
            if anweisung.strip():
                await verbindung.execute(sa.text(anweisung))
    await engine.dispose()

    await asyncio.to_thread(migriere, url)
    # Mehrfaches Ausführen ist unschädlich.
    await asyncio.to_thread(migriere, url)

    engine = create_async_engine(url)
    async with engine.connect() as verbindung:
        nutzer = (
            await verbindung.execute(
                sa.text(
                    "SELECT telegram_id, anzeigename, aktiv, zeitzone, ton FROM users ORDER BY id"
                )
            )
        ).all()
        rollen = (
            await verbindung.execute(
                sa.text(
                    "SELECT u.telegram_id, r.role_name FROM user_roles r "
                    "JOIN users u ON u.id = r.user_id ORDER BY u.id"
                )
            )
        ).all()
        verbrauch = (
            await verbindung.execute(
                sa.text(
                    "SELECT datum::text, user_id, eingabe_tokens, ausgabe_tokens, "
                    "kosten_eur::float FROM usage ORDER BY datum, user_id"
                )
            )
        ).all()
        nachrichten = await verbindung.scalar(sa.text("SELECT count(*) FROM messages"))
        freigaben = await verbindung.scalar(sa.text("SELECT count(*) FROM approvals"))
        version = await verbindung.scalar(sa.text("SELECT version_num FROM alembic_version"))
    await engine.dispose()

    assert nutzer == [
        (222, "Theis", True, "Europe/Berlin", "du"),
        (111, "Lea", True, "Europe/Berlin", "du"),
        (555, "Alt", False, "Europe/Berlin", "du"),
    ]
    assert rollen == [(222, "admin"), (111, "mitarbeiter"), (555, "mitarbeiter")]
    assert verbrauch == [
        ("2026-10-07", 1, 1000, 200, 1.25),
        ("2026-10-08", 1, 3000, 500, 2.5),
        ("2026-10-08", 2, 10, 20, 0.01),
    ]
    assert (nachrichten, freigaben) == (1, 1)
    assert version >= "0004"
