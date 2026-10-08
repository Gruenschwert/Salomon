"""Lange Texte, große Änderungssätze, Sendefehler, Rundenlimit und Sammelabfragen
(Fehlerbild vom 08.10.2026)."""

import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from telegram.error import BadRequest

from app.agent.history import lade_verlauf
from app.agent.loop import (
    FORTSETZEN_TEXT,
    GEKUERZT_HINWEIS,
    LEERE_ANTWORT_TEXT,
    NUR_FREIGABE_TEXT,
    TOOL_ABGESCHNITTEN_TEXT,
    Agent,
)
from app.agent.prompts import baue_system_prompt
from app.auth.approvals import STATUS_BEREITS_ENTSCHIEDEN, Freigaben
from app.channels.base import Antwort, EingehendeNachricht
from app.channels.telegram import (
    TEIL_MAX_ZEICHEN,
    TELEGRAM_MAX_ZEICHEN,
    TEXT_MAX_ZEICHEN,
    TelegramKanal,
    teile_text,
)
from app.config import Settings
from app.db.models import Approval, AuditLog, Message, jetzt
from app.tools.asana_lesen import MAX_SAMMEL, AsanaAufgabenSuchen
from app.tools.base import ToolFehler
from app.tools.registry import fuehre_tool_aus, lade_registry
from tests.asana_fake import ASANA_TOKEN, FakeAsana, asana_kontext
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, system_text, text_block, tool_use_block
from tests.test_asana_schreiben import aufgabe

# Jeder Test handelt als Mitarbeiter mit eigenem, verbundenem Asana-Zugang.
pytestmark = pytest.mark.usefixtures("als_nutzer")

NAME = "asana_aenderungen_ausfuehren"
CHAT_ID = 5
SUCHE = "/workspaces/ws1/tasks/search"
GIDS_57 = [str(1000 + n) for n in range(57)]
ERLEDIGE_57 = [{"operation": "aufgabe_erledigen", "gids": GIDS_57}]


def nachricht(text: str) -> EingehendeNachricht:
    return EingehendeNachricht(
        chat_id=CHAT_ID, absender_id=ERLAUBT_ID, absender_name="X", text=text
    )


# ---------------------------------------------------------------- 1. Lange Texte


def _langer_text(zeichen: int) -> str:
    zeilen = []
    nummer = 0
    while sum(len(z) + 1 for z in zeilen) < zeichen:
        nummer += 1
        zeilen.append(
            f"{nummer}. Aufgabe Nummer {nummer} mit Link "
            f"https://app.asana.com/0/1200000000000000/{1300000000000000 + nummer} und etwas Text"
        )
        if nummer % 7 == 0:
            zeilen.append("")
    return "\n".join(zeilen)[:zeichen].rsplit("\n", 1)[0]


def test_text_mit_12000_zeichen_wird_an_zeilenenden_geteilt():
    text = _langer_text(12000)
    assert 11000 < len(text) <= 12000
    teile = teile_text(text)

    assert len(teile) == 4
    assert all(len(teil) <= TEIL_MAX_ZEICHEN < TELEGRAM_MAX_ZEICHEN for teil in teile)
    # Keine Zeile ist zerrissen: Jede Zeile jedes Teils steht genau so im Original.
    original = set(text.split("\n"))
    for teil in teile:
        assert set(teil.split("\n")) <= original
    # Es fehlt nichts und nichts ist doppelt.
    ohne_leer = [z for z in text.split("\n") if z]
    assert [z for teil in teile for z in teil.split("\n") if z] == ohne_leer


def test_eine_ueberlange_zeile_wird_an_leerzeichen_getrennt_nie_in_der_url():
    url = "https://app.asana.com/0/1200000000000000/1300000000000000/f?" + "x=1&" * 40
    zeile = " ".join(["wort"] * 700 + [url] + ["ende"] * 300)
    assert "\n" not in zeile and len(zeile) > TEIL_MAX_ZEICHEN
    teile = teile_text(zeile)
    assert len(teile) > 1
    assert all(len(teil) <= TEIL_MAX_ZEICHEN for teil in teile)
    assert sum(url in teil for teil in teile) == 1
    assert " ".join(teile) == zeile


