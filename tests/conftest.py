import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import tests.beispiel_tools
from app.agent.loop import Agent
from app.auth.approvals import Freigaben
from app.auth.tresor import Tresor, speichere_geheimnis
from app.auth.users import finde_erlaubten_nutzer, uebernehme_bestand
from app.config import Settings
from app.db.models import Base
from app.db.session import erstelle_engine, erstelle_session_fabrik
from app.observability.alerts import Alarme
from app.observability.costs import Kosten
from app.tools.base import ToolKontext, aktueller_nutzer
from app.tools.registry import lade_registry
from tests.beispiel_tools.schreibend import BeispielSchreiben

ERLAUBT_ID = 111
ADMIN_ID = 222
FREMD_ID = 999
# 32 Byte, nur für Tests
TEST_HAUPTSCHLUESSEL = "dGVzdC1oYXVwdHNjaGx1ZXNzZWwtMzItYnl0ZXMtISE="
# Tabellen mit festen Stammdaten aus der Migration bleiben zwischen den Tests stehen.
_STAMMDATEN = {"roles", "alembic_version"}


def _sqlalchemy_url(url: str) -> str:
    return make_url(url).set(drivername="postgresql+asyncpg").render_as_string(False)


def migriere(url: str, ziel: str = "head") -> None:
    """Führt die Alembic-Migrationen aus, genau wie der Container beim Start."""
    alt = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config("alembic.ini"), ziel)
    finally:
        if alt is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = alt


@pytest.fixture(scope="session")
def pg_server():
    """Ein echter PostgreSQL-Server für die ganze Testsitzung.

    Mit TEST_DATABASE_URL (z. B. in der CI mit Service-Container) wird dieser Server benutzt,
    sonst startet `pgserver` einen eigenen in einem temporären Verzeichnis.
    """
    if extern := os.environ.get("TEST_DATABASE_URL"):
        yield _sqlalchemy_url(extern)
        return
    import pgserver

    with tempfile.TemporaryDirectory(prefix="gs-pg-") as verzeichnis:
        server = pgserver.get_server(Path(verzeichnis), cleanup_mode="stop")
        try:
            yield _sqlalchemy_url(server.get_uri())
        finally:
            server.cleanup()


@pytest.fixture(scope="session")
def pg_url(pg_server) -> str:
    """Die Test-Datenbank, einmal je Sitzung über die Migrationen aufgebaut."""
    migriere(pg_server)
    return pg_server


@pytest.fixture
async def engine(pg_url):
    """Verbindung als Eigentümer der Tabellen: räumt auf und dient den Tests zum Nachsehen."""
    engine = create_async_engine(pg_url, poolclass=NullPool)
    tabellen = ", ".join(sorted(set(Base.metadata.tables) - _STAMMDATEN))
    async with engine.begin() as verbindung:
        await verbindung.execute(text(f"TRUNCATE {tabellen} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def laufzeit_engine(pg_url, engine):
    """Verbindung so, wie der Bot sie im Betrieb hat: als Rolle app_laufzeit."""
    laufzeit = erstelle_engine(pg_url, poolclass=NullPool)
    yield laufzeit
    await laufzeit.dispose()


class TestFabrik:
    """Session-Fabrik für Tests.

    Sitzungen mit Nutzerkontext (über `db_sitzung`) laufen wie im Betrieb als app_laufzeit und
    unterliegen der Row Level Security. Sitzungen ohne Kontext laufen als Eigentümer, damit die
    Tests nachsehen können, was wirklich in den Tabellen steht. Im Betrieb gibt es diesen
    zweiten Weg nicht: Dort ist jede Verbindung app_laufzeit.
    """

    __test__ = False

    def __init__(self, eigentuemer, laufzeit) -> None:
        self.eigentuemer = erstelle_session_fabrik(eigentuemer)
        self.laufzeit = erstelle_session_fabrik(laufzeit)

    def __call__(self, **optionen):
        mit_kontext = "nutzer_id" in (optionen.get("info") or {})
        return (self.laufzeit if mit_kontext else self.eigentuemer)(**optionen)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_allowed_user_ids=frozenset({ERLAUBT_ID, ADMIN_ID}),
        telegram_admin_user_ids=frozenset({ADMIN_ID}),
        anthropic_api_key="test-key",
        model_default="test-modell",
        model_einfach="test-modell-einfach",
        model_komplex="test-modell-komplex",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        price_input_usd_per_mtok="2",
        price_output_usd_per_mtok="10",
        usd_eur_rate="0.5",
        secrets_master_key=TEST_HAUPTSCHLUESSEL,
    )


@pytest.fixture
def session_fabrik(engine, laufzeit_engine):
    return TestFabrik(engine, laufzeit_engine)


@pytest.fixture
def laufzeit_fabrik(laufzeit_engine):
    """Reine Laufzeit-Fabrik: genau das, was der Bot im Betrieb benutzt."""
    return erstelle_session_fabrik(laufzeit_engine)


@pytest.fixture
async def user(settings, session_fabrik):
    await uebernehme_bestand(session_fabrik, settings)
    return await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)


