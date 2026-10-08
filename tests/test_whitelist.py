import pytest
from sqlalchemy import select

from app.auth.users import uebernehme_bestand
from app.channels.base import Antwort, EingehendeNachricht
from app.channels.telegram import TelegramKanal
from app.db.models import AuditLog
from tests.conftest import ERLAUBT_ID, FREMD_ID


def _nachricht(absender_id: int, text: str = "Hallo") -> EingehendeNachricht:
    return EingehendeNachricht(chat_id=1, absender_id=absender_id, absender_name="X", text=text)


@pytest.fixture
async def kanal(settings, session_fabrik, freigaben, kosten, alarme):
    await uebernehme_bestand(session_fabrik, settings)
    aufrufe = []

    async def handler(nachricht, user):
        aufrufe.append(nachricht)
        return Antwort(text=nachricht.text)

    kanal = TelegramKanal(
        settings, session_fabrik, handler=handler, freigaben=freigaben, kosten=kosten, alarme=alarme
    )
    kanal.aufrufe = aufrufe
    return kanal


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