def test_kurze_und_leere_texte():
    assert teile_text("") == []
    assert teile_text("\n\n") == []
    assert teile_text("kurz") == ["kurz"]
    assert teile_text("a\n\nb") == ["a\n\nb"]


@pytest.fixture
def kanal(settings, session_fabrik, freigaben, kosten, alarme, user, monkeypatch):
    kanal = TelegramKanal(
        settings, session_fabrik, handler=None, freigaben=freigaben, kosten=kosten, alarme=alarme
    )
    kanal.gesendet = []
    kanal.dateien = []
    kanal.sendefehler = None

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if kanal.sendefehler and kanal.sendefehler(text, reply_markup):
            raise BadRequest("Message is too long")
        assert len(text) <= TELEGRAM_MAX_ZEICHEN
        kanal.gesendet.append((text, reply_markup))

    async def send_document(self, chat_id, document, **kwargs):
        kanal.dateien.append((document.filename, document.input_file_content.decode("utf-8")))

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    monkeypatch.setattr(type(kanal.application.bot), "send_document", send_document)
    return kanal


async def test_bis_20000_zeichen_wird_nichts_gekuerzt(kanal):
    text = _langer_text(19000)
    await kanal.sende_text(CHAT_ID, text)
    assert kanal.dateien == []
    assert "\n".join(t for t, _ in kanal.gesendet).replace("\n\n", "\n") == text.replace(
        "\n\n", "\n"
    )


async def test_ueber_20000_zeichen_kommt_hinweis_und_der_ganze_text_als_datei(kanal):
    text = _langer_text(26000)
    assert len(text) > TEXT_MAX_ZEICHEN
    await kanal.sende_text(CHAT_ID, text)
    (anfang, _), (hinweis, _) = kanal.gesendet
    assert text.startswith(anfang)
    assert f"{len(text)} Zeichen lang" in hinweis and "Datei" in hinweis
    assert kanal.dateien == [("text.txt", text)]


# ---------------------------------------------------------------- 2. Große Freigaben


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    for gid in GIDS_57:
        fake.route(
            "GET",
            f"/tasks/{gid}",
            aufgabe(gid, f"Überfällige Aufgabe {gid}: Etiketten für den Versand nachbestellen"),
        )
        fake.route("PUT", f"/tasks/{gid}", {"gid": gid})
    return fake


@pytest.fixture
def asana(kontext, fake):
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    return akontext, registry, Freigaben(akontext, registry)


@pytest.fixture
def akanal(asana, session_fabrik, kosten, alarme, kanal):
    kanal._freigaben = asana[2]
    return kanal


async def test_sammelform_mit_57_gids_ergibt_57_operationen(asana, user, fake):
    _, registry, freigaben = asana
    anfrage = await freigaben.anfragen(user, registry.hole(NAME), {"operationen": ERLEDIGE_57})
    zeilen = anfrage.vorschau_text.splitlines()
    assert zeilen[0] == "Asana-Änderungssatz: 57 ändern"
    assert len(zeilen) == 58
    assert zeilen[57].startswith("57. Erledigen: „Überfällige Aufgabe 1056")
    assert anfrage.anzahl == 57

    entscheidung = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert entscheidung.text.startswith("✅ Asana-Änderungssatz ausgeführt: 57 geändert.")
    assert [pfad for _, pfad in fake.aufrufe("PUT")] == [f"/tasks/{gid}" for gid in GIDS_57]


async def test_sammelform_zaehlt_ins_limit_und_laesst_sich_nicht_mischen(kontext, fake, user):
    akontext = asana_kontext(kontext, fake, asana_max_ops_per_changeset=56)
    tool = lade_registry(akontext).hole(NAME)
    with pytest.raises(ToolFehler, match="hat 57 Operationen, erlaubt sind höchstens 56"):
        await tool.bereite_vor(operationen=ERLEDIGE_57)
    for op, meldung in (
        ({"operation": "aufgabe_erledigen", "gids": ["1000"], "gid": "1001"}, "nicht mit „gid“"),
        ({"operation": "aufgabe_erledigen", "gids": []}, "nicht leere Liste"),
        ({"operation": "aufgabe_erledigen", "gids": "1000"}, "nicht leere Liste"),
        ({"operation": "aufgabe_erledigen", "gids": ["1/../2"]}, "gültige Asana-GID"),
        ({"operation": "projekt_anlegen", "name": "x", "gids": ["1"]}, "kennt die Felder gid"),
    ):
        with pytest.raises(ToolFehler, match=meldung):
            await tool.bereite_vor(operationen=[op])
    assert fake.aufrufe("PUT") == []


