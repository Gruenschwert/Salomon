"""Kosten: Preise, Buchung je Modellantwort, /kosten, Limits pro Person und gesamt."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent.gedaechtnis import merke
from app.agent.history import speichere_austausch
from app.agent.preise import PREISE, kosten_usd, preis_fuer
from app.channels.base import Antwort, EingehendeNachricht
from app.channels.befehle import Befehle
from app.channels.telegram import NUR_ADMIN_TEXT, TelegramKanal
from app.db.models import Usage
from app.observability.alerts import Alarme
from tests.conftest import ADMIN_ID, ERLAUBT_ID, FREMD_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block
from tests.test_rollen import FakeQuery

NACHRICHT = EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="Hi")
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-4-5-20251001"
OPUS = "claude-opus-5-5"
ERSATZ = (Decimal("2"), Decimal("10"))


# ---------------------------------------------------------------- Preise


def test_preise_stehen_je_modell_und_token_art_in_der_tabelle():
    """Werte der offiziellen Preisseite, abgerufen am 08.10.2026 (USD je 1 Mio. Tokens)."""
    erwartet = {
        # Modell: (Eingabe, Ausgabe, Cache lesen, Cache schreiben 5 min)
        OPUS: ("4", "20", "0.20", "5"),
        SONNET: ("2", "10", "0.10", "2.50"),
        "claude-sonnet-5": ("2", "10", "0.20", "2.50"),
        HAIKU: ("1", "5", "0.10", "1.25"),
    }
    for modell, werte in erwartet.items():
        preis, bekannt = preis_fuer(modell, *ERSATZ)
        assert bekannt, modell
        assert (preis.eingabe, preis.ausgabe, preis.cache_lesen, preis.cache_schreiben) == tuple(
            Decimal(w) for w in werte
        ), modell
    # Der längere Anfang gewinnt: Sonnet 5.5 ist nicht Sonnet 5.
    assert preis_fuer("claude-sonnet-5-5", *ERSATZ)[0] is PREISE["claude-sonnet-5-5"]
    assert preis_fuer("claude-sonnet-5", *ERSATZ)[0] is PREISE["claude-sonnet-5"]


def test_unbekanntes_modell_nutzt_die_ersatzpreise_aus_der_env():
    preis, bekannt = preis_fuer("anderes-modell", *ERSATZ)
    assert not bekannt
    assert (preis.eingabe, preis.ausgabe) == ERSATZ
    assert (preis.cache_lesen, preis.cache_schreiben) == (Decimal("0.2"), Decimal("2.5"))


def test_token_arten_werden_getrennt_gerechnet():
    preis = PREISE["claude-sonnet-5-5"]
    # 1 Mio. von jeder Art: 2 + 10 + 0,10 + 2,50 USD
    assert kosten_usd(preis, 1_000_000, 1_000_000, 1_000_000, 1_000_000) == Decimal("14.60")
    assert kosten_usd(preis, 10_000, 500) == Decimal("0.025")
    # Dieselben 10.000 Tokens aus dem Cache gelesen kosten ein Zwanzigstel.
    assert kosten_usd(preis, 0, 0, cache_lesen=10_000) == Decimal("0.001")


def test_kostenberechnung_in_euro(kosten):
    # Testkurs 0,5; unbekanntes Modell: 2 USD / 10 USD je 1 Mio. Tokens
    assert kosten.berechne_eur(1_000_000, 0) == Decimal("1")
    assert kosten.berechne_eur(0, 1_000_000) == Decimal("5")
    assert kosten.berechne_eur(1_000_000, 1_000_000, OPUS) == Decimal("12")


# ---------------------------------------------------------------- Buchung


async def test_jede_modellantwort_wird_mit_allen_angaben_gebucht(kosten, user, session_fabrik):
    eur = await kosten.verbuche(
        user,
        1000,
        200,
        modell=SONNET,
        cache_lese_tokens=8000,
        cache_schreib_tokens=4000,
        grund="standard: keine Regel",
    )
    await kosten.verbuche(user, 500, 100, modell=HAIKU, grund="einfach: kurze Frage")
    async with session_fabrik() as session:
        erste, zweite = list(await session.scalars(select(Usage).order_by(Usage.id)))
    assert (erste.user_id, erste.modell, erste.grund_modellwahl) == (
        user.id,
        SONNET,
        "standard: keine Regel",
    )
    assert (
        erste.eingabe_tokens,
        erste.ausgabe_tokens,
        erste.cache_lese_tokens,
        erste.cache_schreib_tokens,
    ) == (1000, 200, 8000, 4000)
    # 1000*2 + 200*10 + 8000*0,10 + 4000*2,50 = 14.800 Mikro-USD
    assert erste.kosten_usd == Decimal("0.014800")
    assert erste.kosten_eur == Decimal("0.007400") == eur
    assert (zweite.modell, zweite.kosten_usd) == (HAIKU, Decimal("0.001000"))
    assert erste.datum == kosten.heute()


async def test_schleife_bucht_jeden_api_aufruf_mit_modell(baue_agent, kosten, user, session_fabrik):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_lesen", {"text": "x"})),
        claude_antwort(text_block("Fertig")),
    )
    await baue_agent(client).beantworte(NACHRICHT, user)
    async with session_fabrik() as session:
        zeilen = list(await session.scalars(select(Usage)))
    assert len(zeilen) == 2
    assert {z.modell for z in zeilen} == {"test-modell"}
    assert await kosten.heute_eur(user.id) == kosten.berechne_eur(200, 100)


# ---------------------------------------------------------------- Limits


async def test_tageslimit_gilt_je_person(baue_agent, kosten, user, admin):
    # 1 Mio. Output-Tokens = 5 EUR = Tageslimit der Person
    await kosten.verbuche(user, 0, 1_000_000)
    client = FakeAnthropic(claude_antwort(text_block("Antwort")))
    agent = baue_agent(client)

    gesperrt = await agent.beantworte(NACHRICHT, user)
    assert gesperrt.text == (
        "Dein Tageslimit von 5,00 € ist erreicht (heute 5,00 €). KI-Anfragen gehen ab morgen "
        "wieder. Befehle wie /kosten funktionieren weiter."
    )
    assert client.aufrufe == []
    # Die andere Person ist davon nicht betroffen.
    anderer = EingehendeNachricht(chat_id=1, absender_id=ADMIN_ID, absender_name="A", text="Hi")
    assert (await agent.beantworte(anderer, admin)).text == "Antwort"


async def test_unter_dem_limit_wird_geantwortet(baue_agent, kosten, user):
    await kosten.verbuche(user, 0, 900_000)
    client = FakeAnthropic(claude_antwort(text_block("ok")))
    assert (await baue_agent(client).beantworte(NACHRICHT, user)).text == "ok"


async def test_warnung_bei_80_prozent_geht_genau_einmal_an_die_person(kosten, user, alarm_texte):
    await kosten.verbuche(user, 0, 700_000)  # 3,50 EUR = 70 %
    assert alarm_texte == []
    await kosten.verbuche(user, 0, 100_000)  # 4,00 EUR = 80 %
    assert alarm_texte == [
        (ERLAUBT_ID, "Hinweis: Du hast 80 % deines Tageslimits erreicht (4,00 € von 5,00 €).")
    ]
    await kosten.verbuche(user, 0, 100_000)
    assert len(alarm_texte) == 1


async def test_gesamtlimit_aller_sperrt_alle_und_warnt_die_admins(
    settings, session_fabrik, alarme, alarm_texte, baue_agent, user, admin
):
    from app.observability.costs import Kosten

    knapp = settings.model_copy(update={"daily_cost_limit_total_eur": Decimal("6")})
    kosten = Kosten(knapp, session_fabrik, alarme)
    await kosten.verbuche(admin.id, 0, 900_000)  # 4,50 EUR: unter dem eigenen Limit des Admins
    assert await kosten.limit_erreicht(user.id) is None
    await kosten.verbuche(user.id, 0, 100_000)  # zusammen 5,00 EUR = 83 % von 6
    assert [(wer, "gemeinsamen Tageslimits" in text) for wer, text in alarm_texte] == [
        (ADMIN_ID, True)
    ]
    await kosten.verbuche(user.id, 0, 200_000)  # zusammen 6,00 EUR
    meldung = await kosten.limit_erreicht(user.id)
    assert meldung.startswith("Das gemeinsame Tageslimit aller von 6,00 € ist erreicht.")
    assert await kosten.limit_erreicht(admin.id) == meldung


async def test_limit_pro_person_ueberschreibt_den_standard(kosten, user, admin):
    assert await kosten.limit_von(user.id) == Decimal("5")
    await kosten.setze_limit(user.id, Decimal("1"))
    assert await kosten.limit_von(user.id) == Decimal("1")
    assert await kosten.limit_von(admin.id) == Decimal("5")
    await kosten.verbuche(user, 0, 200_000)  # 1,00 EUR
    assert "Dein Tageslimit von 1,00 €" in await kosten.limit_erreicht(user.id)
    await kosten.setze_limit(user.id, None)
    assert await kosten.limit_erreicht(user.id) is None


async def test_alarme_gehen_nur_an_admins_und_scheitern_nie(session_fabrik, user):
    alarme = Alarme(session_fabrik)
    await alarme.melde("ohne Verbindung")  # kein Sender verbunden: nur Log
    await alarme.an_person(ERLAUBT_ID, "ohne Verbindung")

    empfaenger = []

    async def kaputter_sender(chat_id: int, text: str) -> None:
        empfaenger.append(chat_id)
        raise RuntimeError("Telegram nicht erreichbar")

    alarme.verbinde(kaputter_sender)
    await alarme.melde("Test")
    await alarme.an_person(ERLAUBT_ID, "Test")
    assert empfaenger == [ADMIN_ID, ERLAUBT_ID]


# ---------------------------------------------------------------- /kosten


def _update(text_: str = "", telegram_id: int = ERLAUBT_ID, query=None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name=""),
        effective_chat=SimpleNamespace(id=5, type="private"),
        effective_message=SimpleNamespace(text=text_, message_id=1),
        callback_query=query,
    )


@pytest.fixture
def kanal(settings, session_fabrik, freigaben, kosten, alarme, user, admin, monkeypatch):
    async def handler(nachricht, user):
        return Antwort(text=nachricht.text)

    kanal = TelegramKanal(
        settings,
        session_fabrik,
        handler=handler,
        freigaben=freigaben,
        kosten=kosten,
        alarme=alarme,
        befehle=Befehle(session_fabrik, kosten=kosten),
    )
    kanal.gesendet = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        kanal.gesendet.append((text, reply_markup))

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    return kanal


async def _befehl(kanal, text_: str, telegram_id: int = ERLAUBT_ID) -> str:
    await kanal._bei_befehl(_update(text_, telegram_id), None)
    return kanal.gesendet[-1][0]


@pytest.fixture
async def testdaten(kosten, session_fabrik, user, admin):
    """Verbrauch über 30 Tage. Jede Zeile: (Person, Tage zurück, Modell, EUR, Grund)."""
    heute = kosten.heute()
    zeilen = [
        (user, 0, SONNET, "0.40", "standard: keine Regel"),
        (user, 0, HAIKU, "0.02", "einfach: kurze Frage"),
        (user, 1, SONNET, "1.20", "standard: keine Regel"),
        (user, 2, OPUS, "0.90", "komplex: Foto oder PDF"),
        (user, 5, HAIKU, "0.03", "einfach: kurze Frage"),
        (user, 20, SONNET, "2.00", "standard: keine Regel"),
        (user, 40, SONNET, "9.00", "standard: keine Regel"),
        (admin, 0, OPUS, "3.00", "komplex: Stichwort Analyse"),
        (admin, 6, SONNET, "1.50", "standard: keine Regel"),
    ]
    async with session_fabrik() as session:
        for nutzer, zurueck, modell, eur, grund in zeilen:
            session.add(
                Usage(
                    datum=heute - timedelta(days=zurueck),
                    user_id=nutzer.id,
                    modell=modell,
                    kosten_usd=Decimal(eur) * 2,
                    kosten_eur=Decimal(eur),
                    grund_modellwahl=grund,
                )
            )
        await session.commit()
    return heute


async def test_kosten_fuer_1_3_7_und_30_tage(kanal, testdaten):
    heute = testdaten
    eins = await _befehl(kanal, "/kosten 1")
    assert eins.splitlines()[1] == "Gesamt: 0,42 € bei 2 Anfragen an die KI"
    assert eins == await _befehl(kanal, "/kosten")

    drei = (await _befehl(kanal, "/kosten 3")).splitlines()
    assert drei[0] == (
        f"Deine Kosten, letzte 3 Tage ({heute - timedelta(days=2):%d.%m.} bis {heute:%d.%m.}):"
    )
    assert drei[1] == "Gesamt: 2,52 € bei 4 Anfragen an die KI"
    assert f"Größter Tag: {heute - timedelta(days=1):%d.%m.} mit 1,20 €" in drei

    sieben = (await _befehl(kanal, "/kosten 7")).splitlines()
    assert sieben[1] == "Gesamt: 2,55 € bei 5 Anfragen an die KI"
    assert sieben[2:6] == [
        "Nach Modell:",
        f"- {SONNET}: 1,60 € (2)",
        f"- {OPUS}: 0,90 € (1)",
        f"- {HAIKU}: 0,05 € (2)",
    ]
    assert "- komplex: Foto oder PDF: 1" in sieben
    assert sieben[-1] == "Heute: 0,42 € von 5,00 € Tageslimit"

    dreissig = (await _befehl(kanal, "/kosten 30")).splitlines()
    # Der Eintrag von vor 40 Tagen zählt nicht mehr mit.
    assert dreissig[1] == "Gesamt: 4,55 € bei 6 Anfragen an die KI"
    assert f"Größter Tag: {heute - timedelta(days=20):%d.%m.} mit 2,00 €" in dreissig
    for ausgabe in (eins, "\n".join(drei), "\n".join(sieben), "\n".join(dreissig)):
        assert "**" not in ausgabe and "`" not in ausgabe


async def test_kosten_ohne_verbrauch_und_mit_falscher_angabe(kanal):
    leer = (await _befehl(kanal, "/kosten 7")).splitlines()
    assert leer[1] == "Gesamt: 0,00 € bei 0 Anfragen an die KI"
    assert leer[-1] == "Heute: 0,00 € von 5,00 € Tageslimit"
    for falsch in ("/kosten 5", "/kosten gestern", "/kosten 7 3", "/kosten -1"):
        assert (await _befehl(kanal, falsch)).startswith("So geht es: /kosten"), falsch


async def test_kosten_aller_nur_fuer_admins_und_nur_zahlen(
    kanal, testdaten, session_fabrik, user, admin
):
    heute = testdaten
    # Inhalte, die in keiner Auswertung auftauchen dürfen:
    await speichere_austausch(session_fabrik, 5, user.id, "geheime Frage von Lea", "Antwort")
    await merke(session_fabrik, user, "geheime Notiz von Lea")

    nein = await _befehl(kanal, "/kosten 7 alle")
    assert nein == "Die Kosten aller sieht nur, wer das Recht dafür hat. Deine eigenen: /kosten"

    alle = await _befehl(kanal, "/kosten 7 alle", telegram_id=ADMIN_ID)
    assert alle.splitlines() == [
        f"Kosten aller, letzte 7 Tage ({heute - timedelta(days=6):%d.%m.} bis {heute:%d.%m.}):",
        f"- Telegram-ID {ADMIN_ID}: 4,50 € (2)",
        f"- Telegram-ID {ERLAUBT_ID}: 2,55 € (5)",
        "Summe: 7,05 € bei 7 Anfragen an die KI",
    ]
    assert "geheim" not in alle
    # Die eigene Auswertung des Admins enthält nur seine Zahlen.
    eigene = await _befehl(kanal, "/kosten 7", telegram_id=ADMIN_ID)
    assert eigene.splitlines()[1] == "Gesamt: 4,50 € bei 2 Anfragen an die KI"


async def test_kosten_funktioniert_auch_bei_erreichtem_limit(kanal, kosten, user):
    await kosten.verbuche(user, 0, 1_000_000)
    assert await kosten.limit_erreicht(user.id) is not None
    text = await _befehl(kanal, "/kosten")
    assert "Heute: 5,00 € von 5,00 € Tageslimit" in text


async def test_limit_setzen_nur_admin_mit_bestaetigung(kanal, kosten, user, session_fabrik):
    nein = await _befehl(kanal, f"/limit {ADMIN_ID} 100")
    assert nein == "Dafür fehlt dir das Recht. Die Rolle dafür kann ein Admin vergeben."

    text = await _befehl(kanal, f"/limit {ERLAUBT_ID} 12,50", telegram_id=ADMIN_ID)
    assert text == f"Bitte bestätigen: Tageslimit von {ERLAUBT_ID} auf 12,50 € setzen"
    assert await kosten.limit_von(user.id) == Decimal("5")
    kennung = kanal.gesendet[-1][1].inline_keyboard[0][0].callback_data.split(":")[1]
    await kanal._bei_admin_klick(
        _update(telegram_id=ADMIN_ID, query=FakeQuery(f"admin:{kennung}:ja")), None
    )
    assert await kosten.limit_von(user.id) == Decimal("12.50")
    assert "Heute: 0,00 € von 12,50 € Tageslimit" in await _befehl(kanal, "/kosten")

    for falsch, teil in (
        (f"/limit {ERLAUBT_ID} viel", "muss eine Zahl in Euro sein"),
        (f"/limit {ERLAUBT_ID} -3", "zwischen 0 und 1000"),
        ("/limit 12", "So geht es"),
    ):
        assert teil in await _befehl(kanal, falsch, telegram_id=ADMIN_ID)

    await _befehl(kanal, f"/limit {ERLAUBT_ID} standard", telegram_id=ADMIN_ID)
    kennung = kanal.gesendet[-1][1].inline_keyboard[0][0].callback_data.split(":")[1]
    await kanal._bei_admin_klick(
        _update(telegram_id=ADMIN_ID, query=FakeQuery(f"admin:{kennung}:ja")), None
    )
    assert await kosten.limit_von(user.id) == Decimal("5")


# ---------------------------------------------------------------- /status


async def test_status_fuer_admin(kanal, kosten, user, freigaben, registry):
    await kosten.verbuche(user.id, 0, 100_000)
    await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": "x"})
    text = await kanal.verarbeite_status(ADMIN_ID)
    assert "gs-assistant 0.1.0" in text
    assert "Uptime: 0 T 0 h 0 min" in text
    assert "Kosten heute (alle): 0,50 € von 20,00 €" in text
    # Gezählt werden nur die eigenen Freigaben; die des Mitarbeiters sieht der Admin nicht.
    assert "Eigene offene Freigaben: 0" in text


async def test_status_nur_fuer_admins(kanal):
    assert await kanal.verarbeite_status(ERLAUBT_ID) == NUR_ADMIN_TEXT
    assert await kanal.verarbeite_status(FREMD_ID) is None
