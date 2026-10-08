"""Modellstufen: Router, Eskalation, Rückfall, /modell und Prompt-Caching."""

import logging
from decimal import Decimal

import anthropic
import httpx
import pytest
from sqlalchemy import select

from app.agent.preise import PREISE, kosten_usd
from app.agent.router import (
    AUTO,
    EINFACH,
    KOMPLEX,
    STANDARD,
    ModellVorgaben,
    waehle_modell,
)
from app.channels.base import Bild, DateiHinweis, EingehendeNachricht
from app.channels.befehle import Befehle
from app.config import Settings
from app.db.models import Usage
from tests.beispiel_tools.lesend import BeispielLesen
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

M_EINFACH = "test-modell-einfach"
M_STANDARD = "test-modell"
M_KOMPLEX = "test-modell-komplex"


def nachricht(text: str = "", **weitere) -> EingehendeNachricht:
    return EingehendeNachricht(
        chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text=text, **weitere
    )


async def _buchungen(session_fabrik) -> list[Usage]:
    async with session_fabrik() as session:
        return list(await session.scalars(select(Usage).order_by(Usage.id)))


def _nicht_gefunden() -> anthropic.NotFoundError:
    antwort = httpx.Response(404, request=httpx.Request("POST", "https://api.anthropic.com"))
    return anthropic.NotFoundError("model not found", response=antwort, body=None)


# ---------------------------------------------------------------- Einstellungen


def test_standardwerte_und_model_default_als_rueckfall():
    basis = {
        "telegram_bot_token": "x",
        "anthropic_api_key": "y",
        "database_url": "postgresql+asyncpg://x/y",
        "price_input_usd_per_mtok": 2,
        "price_output_usd_per_mtok": 10,
        "usd_eur_rate": 1,
        "_env_file": None,
    }
    ohne = Settings(**basis)
    assert ohne.modell(EINFACH) == "claude-haiku-4-5-20251001"
    assert ohne.modell(STANDARD) == "claude-sonnet-5-5"
    assert ohne.modell(KOMPLEX) == "claude-opus-5-5"
    # Wer bisher nur MODEL_DEFAULT gesetzt hat, behält sein Modell als Standardstufe.
    assert Settings(**basis, model_default="altes-modell").modell(STANDARD) == "altes-modell"
    beide = Settings(**basis, model_default="altes-modell", model_standard="neues-modell")
    assert beide.modell(STANDARD) == "neues-modell"


# ---------------------------------------------------------------- Router


@pytest.mark.parametrize(
    ("eingang", "stufe", "grund"),
    [
        (nachricht("Zeig mir meine Aufgaben für heute"), EINFACH, "einfach: kurze Lesefrage"),
        (nachricht("Wie viele Aufgaben sind überfällig?"), EINFACH, "einfach: kurze Lesefrage"),
        (nachricht("Wie ist der Status im Projekt Relaunch?"), EINFACH, "Stichwort status"),
        (nachricht("Hi, was kannst du?"), STANDARD, "standard: keine besondere Regel"),
        # Eine Lesefrage mit Änderungswunsch ist nicht mehr einfach.
        (nachricht("Zeig mir die Liste und lösche die erste"), STANDARD, "standard"),
        (nachricht("Leg bitte eine Aufgabe für Lea an"), STANDARD, "standard"),
        (nachricht("Mach eine Analyse der Verkäufe im März"), KOMPLEX, "Stichwort analyse"),
        (nachricht("Schreib einen Projektplan für den Messeauftritt"), KOMPLEX, "projektplan"),
        (nachricht("Auswertung der Buchhaltung bitte"), KOMPLEX, "Stichwort auswertung"),
        (nachricht("Vergleich der beiden Angebote"), KOMPLEX, "Stichwort vergleich"),
        (nachricht("Status? " + "x" * 1600), KOMPLEX, "komplex: lange Nachricht"),
        (
            nachricht("Zeig mir das", bilder=(Bild("image/jpeg", b"x"),)),
            KOMPLEX,
            "komplex: Foto oder PDF",
        ),
        (
            nachricht("Status", dateien=(DateiHinweis("v1", "Rechnung.PDF", "", 10),)),
            KOMPLEX,
            "komplex: Foto oder PDF",
        ),
        (
            nachricht("Zeig mir das", dateien=(DateiHinweis("v1", "liste.csv", "text/csv", 10),)),
            STANDARD,
            "standard",
        ),
    ],
)
def test_router_regeln(user, eingang, stufe, grund):
    wahl = waehle_modell(eingang, user)
    assert wahl.stufe == stufe
    assert grund in wahl.grund


