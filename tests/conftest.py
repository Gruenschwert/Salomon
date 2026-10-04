import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import tests.beispiel_tools
from app.auth.users import finde_erlaubten_nutzer, synchronisiere_whitelist
from app.config import Settings
from app.db.models import Base
from app.db.session import erstelle_session_fabrik
from app.tools.base import ToolKontext
from app.tools.registry import lade_registry
from tests.beispiel_tools.schreibend import BeispielSchreiben

ERLAUBT_ID = 111
ADMIN_ID = 222
FREMD_ID = 999


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_allowed_user_ids=frozenset({ERLAUBT_ID, ADMIN_ID}),
        telegram_admin_user_ids=frozenset({ADMIN_ID}),
        anthropic_api_key="test-key",
        model_default="test-modell",
        model_cheap="test-modell-guenstig",
        database_url="sqlite+aiosqlite://",
    )


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as verbindung:
        await verbindung.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_fabrik(engine):
    return erstelle_session_fabrik(engine)


@pytest.fixture
async def user(settings, session_fabrik):
    await synchronisiere_whitelist(session_fabrik, settings)
    return await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)


@pytest.fixture
async def admin(user, session_fabrik):
    return await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID)


@pytest.fixture
def kontext(settings, session_fabrik) -> ToolKontext:
    return ToolKontext(settings=settings, session_fabrik=session_fabrik)


@pytest.fixture
def registry(kontext):
    """Registry mit Beispiel-Tools statt der echten Tools."""
    BeispielSchreiben.ausgefuehrt.clear()
    return lade_registry(kontext, paket=tests.beispiel_tools)