async def test_freigabe_mit_57_operationen_kommt_kompakt_mit_datei_und_buttons(asana, akanal, user):
    _, registry, freigaben = asana
    anfrage = await freigaben.anfragen(user, registry.hole(NAME), {"operationen": ERLEDIGE_57})
    assert len(anfrage.vorschau_text) > TELEGRAM_MAX_ZEICHEN
    assert anfrage.kompakt_text.splitlines()[:3] == [
        "Asana-Änderungssatz: 57 ändern",
        "57 mal Aufgabe erledigen",
        "Die ersten 10:",
    ]
    assert len(anfrage.kompakt_text.splitlines()) == 14
    assert anfrage.kompakt_text.splitlines()[-1] == (
        "… und 47 weitere. Die vollständige Liste steht in der Datei."
    )

    await akanal._sende(
        CHAT_ID, Antwort(text="Ich hake 57 Aufgaben ab.", freigaben=(anfrage,)), ERLAUBT_ID
    )

    texte = [text for text, _ in akanal.gesendet]
    assert texte[0] == "Ich hake 57 Aufgaben ab."
    assert texte[1].startswith("Freigabe erforderlich:\nAsana-Änderungssatz: 57 ändern")
    assert texte[-1] == "Freigabe für 57 Änderungen, gültig 15 Minuten"
    # Die Buttons hängen an der kurzen letzten Nachricht, sonst nirgends.
    assert [buttons is not None for _, buttons in akanal.gesendet] == [False, False, True]
    assert [b.callback_data for b in akanal.gesendet[-1][1].inline_keyboard[0]] == [
        f"freigabe:{anfrage.approval_id}:ja",
        f"freigabe:{anfrage.approval_id}:nein",
    ]
    assert akanal.dateien == [(f"aenderungssatz_{anfrage.approval_id}.txt", anfrage.vorschau_text)]


async def test_bis_15_gleichartige_operationen_bleibt_die_vorschau_vollstaendig(
    asana, akanal, user
):
    _, registry, freigaben = asana
    ops = [{"operation": "aufgabe_erledigen", "gids": GIDS_57[:15]}]
    anfrage = await freigaben.anfragen(user, registry.hole(NAME), {"operationen": ops})
    assert anfrage.kompakt_text is None and anfrage.anzahl == 15
    await akanal.sende_freigabe_anfrage(CHAT_ID, anfrage)
    assert akanal.dateien == []
    assert akanal.gesendet[0][0] == f"Freigabe erforderlich:\n{anfrage.vorschau_text}"
    assert akanal.gesendet[-1][0] == "Freigabe für 15 Änderungen, gültig 15 Minuten"


async def test_loeschungen_sind_auch_in_der_kompakten_vorschau_markiert(kontext, fake, admin):
    for gid in GIDS_57[:20]:
        fake.route("DELETE", f"/tasks/{gid}", {})
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    anfrage = await Freigaben(akontext, registry).anfragen(
        admin,
        registry.hole(NAME),
        {
            "operationen": [
                {"operation": "aufgabe_loeschen", "gids": GIDS_57[:20]},
                {"operation": "aufgabe_erledigen", "gid": GIDS_57[30]},
            ]
        },
    )
    assert anfrage.kompakt_text.splitlines()[:3] == [
        "Asana-Änderungssatz: 1 ändern, 20 🗑 löschen",
        "🗑 20 mal Aufgabe löschen",
        "1 mal Aufgabe erledigen",
    ]
    assert "**" not in anfrage.kompakt_text


# ---------------------------------------------------------------- 3. Sendefehler