def test_router_grund_enthaelt_keine_nachrichteninhalte(user):
    for text in ("Zeig mir das Gehalt von Lea Beispiel", "Analyse zu Kunde Geheim GmbH"):
        grund = waehle_modell(nachricht(text), user).grund
        assert "Lea" not in grund and "Geheim" not in grund


def test_vorgabe_aus_modell_befehl_geht_vor(user):
    wahl = waehle_modell(nachricht("Mach eine Analyse"), user, EINFACH)
    assert (wahl.stufe, wahl.grund) == (EINFACH, "einfach: von dir gewählt (/modell)")
    assert waehle_modell(nachricht("Mach eine Analyse"), user, AUTO).stufe == KOMPLEX


# ---------------------------------------------------------------- Schleife


@pytest.mark.parametrize(
    ("text", "modell"),
    [
        ("Zeig mir meine Aufgaben", M_EINFACH),
        ("Hi", M_STANDARD),
        ("Mach eine Analyse der Verkäufe", M_KOMPLEX),
    ],
)
async def test_schleife_nutzt_das_modell_der_stufe_und_bucht_den_grund(
    baue_agent, user, session_fabrik, text, modell
):
    client = FakeAnthropic(claude_antwort(text_block("Antwort")))
    antwort = await baue_agent(client).beantworte(nachricht(text), user)
    assert antwort.text == "Antwort"
    assert [a["model"] for a in client.aufrufe] == [modell]
    (buchung,) = await _buchungen(session_fabrik)
    assert buchung.modell == modell
    assert buchung.grund_modellwahl == waehle_modell(nachricht(text), user).grund


async def test_einfaches_modell_mit_lesendem_tool_bleibt_einfach(baue_agent, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_lesen", {"text": "a"})),
        claude_antwort(text_block("Fertig")),
    )
    antwort = await baue_agent(client).beantworte(nachricht("Zeig mir die Liste"), user)
    assert antwort.text == "Fertig"
    assert [a["model"] for a in client.aufrufe] == [M_EINFACH, M_EINFACH]


@pytest.mark.parametrize(
    ("erste_antwort", "grund"),
    [
        (claude_antwort(text_block("")), "leere Antwort"),
        (claude_antwort(tool_use_block("gibt_es_nicht", {})), "ungültiger Tool-Aufruf"),
        # Pflichtfeld `text` fehlt
        (claude_antwort(tool_use_block("beispiel_lesen", {})), "ungültiger Tool-Aufruf"),
        (claude_antwort(tool_use_block("beispiel_schreiben", {"text": "a"})), "schreibendes Tool"),
    ],
)
async def test_eskalation_einmal_auf_standard_und_beide_laeufe_gebucht(
    baue_agent, user, session_fabrik, erste_antwort, grund
):
    client = FakeAnthropic(erste_antwort, claude_antwort(text_block("Vom Standardmodell")))
    antwort = await baue_agent(client).beantworte(nachricht("Zeig mir die Liste"), user)

    assert antwort.text == "Vom Standardmodell"
    assert antwort.freigaben == ()
    assert [a["model"] for a in client.aufrufe] == [M_EINFACH, M_STANDARD]
    # Der zweite Lauf beginnt neu: nur die Nutzernachricht, nichts vom gescheiterten Versuch.
    assert [m["role"] for m in client.aufrufe[1]["messages"]] == ["user"]
    erste, zweite = await _buchungen(session_fabrik)
    assert (erste.modell, zweite.modell) == (M_EINFACH, M_STANDARD)
    assert erste.kosten_eur > 0 and zweite.kosten_eur > 0
    assert erste.grund_modellwahl.startswith("einfach: kurze Lesefrage")
    assert zweite.grund_modellwahl == f"standard: eskaliert von einfach ({grund})"


