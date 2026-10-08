from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agent.loop import TAGESLIMIT_TEXT
from app.channels.base import Antwort, EingehendeNachricht
from app.channels.telegram import NUR_ADMIN_TEXT, TelegramKanal
from app.db.models import Usage
from app.observability.alerts import Alarme
from tests.conftest import ADMIN_ID, ERLAUBT_ID, FREMD_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

NACHRICHT = EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="Hi")


def test_kostenberechnung(kosten):
    # Testpreise: 2 USD / 10 USD pro 1 Mio. Tokens, Kurs 0,5
    assert kosten.berechne_eur(1_000_000, 0) == Decimal("1")
    assert kosten.berechne_eur(0, 1_000_000) == Decimal("5")
    assert kosten.berechne_eur(500_000, 100_000) == Decimal("1")


async def test_jeder_api_aufruf_wird_als_eigene_zeile_gebucht(kosten, user, session_fabrik):
    await kosten.verbuche(user.id, 1000, 200)
    await kosten.verbuche(user.id, 500, 100)
    async with session_fabrik() as session:
        erste, zweite = list(await session.scalars(select(Usage).order_by(Usage.id)))
    assert (erste.eingabe_tokens, erste.ausgabe_tokens) == (1000, 200)
    assert (zweite.eingabe_tokens, zweite.ausgabe_tokens) == (500, 100)
    assert erste.datum == kosten.heute() and erste.user_id == user.id
    assert await kosten.heute_eur() == kosten.berechne_eur(1500, 300)


async def test_schleife_verbucht_jeden_api_aufruf(baue_agent, kosten, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_lesen", {"text": "x"})),
        claude_antwort(text_block("Fertig")),
    )
    await baue_agent(client).beantworte(NACHRICHT, user)
    assert await kosten.heute_eur() == kosten.berechne_eur(200, 100)


async def test_tageslimit_blockiert_neue_anfragen(baue_agent, kosten, user, admin):
    # 1 Mio. Output-Tokens = 5 EUR = Tageslimit; das Limit gilt für alle Nutzer gemeinsam.
    await kosten.verbuche(admin.id, 0, 1_000_000)
    client = FakeAnthropic(claude_antwort(text_block("sollte nie kommen")))
    antwort = await baue_agent(client).beantworte(NACHRICHT, user)
    assert antwort.text == TAGESLIMIT_TEXT
    assert client.aufrufe == []


async def test_unter_dem_limit_wird_geantwortet(baue_agent, kosten, user):
    await kosten.verbuche(user.id, 0, 900_000)
    client = FakeAnthropic(claude_antwort(text_block("ok")))
    assert (await baue_agent(client).beantworte(NACHRICHT, user)).text == "ok"


async def test_alarm_bei_80_prozent_genau_einmal(kosten, user, alarm_texte):
    await kosten.verbuche(user.id, 0, 700_000)  # 3,50 EUR = 70 %
    assert alarm_texte == []
    await kosten.verbuche(user.id, 0, 100_000)  # 4,00 EUR = 80 %
    assert len(alarm_texte) == 1
    assert "80 %" in alarm_texte[0][1]
    assert alarm_texte[0][0] == ADMIN_ID
    await kosten.verbuche(user.id, 0, 100_000)
    assert len(alarm_texte) == 1


async def test_alarme_gehen_nur_an_admins_und_scheitern_nie(session_fabrik, user):
    alarme = Alarme(session_fabrik)
    await alarme.melde("ohne Verbindung")  # kein Sender verbunden: nur Log

    empfaenger = []

    async def kaputter_sender(chat_id: int, text: str) -> None:
        empfaenger.append(chat_id)
        raise RuntimeError("Telegram nicht erreichbar")

    alarme.verbinde(kaputter_sender)
    await alarme.melde("Test")
    assert empfaenger == [ADMIN_ID]


@pytest.fixture
def kanal(settings, session_fabrik, freigaben, kosten, alarme, user):
    async def handler(nachricht, user):
        return Antwort(text=nachricht.text)

    return TelegramKanal(
        settings, session_fabrik, handler=handler, freigaben=freigaben, kosten=kosten, alarme=alarme
    )


async def test_status_fuer_admin(kanal, kosten, user, freigaben, registry):
    await kosten.verbuche(user.id, 0, 100_000)
    await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": "x"})
    text = await kanal.verarbeite_status(ADMIN_ID)
    assert "gs-assistant 0.1.0" in text
    assert "Uptime: 0 T 0 h 0 min" in text
    assert "Kosten heute: 0,50 € von 5,00 €" in text
    assert "Offene Freigaben: 1" in text


async def test_status_nur_fuer_admins(kanal):
    assert await kanal.verarbeite_status(ERLAUBT_ID) == NUR_ADMIN_TEXT
    assert await kanal.verarbeite_status(FREMD_ID) is None