async def test_sendefehler_verwirft_den_satz_und_sagt_es_dem_nutzer(
    asana, akanal, user, fake, session_fabrik, alarm_texte, caplog
):
    _, registry, freigaben = asana
    anfrage = await freigaben.anfragen(user, registry.hole(NAME), {"operationen": ERLEDIGE_57})
    # Telegram lehnt genau die Nachricht mit den Buttons ab.
    akanal.sendefehler = lambda text, buttons: buttons is not None

    with caplog.at_level(logging.ERROR):
        await akanal._sende(
            CHAT_ID, Antwort(text="Änderungssatz vorbereitet.", freigaben=(anfrage,)), ERLAUBT_ID
        )

    assert akanal.gesendet[-1][0] == (
        "Die Freigabe konnte nicht gesendet werden: BadRequest: Message is too long. "
        "Es wurde nichts geändert."
    )
    async with session_fabrik() as session:
        approval = await session.get(Approval, anfrage.approval_id)
        eintraege = list(await session.scalars(select(AuditLog)))
        (hinweis,) = list(await session.scalars(select(Message)))
    assert approval.status == "abgelehnt"
    assert any(e.fehler and e.fehler.startswith("nicht zustellbar") for e in eintraege)
    # Claude erfährt es beim nächsten Mal aus dem Verlauf.
    assert "konnte nicht gesendet werden" in hinweis.inhalt
    assert f"Freigabe #{anfrage.approval_id} konnte nicht gesendet werden" in caplog.text
    assert "BadRequest" in caplog.text
    assert alarm_texte and "nicht zustellbar" in alarm_texte[0][1]
    # Der verworfene Satz lässt sich nicht mehr ausführen.
    spaet = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert spaet.status == STATUS_BEREITS_ENTSCHIEDEN
    assert fake.aufrufe("PUT") == []


async def test_grund_des_sendefehlers_enthaelt_keine_secrets(asana, akanal, user, monkeypatch):
    akontext, registry, freigaben = asana
    akanal._geheimnisse = (ASANA_TOKEN,)
    anfrage = await freigaben.anfragen(
        user,
        registry.hole(NAME),
        {"operationen": [{"operation": "aufgabe_erledigen", "gid": "1000"}]},
    )

    async def kaputt(chat_id, anfrage):
        raise RuntimeError(f"Verbindung mit Bearer {ASANA_TOKEN} abgelehnt")

    monkeypatch.setattr(akanal, "sende_freigabe_anfrage", kaputt)
    await akanal._sende(CHAT_ID, Antwort(text="x", freigaben=(anfrage,)), ERLAUBT_ID)
    assert "RuntimeError: Verbindung mit Bearer *** abgelehnt" in akanal.gesendet[-1][0]
    assert ASANA_TOKEN not in akanal.gesendet[-1][0]


# ---------------------------------------------------------------- 4. Fallback, Limits


def _abgeschnitten(*bloecke) -> SimpleNamespace:
    return claude_antwort(*bloecke, stop_reason="max_tokens")


async def test_fallback_satz_nur_ohne_text_und_mit_logeintrag(baue_agent, user, caplog):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_lesen", {"text": "a"})),
        claude_antwort(tool_use_block("beispiel_fehler", {})),
        claude_antwort(stop_reason="end_turn"),
    )
    with caplog.at_level(logging.WARNING):
        antwort = await baue_agent(client).beantworte(nachricht("x"), user)
    assert antwort.text == LEERE_ANTWORT_TEXT
    assert (
        "Leere Antwort von Claude: stop_reason=end_turn, Runden=3, Tool-Aufrufe=2, "
        "letztes Tool=beispiel_fehler"
    ) in caplog.text


async def test_ohne_text_aber_mit_freigabe_kommt_kein_fallback_satz(baue_agent, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_schreiben", {"text": "x"})),
        claude_antwort(stop_reason="end_turn"),
    )
    antwort = await baue_agent(client).beantworte(nachricht("x"), user)
    assert antwort.text == NUR_FREIGABE_TEXT
    assert len(antwort.freigaben) == 1