async def test_eskalation_nur_einmal_und_nie_vom_standardmodell(baue_agent, user, session_fabrik):
    """Bleibt auch das Standardmodell leer, gibt es keinen dritten Versuch mit neuem Modell."""
    client = FakeAnthropic(claude_antwort(text_block("")))
    await baue_agent(client).beantworte(nachricht("Zeig mir die Liste"), user)
    modelle = [a["model"] for a in client.aufrufe]
    assert modelle[0] == M_EINFACH
    assert set(modelle[1:]) == {M_STANDARD}

    client = FakeAnthropic(claude_antwort(text_block("")))
    await baue_agent(client).beantworte(nachricht("Hi"), user)
    assert {a["model"] for a in client.aufrufe} == {M_STANDARD}


async def test_tool_mit_kennzeichen_komplex_schaltet_auf_das_starke_modell(
    baue_agent, user, session_fabrik, monkeypatch
):
    monkeypatch.setattr(BeispielLesen, "komplex", True)
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_lesen", {"text": "a"})),
        claude_antwort(text_block("Fertig")),
    )
    await baue_agent(client).beantworte(nachricht("Hi"), user)
    assert [a["model"] for a in client.aufrufe] == [M_STANDARD, M_KOMPLEX]
    erste, zweite = await _buchungen(session_fabrik)
    assert zweite.grund_modellwahl == "komplex: Tool beispiel_lesen"


async def test_nicht_verfuegbares_modell_faellt_auf_standard_zurueck(
    baue_agent, user, session_fabrik, caplog
):
    client = FakeAnthropic(_nicht_gefunden(), claude_antwort(text_block("Antwort")))
    agent = baue_agent(client)
    with caplog.at_level(logging.WARNING, logger="app.agent.loop"):
        antwort = await agent.beantworte(nachricht("Mach eine Analyse"), user)

    assert antwort.text == "Antwort"
    assert [a["model"] for a in client.aufrufe] == [M_KOMPLEX, M_STANDARD]
    assert f"Modell {M_KOMPLEX} ist nicht verfügbar" in caplog.text
    # Gebucht wird das Modell, das wirklich geantwortet hat.
    assert [b.modell for b in await _buchungen(session_fabrik)] == [M_STANDARD]
    # Danach wird das fehlende Modell in dieser Laufzeit nicht erneut versucht.
    await agent.beantworte(nachricht("Noch eine Analyse"), user)
    assert client.aufrufe[-1]["model"] == M_STANDARD


async def test_fehlt_auch_das_standardmodell_gibt_es_die_normale_fehlermeldung(baue_agent, user):
    client = FakeAnthropic(_nicht_gefunden())
    antwort = await baue_agent(client).beantworte(nachricht("Hi"), user)
    assert len(client.aufrufe) == 1
    assert antwort.text


# ---------------------------------------------------------------- /modell


async def test_modell_befehl_gilt_pro_person(
    settings, session_fabrik, registry, freigaben, kosten, user, admin
):
    from app.agent.loop import Agent

    vorgaben = ModellVorgaben()
    befehle = Befehle(session_fabrik, kosten=kosten, vorgaben=vorgaben)
    client = FakeAnthropic(claude_antwort(text_block("Antwort")))
    agent = Agent(settings, session_fabrik, client, registry, freigaben, kosten, vorgaben)

    async def befehl(nutzer, *argumente) -> str:
        return (await befehle.fuehre_aus("modell", nutzer, list(argumente), 1, True)).text

    assert "Modellstufe: auto" in await befehl(user)
    assert await befehl(user, "Komplex") == "Modellstufe: komplex."
    assert "Möglich sind" in await befehl(user, "riesig")
    assert "Modellstufe: komplex" in await befehl(user)
    assert "Modellstufe: auto" in await befehl(admin)

    await agent.beantworte(nachricht("Hi"), user)
    await agent.beantworte(nachricht("Hi"), admin)
    assert [a["model"] for a in client.aufrufe] == [M_KOMPLEX, M_STANDARD]

    assert await befehl(user, "auto") == "Modellstufe: auto."
    await agent.beantworte(nachricht("Hi"), user)
    assert client.aufrufe[-1]["model"] == M_STANDARD