@pytest.fixture
async def admin(user, session_fabrik):
    return await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID)


@pytest.fixture
async def asana_verbunden(settings, session_fabrik, user, admin) -> None:
    """Mitarbeiter und Admin haben je ihren eigenen Asana-Zugang verbunden."""
    from tests.asana_fake import ASANA_TOKEN

    tresor = Tresor.aus_settings(settings)
    for nutzer in (user, admin):
        await speichere_geheimnis(session_fabrik, tresor, nutzer, "asana", ASANA_TOKEN)


@pytest.fixture
def als_nutzer(user, asana_verbunden):
    """Tests, die ein Tool direkt aufrufen, handeln als der Mitarbeiter.

    Im Betrieb setzt der Agent-Code diesen Kontext bei jedem Tool-Aufruf.
    """
    marke = aktueller_nutzer.set(user)
    yield user
    aktueller_nutzer.reset(marke)


@pytest.fixture
def kontext(settings, session_fabrik) -> ToolKontext:
    return ToolKontext(settings=settings, session_fabrik=session_fabrik)


@pytest.fixture
def registry(kontext):
    """Registry mit Beispiel-Tools statt der echten Tools."""
    BeispielSchreiben.ausgefuehrt.clear()
    return lade_registry(kontext, paket=tests.beispiel_tools)


@pytest.fixture
def freigaben(kontext, registry) -> Freigaben:
    return Freigaben(kontext, registry)


@pytest.fixture
def alarm_texte() -> list[tuple[int, str]]:
    """Alle verschickten Alarme als (Chat-ID, Text)."""
    return []


@pytest.fixture
def alarme(session_fabrik, alarm_texte) -> Alarme:
    async def sende(chat_id: int, text: str) -> None:
        alarm_texte.append((chat_id, text))

    alarme = Alarme(session_fabrik)
    alarme.verbinde(sende)
    return alarme


@pytest.fixture
def kosten(settings, session_fabrik, alarme) -> Kosten:
    return Kosten(settings, session_fabrik, alarme)


@pytest.fixture
def baue_agent(settings, session_fabrik, registry, freigaben, kosten):
    def _baue(client) -> Agent:
        return Agent(settings, session_fabrik, client, registry, freigaben, kosten)

    return _baue


@pytest.fixture
def mail_server():
    """Lokaler IMAP- und SMTP-Server im Arbeitsspeicher; kein Zugriff auf das Internet."""
    from app.mail import verbindung
    from tests.mail_fake import FakeMailServer

    verbindung.bremse.leere()
    server = FakeMailServer()
    yield server
    server.stoppe()


@pytest.fixture
def mail_netz(mail_server, monkeypatch):
    from app.mail import verbindung
    from tests.mail_fake import TestNetz

    async def ohne_warten(sekunden: float) -> None:
        return None

    monkeypatch.setattr(verbindung, "_warte", ohne_warten)
    return TestNetz(mail_server)


@pytest.fixture
def mkontext(kontext, mail_netz) -> ToolKontext:
    """Tool-Kontext, dessen Mail-Verbindungen beim lokalen Test-Server landen."""
    from dataclasses import replace

    return replace(kontext, mail_netz=mail_netz)