async def test_abgeschnittener_tool_aufruf_wird_nicht_ausgefuehrt_und_kuerzer_wiederholt(
    baue_agent, user, session_fabrik, caplog
):
    """Der Fall vom 08.10.: Der Tool-Aufruf mit 57 Operationen endet am Ausgabelimit."""
    from tests.beispiel_tools.schreibend import BeispielSchreiben

    client = FakeAnthropic(
        _abgeschnitten(
            text_block("Änderungssatz vorbereitet."),
            tool_use_block("beispiel_schreiben", {}),
        ),
        claude_antwort(tool_use_block("beispiel_schreiben", {"text": "kurz"})),
        claude_antwort(text_block("Bitte gib den Änderungssatz frei.")),
    )
    with caplog.at_level(logging.WARNING):
        antwort = await baue_agent(client).beantworte(nachricht("Hake alle ab"), user)

    assert antwort.text == "Bitte gib den Änderungssatz frei."
    assert len(antwort.freigaben) == 1
    assert BeispielSchreiben.ausgefuehrt == []
    # Der unvollständige Aufruf geht nicht in den Verlauf; Claude bekommt den Hinweis.
    zweiter = client.aufrufe[1]["messages"]
    assert zweiter[-2] == {"role": "assistant", "content": "Änderungssatz vorbereitet."}
    assert zweiter[-1] == {"role": "user", "content": TOOL_ABGESCHNITTEN_TEXT}
    assert "gids" in TOOL_ABGESCHNITTEN_TEXT
    assert "Tool-Aufruf am Ausgabelimit abgeschnitten" in caplog.text


async def test_zweimal_abgeschnitten_ergibt_ehrliche_meldung_statt_schweigen(
    settings, baue_agent, user
):
    client = FakeAnthropic(_abgeschnitten(tool_use_block("beispiel_schreiben", {})))
    antwort = await baue_agent(client).beantworte(nachricht("Hake alle ab"), user)
    assert len(client.aufrufe) == 2
    assert antwort.freigaben == ()
    assert f"MAX_OUTPUT_TOKENS={settings.max_output_tokens}" in antwort.text
    assert "Es wurde nichts vorbereitet und nichts geändert" in antwort.text
    assert antwort.text != LEERE_ANTWORT_TEXT


async def test_lange_textantwort_wird_fortgesetzt_statt_abgeschnitten(baue_agent, user):
    client = FakeAnthropic(
        _abgeschnitten(text_block("Teil 1 der Liste")),
        _abgeschnitten(text_block("Teil 2 der Liste")),
        claude_antwort(text_block("Teil 3 und Schluss")),
    )
    antwort = await baue_agent(client).beantworte(nachricht("Zeig alles"), user)
    assert antwort.text == "Teil 1 der Liste\nTeil 2 der Liste\nTeil 3 und Schluss"
    assert client.aufrufe[1]["messages"][-1] == {"role": "user", "content": FORTSETZEN_TEXT}
    # Reicht auch das nicht, endet die Antwort mit einem klaren Hinweis.
    endlos = FakeAnthropic(_abgeschnitten(text_block("immer weiter")))
    antwort = await baue_agent(endlos).beantworte(nachricht("Zeig alles"), user)
    assert len(endlos.aufrufe) == 3
    assert antwort.text.endswith(GEKUERZT_HINWEIS)
    assert "Schreib „weiter“" in GEKUERZT_HINWEIS


async def test_rundenlimit_ist_einstellbar_und_meldet_vorbereitete_freigaben(
    settings, session_fabrik, registry, freigaben, kosten, user
):
    knapp = settings.model_copy(update={"agent_max_rounds": 3})
    client = FakeAnthropic(claude_antwort(tool_use_block("beispiel_schreiben", {"text": "x"})))
    agent = Agent(knapp, session_fabrik, client, registry, freigaben, kosten)
    antwort = await agent.beantworte(nachricht("Endlos"), user)
    assert len(client.aufrufe) == 3
    assert antwort.text.startswith("Ich habe nach 3 Runden aufgehört")
    assert "3 Änderungssätze sind vorbereitet und warten auf deine Freigabe." in antwort.text
    assert len(antwort.freigaben) == 3


def test_agent_max_rounds_aus_der_umgebung_und_leer(monkeypatch):
    from tests.test_config import PFLICHT

    for name, wert in PFLICHT.items():
        monkeypatch.setenv(name, wert)
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "")
    assert Settings(_env_file=None).agent_max_rounds == 25
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "40")
    assert Settings(_env_file=None).agent_max_rounds == 40
    # Die frühere Variable stört nicht mehr.
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "8")
    assert Settings(_env_file=None).agent_max_rounds == 40


# ---------------------------------------------------------------- 5. Sammelabfragen


def _zeile(gid: str, name: str, faellig: str | None, projekt: str = "Launch", **extra) -> dict:
    return {
        "gid": gid,
        "name": name,
        "completed": False,
        "due_on": faellig,
        "created_at": f"2026-01-01T00:00:{int(gid) % 60:02d}.000Z",
        "memberships": [
            {"project": {"gid": "100", "name": projekt}, "section": {"gid": "201", "name": "Offen"}}
        ],
        **extra,
    }


