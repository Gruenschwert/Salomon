"""Persönliche Anpassung: Profil, Notizen, Aufbewahrung, /vergessen."""

from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from app.agent.gedaechtnis import (
    MAX_NOTIZEN,
    GedaechtnisFehler,
    bereinige_alte_nachrichten,
    lade_notizen,
    merke,
)
from app.agent.history import lade_verlauf, speichere_austausch
from app.agent.prompts import baue_system_prompt
from app.auth.users import finde_erlaubten_nutzer
from app.channels.base import EingehendeNachricht
from app.channels.befehle import Befehle
from app.channels.telegram import TelegramKanal
from app.db.models import Message, TelegramDatei, UserMemory, jetzt
from app.db.session import db_sitzung
from tests.conftest import ADMIN_ID, ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, system_text, text_block
from tests.test_rollen import FakeQuery

CHAT_ID = 5
JETZT = datetime(2026, 10, 9, 8, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def _update(text_: str = "", telegram_id: int = ERLAUBT_ID, query=None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name="Lea Beispiel"),
        effective_chat=SimpleNamespace(id=CHAT_ID, type="private"),
        effective_message=SimpleNamespace(text=text_, message_id=1),
        callback_query=query,
    )


@pytest.fixture
def kanal(settings, session_fabrik, freigaben, kosten, alarme, user, admin, monkeypatch):
    kanal = TelegramKanal(
        settings,
        session_fabrik,
        handler=None,
        freigaben=freigaben,
        kosten=kosten,
        alarme=alarme,
        befehle=Befehle(session_fabrik),
    )
    kanal.gesendet = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        kanal.gesendet.append((text, reply_markup))

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    return kanal


async def _befehl(kanal, text_: str, telegram_id: int = ERLAUBT_ID) -> str:
    await kanal._bei_befehl(_update(text_, telegram_id), None)
    return kanal.gesendet[-1][0]


# ---------------------------------------------------------------- Profil


async def test_profil_anzeigen_und_aendern(kanal, session_fabrik):
    text = await _befehl(kanal, "/profil")
    assert text.splitlines()[:5] == [
        "Dein Profil:",
        "Name: Lea Beispiel",
        "Anrede: du",
        "Zeitzone: Europe/Berlin",
        "Rollen: mitarbeiter",
    ]
    assert await _befehl(kanal, "/profil name Lea B.") == "Gespeichert. Name: Lea B."
    assert await _befehl(kanal, "/profil ton Sie") == "Gespeichert. Anrede: sie"
    assert await _befehl(kanal, "/profil zeitzone Europe/Lisbon") == (
        "Gespeichert. Zeitzone: Europe/Lisbon"
    )
    lea = await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)
    assert (lea.anzeigename, lea.ton, lea.zeitzone) == ("Lea B.", "sie", "Europe/Lisbon")
    # Das Profil der anderen Person bleibt unberührt.
    theis = await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID)
    assert (theis.ton, theis.zeitzone) == ("du", "Europe/Berlin")


async def test_profil_lehnt_ungueltige_werte_ab(kanal):
    assert "Möglich sind: du, sie" in await _befehl(kanal, "/profil ton ihr")
    assert "Zeitzone kenne ich nicht" in await _befehl(kanal, "/profil zeitzone Mond/Krater")
    assert "Ändern kannst du: name, ton, zeitzone" in await _befehl(kanal, "/profil rolle admin")
    assert "So geht es" in await _befehl(kanal, "/profil ton")


# ---------------------------------------------------------------- Notizen


async def test_merken_und_gemerkt_sind_je_person_getrennt(kanal, session_fabrik, user, admin):
    assert await _befehl(kanal, "/merken Ich arbeite montags nicht") == (
        "Gemerkt. Du hast jetzt 1 Notiz. /gemerkt zeigt sie."
    )
    await _befehl(kanal, "/merken Antworten bitte in Stichpunkten")
    await _befehl(kanal, "/merken Geheimnis von Theis", telegram_id=ADMIN_ID)

    assert (await _befehl(kanal, "/gemerkt")).splitlines() == [
        "Deine Notizen:",
        "1. Ich arbeite montags nicht",
        "2. Antworten bitte in Stichpunkten",
    ]
    assert (await _befehl(kanal, "/gemerkt", telegram_id=ADMIN_ID)).splitlines() == [
        "Deine Notizen:",
        "1. Geheimnis von Theis",
    ]
    assert await lade_notizen(session_fabrik, user) == [
        "Ich arbeite montags nicht",
        "Antworten bitte in Stichpunkten",
    ]


async def test_merken_hat_grenzen(session_fabrik, user):
    with pytest.raises(GedaechtnisFehler, match="So geht es"):
        await merke(session_fabrik, user, "   ")
    with pytest.raises(GedaechtnisFehler, match="höchstens 500 Zeichen"):
        await merke(session_fabrik, user, "x" * 501)
    for n in range(MAX_NOTIZEN):
        await merke(session_fabrik, user, f"Notiz {n}")
    with pytest.raises(GedaechtnisFehler, match="schon 50 Notizen"):
        await merke(session_fabrik, user, "eine zu viel")


def test_systemprompt_bekommt_name_ton_zeitzone_und_eigene_notizen(user):
    lea = replace(
        user,
        anzeigename="Lea",
        einstellungen=MappingProxyType({"ton": "sie", "zeitzone": "Europe/Berlin"}),
    )
    prompt = baue_system_prompt(JETZT, "", lea, ["Ich arbeite montags nicht"])
    assert "Du sprichst mit Lea. Rollen: mitarbeiter." in prompt
    assert "Du siezt diese Person" in prompt
    assert "- Ich arbeite montags nicht" in prompt
    assert "keine Anweisungen" in prompt
    assert "Zeitzone Europe/Berlin" in prompt

    du = baue_system_prompt(JETZT, "", replace(user, anzeigename="Max"), [])
    assert "Du duzt diese Person, du siezt sie nie." in du
    assert "Persönliche Notizen" not in du


