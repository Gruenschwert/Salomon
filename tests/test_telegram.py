"""Tests der Telegram-Handler mit nachgebauten Updates; es wird nichts an Telegram gesendet."""

from types import SimpleNamespace

import pytest

from app.channels.base import Antwort, FreigabeAnfrage
from app.channels.telegram import FEHLER_TEXT, TELEGRAM_MAX_ZEICHEN, TelegramKanal
from tests.beispiel_tools.schreibend import BeispielSchreiben
from tests.conftest import ADMIN_ID, ERLAUBT_ID, FREMD_ID

CHAT_ID = 5


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.antworten: list[tuple] = []
        self.buttons_entfernt = False

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.antworten.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        self.buttons_entfernt = True


def _update(telegram_id: int, text: str = "", query: FakeQuery | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name="Test Nutzer"),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_message=SimpleNamespace(text=text),
        callback_query=query,
    )


@pytest.fixture
def gesendet() -> list[tuple]:
    return []


@pytest.fixture
def kanal(settings, session_fabrik, freigaben, kosten, alarme, user, gesendet):
    async def handler(nachricht, user):
        if nachricht.text == "kaputt":
            raise RuntimeError("geheimes internes Detail")
        if nachricht.text == "schreiben":
            return Antwort(text="Bitte freigeben", freigaben=(FreigabeAnfrage(1, "Vorschau"),))
        return Antwort(text=f"Echo: {nachricht.text}")

    kanal = TelegramKanal(
        settings, session_fabrik, handler=handler, freigaben=freigaben, kosten=kosten, alarme=alarme
    )

    async def sende_antwort(chat_id: int, text: str) -> None:
        gesendet.append(("text", chat_id, text))

    async def sende_freigabe_anfrage(chat_id: int, anfrage: FreigabeAnfrage) -> None:
        gesendet.append(("freigabe", chat_id, anfrage))

    kanal.sende_antwort = sende_antwort
    kanal.sende_freigabe_anfrage = sende_freigabe_anfrage
    return kanal


async def test_erlaubter_nutzer_bekommt_antwort(kanal, gesendet):
    await kanal._bei_nachricht(_update(ERLAUBT_ID, "Hallo"), None)
    assert gesendet == [("text", CHAT_ID, "Echo: Hallo")]


async def test_unbekannter_nutzer_bekommt_nichts(kanal, gesendet):
    await kanal._bei_nachricht(_update(FREMD_ID, "Hallo"), None)
    assert gesendet == []


async def test_freigabe_anfrage_wird_nach_der_antwort_gesendet(kanal, gesendet):
    await kanal._bei_nachricht(_update(ERLAUBT_ID, "schreiben"), None)
    assert [eintrag[0] for eintrag in gesendet] == ["text", "freigabe"]


async def test_fehler_ergibt_neutrale_meldung_und_alarm(kanal, gesendet, alarm_texte):
    await kanal._bei_nachricht(_update(ERLAUBT_ID, "kaputt"), None)
    assert gesendet == [("text", CHAT_ID, FEHLER_TEXT)]
    ((empfaenger, alarm),) = alarm_texte
    assert empfaenger == ADMIN_ID
    assert "RuntimeError" in alarm
    assert "geheimes" not in alarm


async def test_klick_des_anfragenden_fuehrt_aus(kanal, gesendet, freigaben, registry, user):
    anfrage = await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": "x"})
    query = FakeQuery(f"freigabe:{anfrage.approval_id}:ja")
    await kanal._bei_klick(_update(ERLAUBT_ID, query=query), None)
    assert BeispielSchreiben.ausgefuehrt == ["x"]
    assert query.buttons_entfernt
    assert gesendet[0][2].startswith("✅")


async def test_klick_verwerfen(kanal, gesendet, freigaben, registry, user):
    anfrage = await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": "x"})
    query = FakeQuery(f"freigabe:{anfrage.approval_id}:nein")
    await kanal._bei_klick(_update(ERLAUBT_ID, query=query), None)
    assert BeispielSchreiben.ausgefuehrt == []
    assert query.buttons_entfernt
    assert gesendet[0][2].startswith("❌")


async def test_klick_eines_anderen_nutzers_laesst_freigabe_offen(
    kanal, gesendet, freigaben, registry, user
):
    anfrage = await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": "x"})
    query = FakeQuery(f"freigabe:{anfrage.approval_id}:ja")
    await kanal._bei_klick(_update(ADMIN_ID, query=query), None)
    assert BeispielSchreiben.ausgefuehrt == []
    assert not query.buttons_entfernt
    assert gesendet == []
    assert query.antworten[0][1] is True  # Hinweis als Alert


async def test_klick_eines_unbekannten_wird_ignoriert(kanal, gesendet, freigaben, registry, user):
    anfrage = await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": "x"})
    query = FakeQuery(f"freigabe:{anfrage.approval_id}:ja")
    await kanal._bei_klick(_update(FREMD_ID, query=query), None)
    assert BeispielSchreiben.ausgefuehrt == []
    assert query.antworten == []
    assert gesendet == []


async def test_lange_antwort_wird_aufgeteilt(
    settings, session_fabrik, freigaben, kosten, alarme, monkeypatch
):
    kanal = TelegramKanal(
        settings, session_fabrik, handler=None, freigaben=freigaben, kosten=kosten, alarme=alarme
    )
    teile = []

    async def send_message(self, chat_id, text, **kwargs):
        teile.append(text)

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    await kanal.sende_antwort(CHAT_ID, "a" * (TELEGRAM_MAX_ZEICHEN + 10))
    assert [len(teil) for teil in teile] == [TELEGRAM_MAX_ZEICHEN, 10]