@pytest.fixture
def suche(kontext):
    fake = FakeAsana()
    return fake, AsanaAufgabenSuchen(asana_kontext(kontext, fake))


async def test_stichtag_bis_inklusive_august_2026(suche):
    fake, tool = suche
    fake.route(
        "GET",
        SUCHE,
        [
            _zeile("1", "Am Stichtag", "2026-08-31"),
            _zeile("2", "Einen Tag danach", "2026-09-01"),
            _zeile("3", "Im Juli", "2026-07-15"),
            _zeile("4", "Ohne Datum", None),
        ],
    )
    ergebnis = await tool.ausfuehren(faellig_bis="2026-08-31")
    assert ergebnis["gesamt"] == 2
    assert ergebnis["stichtag"] == "fällig am oder vor dem 31.08.2026"
    assert ergebnis["aufgaben"] == [
        "3 | 2026-07-15 | Launch | Im Juli",
        "1 | 2026-08-31 | Launch | Am Stichtag",
    ]
    params = fake.anfragen[0].url.params
    # Die Suche wird einen Tag weiter gefasst, damit der Stichtag sicher dabei ist.
    assert params["due_on.before"] == "2026-09-01"
    assert params["completed"] == "false"


async def test_ueberfaellig_heisst_vor_heute_und_offen(suche):
    fake, tool = suche
    heute = datetime.now(ZoneInfo("Europe/Berlin")).date()
    fake.route(
        "GET",
        SUCHE,
        [
            _zeile("1", "Gestern", (heute - timedelta(days=1)).isoformat()),
            _zeile("2", "Heute", heute.isoformat()),
            _zeile("3", "Erledigt", "2026-01-01", completed=True),
        ],
    )
    ergebnis = await tool.ausfuehren(ueberfaellig=True, zustaendig="ich")
    assert [zeile.split(" | ")[0] for zeile in ergebnis["aufgaben"]] == ["1"]
    params = fake.anfragen[0].url.params
    assert params["due_on.before"] == heute.isoformat()
    assert params["assignee.any"] == "me"
    # Ein früherer Stichtag gewinnt gegen „überfällig“.
    await tool.ausfuehren(ueberfaellig=True, faellig_bis="2026-03-31")
    assert fake.anfragen[-1].url.params["due_on.before"] == "2026-04-01"


async def test_alle_seiten_werden_selbst_geholt_bis_zur_obergrenze(suche):
    fake, tool = suche
    alle = [_zeile(str(n), f"Aufgabe {n}", "2026-01-10") for n in range(1, 251)]

    def seite(anfrage: httpx.Request) -> httpx.Response:
        # Wie Asana: die 100 jüngsten, die älter sind als created_at.before.
        vor = anfrage.url.params.get("created_at.before")
        start = (
            0 if vor is None else next(i for i, a in enumerate(alle) if a["created_at"] == vor) + 1
        )
        return httpx.Response(200, json={"data": alle[start : start + 100]})

    for nummer, eintrag in enumerate(alle):
        eintrag["created_at"] = f"2026-01-01T00:00:00.{nummer:06d}Z"
    fake.route("GET", SUCHE, seite)

    ergebnis = await tool.ausfuehren(projekt="100", abschnitt="201")
    assert (ergebnis["gesamt"], ergebnis["angezeigt"]) == (250, 250)
    assert len(fake.anfragen) == 3
    assert fake.anfragen[0].url.params["sections.any"] == "201"
    assert "created_at.before" not in fake.anfragen[0].url.params
    assert "hinweis" not in ergebnis

    alle.extend(_zeile(str(n), f"Aufgabe {n}", "2026-01-10") for n in range(251, 451))
    for nummer, eintrag in enumerate(alle):
        eintrag["created_at"] = f"2026-01-01T00:00:00.{nummer:06d}Z"
    voll = await tool.ausfuehren(projekt="100")
    assert voll["angezeigt"] == MAX_SAMMEL == 300
    assert "Obergrenze 300" in voll["hinweis"]