async def test_grund_der_modellwahl_steht_in_kosten(baue_agent, kosten, session_fabrik, user):
    client = FakeAnthropic(claude_antwort(text_block("Antwort")))
    agent = baue_agent(client)
    await agent.beantworte(nachricht("Zeig mir meine Aufgaben"), user)
    await agent.beantworte(nachricht("Mach eine Analyse"), user)
    befehle = Befehle(session_fabrik, kosten=kosten)
    text = (await befehle.fuehre_aus("kosten", user, ["1"], 1, True)).text
    assert "Modellwahl:" in text
    assert "- einfach: kurze Lesefrage (Stichwort zeig mir): 1" in text
    assert "- komplex: Stichwort analyse: 1" in text
    assert f"- {M_EINFACH}:" in text and f"- {M_KOMPLEX}:" in text


# ---------------------------------------------------------------- Prompt-Caching


async def test_cache_marken_am_festen_prompt_und_am_letzten_tool(baue_agent, registry, user, admin):
    client = FakeAnthropic(claude_antwort(text_block("Antwort")))
    agent = baue_agent(client)
    await agent.beantworte(nachricht("Hi"), user)
    await agent.beantworte(nachricht("Etwas anderes"), user)
    await agent.beantworte(nachricht("Hi"), admin)
    erster, zweiter, vom_admin = client.aufrufe

    fest, persoenlich = erster["system"]
    assert fest["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in persoenlich
    # Der feste Teil ist für alle gleich; Datum, Name und Notizen stehen dahinter.
    assert fest == zweiter["system"][0] == vom_admin["system"][0]
    assert "Heute ist" not in fest["text"] and "Heute ist" in persoenlich["text"]

    tools = erster["tools"]
    assert [t["name"] for t in tools] == sorted(t["name"] for t in tools)
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in tools[:-1])
    # Gleiche Rechte: Byte für Byte dieselbe Tool-Liste, sonst greift der Cache nicht.
    assert tools == zweiter["tools"]
    # Die Marke verändert die Definitionen in der Registry nicht.
    assert all("cache_control" not in t for t in registry.api_definitionen(user))
    assert len(vom_admin["tools"]) == len(tools) + 1


async def test_cache_tokens_werden_getrennt_gebucht(baue_agent, user, session_fabrik):
    antwort = claude_antwort(text_block("Antwort"), input_tokens=200, output_tokens=300)
    antwort.usage.cache_read_input_tokens = 10_000
    antwort.usage.cache_creation_input_tokens = 40
    await baue_agent(FakeAnthropic(antwort)).beantworte(nachricht("Hi"), user)
    (b,) = await _buchungen(session_fabrik)
    assert (b.eingabe_tokens, b.ausgabe_tokens) == (200, 300)
    assert (b.cache_lese_tokens, b.cache_schreib_tokens) == (10_000, 40)


def test_kostenvergleich_vorher_nachher():
    """Rechnung aus der Preistabelle für eine typische Anfrage mit einem Tool-Aufruf:
    zwei Runden, je rund 10.000 Tokens fester Vorspann (Prompt und Tool-Definitionen),
    200 Tokens Gespräch und 300 Tokens Antwort. Die 10.000 sind eine Schätzung."""
    vorspann, rest, ausgabe, runden = 10_000, 200, 300, 2
    sonnet, haiku = PREISE["claude-sonnet-5-5"], PREISE["claude-haiku-4-5"]

    vorher = runden * kosten_usd(sonnet, vorspann + rest, ausgabe)
    warm = runden * kosten_usd(sonnet, rest, ausgabe, cache_lesen=vorspann)
    # Erste Anfrage nach einer Pause: Runde 1 schreibt den Cache, Runde 2 liest ihn.
    kalt = kosten_usd(sonnet, rest, ausgabe, cache_schreiben=vorspann) + kosten_usd(
        sonnet, rest, ausgabe, cache_lesen=vorspann
    )
    einfach = runden * kosten_usd(haiku, rest, ausgabe, cache_lesen=vorspann)

    assert vorher == Decimal("0.0468")
    assert warm == Decimal("0.0088")
    assert kalt == Decimal("0.0328")
    assert einfach == Decimal("0.0054")
