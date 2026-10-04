import pytest
from sqlalchemy import select

from app.auth.users import finde_erlaubten_nutzer, synchronisiere_whitelist
from app.channels.base import Antwort, EingehendeNachricht
from app.channels.telegram import TelegramKanal
from app.db.models import AuditLog, User
from tests.conftest import ADMIN_ID, ERLAUBT_ID, FREMD_ID


def _nachricht(absender_id: int, text: str = "Hallo") -> EingehendeNachricht:
    return EingehendeNachricht(chat_id=1, absender_id=absender_id, absender_name="X", text=text)


@pytest.fixture
async def kanal(settings, session_fabrik, freigaben):
    await synchronisiere_whitelist(session_fabrik, settings)
    aufrufe = []

    async def handler(nachricht, user):
        aufrufe.append(nachricht)
        return Antwort(text=nachricht.text)

    kanal = TelegramKanal(settings, session_fabrik, handler=handler, freigaben=freigaben)
    kanal.aufrufe = aufrufe
    return kanal


async def test_sync_legt_nutzer_mit_rollen_an(settings, session_fabrik):
    await synchronisiere_whitelist(session_fabrik, settings)
    async with session_fabrik() as session:
        rollen = {u.telegram_id: u.rolle for u in await session.scalars(select(User))}
    assert rollen == {ERLAUBT_ID: "user", ADMIN_ID: "admin"}


async def test_sync_deaktiviert_entfernte_nutzer(settings, session_fabrik):
    await synchronisiere_whitelist(session_fabrik, settings)
    kleiner = settings.model_copy(update={"telegram_allowed_user_ids": frozenset({ADMIN_ID})})
    await synchronisiere_whitelist(session_fabrik, kleiner)
    assert await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID) is None
    assert await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID) is not None


async def test_erlaubter_nutzer_bekommt_antwort(kanal):
    antwort = await kanal.verarbeite(_nachricht(ERLAUBT_ID, "Ping"))
    assert antwort == Antwort(text="Ping")


async def test_unbekannter_nutzer_wird_ignoriert(kanal, session_fabrik):
    antwort = await kanal.verarbeite(_nachricht(FREMD_ID))
    assert antwort is None
    assert kanal.aufrufe == []
    async with session_fabrik() as session:
        eintrag = (await session.scalars(select(AuditLog))).one()
    assert eintrag.tool_name == "unbekannt"
    assert eintrag.user_id is None
    assert eintrag.parameter == {"telegram_id": FREMD_ID}