async def test_kompakte_zeile_kuerzt_namen_und_passt_als_ganzes_ins_ergebnis(suche, user, kontext):
    fake, tool = suche
    fake.route(
        "GET",
        SUCHE,
        httpx.Response(
            200,
            json={
                "data": [_zeile(str(n), "N" * 200, "2026-01-10", "P" * 80) for n in range(1, 100)]
            },
        ),
    )
    ergebnis = await fuehre_tool_aus(tool, {"faellig_bis": "2026-08-31"}, user, tool.kontext)
    assert not ergebnis.fehler
    gid, faellig, projekt, name = ergebnis.daten["aufgaben"][0].split(" | ")
    assert (len(name), len(projekt)) == (60, 30)
    assert name.endswith("…")
    # 99 Zeilen passen ungekürzt hinein; mit dem alten Limit von 8000 Zeichen wären sie es nicht.
    assert 8000 < len(ergebnis.text) < 40000
    assert "gekürzt" not in ergebnis.text
    assert ergebnis.daten["gesamt"] == 99


async def test_alte_parameternamen_gelten_weiter(suche):
    fake, tool = suche
    fake.route("GET", SUCHE, [_zeile("1", "A", "2026-01-10", completed=True)])
    ergebnis = await tool.ausfuehren(projekt_gid="100", zustaendig_gid="me", nur_offene=False)
    assert ergebnis["gesamt"] == 1
    params = fake.anfragen[0].url.params
    assert (params["projects.any"], params["assignee.any"]) == ("100", "me")
    assert "completed" not in params


# ---------------------------------------------------------------- 6./7. Systemprompt


def test_systemprompt_regeln_fuer_sammelaufgaben_und_offene_freigaben():
    prompt = baue_system_prompt(datetime(2026, 10, 8, 13, 10, tzinfo=ZoneInfo("Europe/Berlin")))
    for stichwort in (
        "Sammelaufgaben",
        "Zähle zuerst",
        "frage nicht Projekt für Projekt",
        "Nenne dem Nutzer die Anzahl und den Stichtag",
        "fällig am \noder vor dem 31.08.2026".replace("\n", ""),
        "EINEN Änderungssatz",
        "Sammelform „gids“",
        "teilst du in Pakete",
        "Offene oder verworfene Freigaben",
        "bereitest den Änderungssatz neu vor",
        "du schweigst nie",
    ):
        assert stichwort in prompt, stichwort
    assert "Stand der letzten Freigaben" not in prompt.split("Heute ist")[1]


async def test_stand_der_freigaben_steht_fuer_claude_im_systemprompt(
    baue_agent, freigaben, registry, user, session_fabrik
):
    tool = registry.hole("beispiel_schreiben")
    offen = await freigaben.anfragen(user, tool, {"text": "erste"})
    verworfen = await freigaben.anfragen(user, tool, {"text": "zweite"})
    await freigaben.verwerfen(verworfen.approval_id, "BadRequest", user)

    stand = await freigaben.stand(user)
    assert f"#{verworfen.approval_id} vor 0 min: Schreiben: zweite – verworfen" in stand
    assert f"#{offen.approval_id} vor 0 min: Schreiben: erste – wartet auf ✅ oder ❌" in stand
    spaeter = await freigaben.stand(user, zeitpunkt=jetzt() + timedelta(minutes=20))
    assert "abgelaufen, nichts wurde ausgeführt" in spaeter
    assert await freigaben.stand(user, zeitpunkt=jetzt() + timedelta(hours=3)) == ""

    client = FakeAnthropic(claude_antwort(text_block("Die Buttons warten noch auf dich.")))
    antwort = await baue_agent(client).beantworte(nachricht("mach das"), user)
    system = system_text(client.aufrufe[0])
    assert "Stand der letzten Freigaben dieses Nutzers" in system
    assert "wartet auf ✅ oder ❌ des Nutzers" in system and "verworfen" in system
    assert antwort.text == "Die Buttons warten noch auf dich."
    assert (await lade_verlauf(session_fabrik, user, CHAT_ID, 5))[-1]["content"] == antwort.text


def test_heutiges_datum_steht_weiter_im_prompt():
    prompt = baue_system_prompt(datetime(2026, 10, 8, 13, 10, tzinfo=ZoneInfo("Europe/Berlin")))
    assert "Heute ist Donnerstag, der 08.10.2026" in prompt
    assert date(2026, 8, 31).strftime("%d.%m.%Y") in prompt