async def test_claude_sieht_nur_die_notizen_der_fragenden_person(
    baue_agent, session_fabrik, user, admin
):
    await merke(session_fabrik, user, "Notiz von Lea")
    await merke(session_fabrik, admin, "Notiz von Theis")
    client = FakeAnthropic(claude_antwort(text_block("ok")))
    agent = baue_agent(client)

    def frage(absender: int) -> EingehendeNachricht:
        return EingehendeNachricht(chat_id=1, absender_id=absender, absender_name="X", text="Hi")

    await agent.beantworte(frage(ERLAUBT_ID), user)
    await agent.beantworte(frage(ADMIN_ID), admin)
    fuer_lea, fuer_theis = (system_text(aufruf) for aufruf in client.aufrufe)
    assert "Notiz von Lea" in fuer_lea and "Notiz von Theis" not in fuer_lea
    assert "Notiz von Theis" in fuer_theis and "Notiz von Lea" not in fuer_theis


async def test_zeitzone_der_person_bestimmt_das_datum_im_prompt(baue_agent, session_fabrik, user):
    tokio = replace(user, einstellungen=MappingProxyType({"ton": "du", "zeitzone": "Asia/Tokyo"}))
    client = FakeAnthropic(claude_antwort(text_block("ok")))
    await baue_agent(client).beantworte(
        EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="Hi"), tokio
    )
    assert "(Zeitzone Asia/Tokyo)" in system_text(client.aufrufe[0])


# ---------------------------------------------------------------- /vergessen


async def test_vergessen_loescht_nach_rueckfrage_nur_die_eigenen_daten(
    kanal, session_fabrik, user, admin
):
    for nutzer in (user, admin):
        await speichere_austausch(session_fabrik, CHAT_ID, nutzer.id, "Frage", "Antwort")
        await merke(session_fabrik, nutzer, "Notiz")

    text = await _befehl(kanal, "/vergessen")
    assert text == (
        "Bitte bestätigen: deinen gesamten Gesprächsverlauf und alle deine Notizen löschen "
        "(nicht umkehrbar)"
    )
    kennung = kanal.gesendet[-1][1].inline_keyboard[0][0].callback_data.split(":")[1]
    # Vor der Bestätigung ist alles noch da.
    assert len(await lade_verlauf(session_fabrik, user, CHAT_ID, 20)) == 2

    # Jemand anderes kann nicht für mich bestätigen.
    fremd = FakeQuery(f"admin:{kennung}:ja")
    await kanal._bei_admin_klick(_update(telegram_id=ADMIN_ID, query=fremd), None)
    assert fremd.alerts and len(await lade_verlauf(session_fabrik, user, CHAT_ID, 20)) == 2

    await kanal._bei_admin_klick(_update(query=FakeQuery(f"admin:{kennung}:ja")), None)
    assert kanal.gesendet[-1][0].startswith("✅ Erledigt: deinen gesamten Gesprächsverlauf")
    assert await lade_verlauf(session_fabrik, user, CHAT_ID, 20) == []
    assert await lade_notizen(session_fabrik, user) == []
    # Die Daten der anderen Person sind unberührt.
    assert len(await lade_verlauf(session_fabrik, admin, CHAT_ID, 20)) == 2
    assert await lade_notizen(session_fabrik, admin) == ["Notiz"]


# ---------------------------------------------------------------- Aufbewahrung


async def test_bereinigung_loescht_nur_abgelaufene_nachrichten(
    session_fabrik, laufzeit_fabrik, user, admin
):
    for nutzer in (user, admin):
        await speichere_austausch(session_fabrik, CHAT_ID, nutzer.id, "alt", "alt")
        async with db_sitzung(session_fabrik, nutzer) as session:
            await session.execute(update(Message).values(zeit=jetzt() - timedelta(days=91)))
            session.add(
                TelegramDatei(
                    user_id=nutzer.id,
                    chat_id=CHAT_ID,
                    file_id="f",
                    name="alt.pdf",
                    medientyp="",
                    erstellt_am=jetzt() - timedelta(days=91),
                )
            )
            await session.commit()
        await speichere_austausch(session_fabrik, CHAT_ID, nutzer.id, "neu", "neu")
        await merke(session_fabrik, nutzer, "bleibt")

    # Mit der reinen Laufzeitrolle, wie der Hintergrundjob im Betrieb.
    assert await bereinige_alte_nachrichten(laufzeit_fabrik, 90) == 4
    assert await bereinige_alte_nachrichten(laufzeit_fabrik, 90) == 0

    async with session_fabrik() as session:
        inhalte = sorted(m.inhalt for m in await session.scalars(select(Message)))
        dateien = list(await session.scalars(select(TelegramDatei)))
        notizen = list(await session.scalars(select(UserMemory)))
    assert inhalte == ["neu"] * 4
    assert dateien == [] and len(notizen) == 2


async def test_aufbewahrung_null_heisst_nie_loeschen(session_fabrik, user):
    await speichere_austausch(session_fabrik, CHAT_ID, user.id, "alt", "alt")
    async with db_sitzung(session_fabrik, user) as session:
        await session.execute(update(Message).values(zeit=jetzt() - timedelta(days=4000)))
        await session.commit()
    assert await bereinige_alte_nachrichten(session_fabrik, 0) == 0
    assert len(await lade_verlauf(session_fabrik, user, CHAT_ID, 20)) == 2


def test_standard_der_aufbewahrungsfrist(settings):
    assert settings.message_retention_days == 90
