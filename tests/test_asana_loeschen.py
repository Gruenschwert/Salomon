from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.auth.approvals import STATUS_BEREITS_ENTSCHIEDEN, Freigaben
from app.db.models import Approval, AsanaOperation, AuditLog, jetzt
from app.tools.asana_schreiben import AsanaAenderungenAusfuehren
from app.tools.base import ToolFehler, aktuelle_freigabe, aktueller_nutzer
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext
from tests.conftest import ADMIN_ID
from tests.test_asana_schreiben import aufgabe

# Jeder Test handelt als Mitarbeiter mit eigenem, verbundenem Asana-Zugang.
pytestmark = pytest.mark.usefixtures("als_nutzer")

NAME = "asana_aenderungen_ausfuehren"
SCHREIBEND = ("POST", "PUT", "DELETE")

LOESCHSATZ = [
    {"operation": "aufgabe_erledigen", "gid": "8"},
    {"operation": "aufgabe_loeschen", "gid": "7"},
    {"operation": "projekt_loeschen", "gid": "100"},
]


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/tasks/7", aufgabe("7", "Etiketten", num_subtasks=2))
    fake.route("GET", "/tasks/8", aufgabe("8", "Budget"))
    fake.route("GET", "/projects/100", {"gid": "100", "name": "Launch"})
    fake.route(
        "GET",
        "/sections/201",
        {"gid": "201", "name": "Offen", "project": {"gid": "100", "name": "Launch"}},
    )
    fake.route("GET", "/sections/201/tasks", [])
    fake.route("PUT", "/tasks/8", {"gid": "8"})
    fake.route("DELETE", "/tasks/7", {})
    fake.route("DELETE", "/projects/100", {})
    fake.route("DELETE", "/sections/201", {})
    return fake


@pytest.fixture
def baue(kontext, fake):
    """Liefert Tool und Freigaben für die gewünschten Asana-Settings."""

    def _baue(**settings_werte) -> tuple[AsanaAenderungenAusfuehren, Freigaben]:
        akontext = asana_kontext(kontext, fake, **settings_werte)
        registry = lade_registry(akontext)
        return registry.hole(NAME), Freigaben(akontext, registry)

    return _baue


def _geschrieben(fake: FakeAsana) -> list[tuple[str, str]]:
    return [a for a in fake.aufrufe() if a[0] in SCHREIBEND]


async def _status(session_fabrik, approval_id: int) -> str:
    async with session_fabrik() as session:
        return (await session.get(Approval, approval_id)).status


async def _anfrage(freigaben, tool, nutzer, operationen=LOESCHSATZ):
    return await freigaben.anfragen(nutzer, tool, {"operationen": operationen})


async def test_vorschau_markiert_loeschungen_mit_namen(baue, admin, fake):
    tool, freigaben = baue()
    anfrage = await _anfrage(
        freigaben, tool, admin, [*LOESCHSATZ, {"operation": "abschnitt_loeschen", "gid": "201"}]
    )
    assert anfrage.vorschau_text.splitlines() == [
        "Asana-Änderungssatz: 1 ändern, 3 🗑 löschen",
        "1. Erledigen: „Budget“",
        "2. 🗑 Löschen: Aufgabe „Etiketten“ samt 2 Unteraufgaben",
        "3. 🗑 Löschen: Projekt „Launch“ samt allen Aufgaben darin",
        "4. 🗑 Löschen: Abschnitt „Offen“ (Projekt „Launch“)",
    ]
    assert _geschrieben(fake) == []


async def test_nicht_leerer_abschnitt_wird_in_der_vorschau_gemeldet(baue, admin, fake):
    fake.route("GET", "/sections/201/tasks", [{"gid": "7"}, {"gid": "8"}])
    tool, freigaben = baue()
    anfrage = await _anfrage(
        freigaben, tool, admin, [{"operation": "abschnitt_loeschen", "gid": "201"}]
    )
    assert "⚠️ der Abschnitt ist nicht leer (2 Aufgaben)" in anfrage.vorschau_text


async def test_erstes_ok_fuehrt_noch_nichts_aus_sondern_fragt_nach(
    baue, admin, fake, session_fabrik
):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)

    erste = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)

    assert erste.rueckfrage
    assert erste.text.splitlines() == [
        "Wirklich löschen? 2 Objekte",
        "2. 🗑 Löschen: Aufgabe „Etiketten“ samt 2 Unteraufgaben",
        "3. 🗑 Löschen: Projekt „Launch“ samt allen Aufgaben darin",
        "Gelöschtes kann in der Regel nicht zuverlässig wiederhergestellt werden.",
        "Auch die eine übrige Operation des Satzes läuft erst nach dieser Bestätigung.",
    ]
    # Ohne zweite Bestätigung läuft keine Operation, auch nicht die harmlose erste.
    assert _geschrieben(fake) == []
    assert await _status(session_fabrik, anfrage.approval_id) == "bestätigung"
    assert await freigaben.anzahl_offen(admin) == 1


async def test_zweite_bestaetigung_fuehrt_den_ganzen_satz_aus(baue, admin, fake, session_fabrik):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)

    zweite = await freigaben.entscheiden(
        anfrage.approval_id, ADMIN_ID, genehmigt=True, bestaetigt=True
    )

    assert zweite.text.startswith("✅ Asana-Änderungssatz ausgeführt: 1 geändert, 2 🗑 gelöscht.")
    assert not zweite.rueckfrage
    assert _geschrieben(fake) == [
        ("PUT", "/tasks/8"),
        ("DELETE", "/tasks/7"),
        ("DELETE", "/projects/100"),
    ]
    assert await _status(session_fabrik, anfrage.approval_id) == "genehmigt"
    async with session_fabrik() as session:
        eintraege = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    detail = next(e for e in eintraege if {"freigabe", "operationen"} <= set(e.parameter))
    assert detail.parameter["operationen"][1]["vorher"]["name"] == "Etiketten"
    assert any("zweite Bestätigung angefragt" in e.ergebnis_kurz for e in eintraege)


async def test_abbrechen_in_der_zweiten_stufe_verwirft_alles(baue, admin, fake, session_fabrik):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)

    abbruch = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=False)

    assert abbruch.text.startswith("❌ Verworfen")
    assert _geschrieben(fake) == []
    assert await _status(session_fabrik, anfrage.approval_id) == "abgelehnt"
    # Danach hilft auch der Lösch-Button nicht mehr.
    spaet = await freigaben.entscheiden(
        anfrage.approval_id, ADMIN_ID, genehmigt=True, bestaetigt=True
    )
    assert spaet.status == STATUS_BEREITS_ENTSCHIEDEN
    assert _geschrieben(fake) == []


async def test_loesch_klick_ohne_erstes_ok_zaehlt_nicht(baue, admin, fake, session_fabrik):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    direkt = await freigaben.entscheiden(
        anfrage.approval_id, ADMIN_ID, genehmigt=True, bestaetigt=True
    )
    assert direkt.status == STATUS_BEREITS_ENTSCHIEDEN
    assert _geschrieben(fake) == []
    assert await _status(session_fabrik, anfrage.approval_id) == "offen"


async def test_zweites_normales_ok_ersetzt_die_bestaetigung_nicht(baue, admin, fake):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    nochmal = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    assert nochmal.status == STATUS_BEREITS_ENTSCHIEDEN
    assert _geschrieben(fake) == []


async def test_doppelte_bestaetigung_loescht_nur_einmal(baue, admin, fake):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    for _ in range(2):
        await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True, bestaetigt=True)
    assert fake.aufrufe("DELETE") == [("DELETE", "/tasks/7"), ("DELETE", "/projects/100")]


async def test_freigabe_laeuft_auch_in_der_zweiten_stufe_ab(baue, admin, fake, session_fabrik):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    spaet = await freigaben.entscheiden(
        anfrage.approval_id,
        ADMIN_ID,
        genehmigt=True,
        bestaetigt=True,
        zeitpunkt=jetzt() + timedelta(minutes=16),
    )
    assert spaet.status == "abgelaufen"
    assert _geschrieben(fake) == []


async def test_ohne_zweite_bestaetigung_loescht_auch_ein_direkter_aufruf_nicht(
    baue, admin, fake, session_fabrik
):
    tool, _ = baue()
    marke_nutzer = aktueller_nutzer.set(admin)
    marke = aktuelle_freigabe.set(1)
    try:
        with pytest.raises(ToolFehler, match="zweiten Bestätigung"):
            await tool.ausfuehren(operationen=LOESCHSATZ)
    finally:
        aktuelle_freigabe.reset(marke)
        aktueller_nutzer.reset(marke_nutzer)
    assert _geschrieben(fake) == []
    async with session_fabrik() as session:
        assert list(await session.scalars(select(AsanaOperation))) == []


async def test_rolle_user_darf_nicht_loeschen(baue, user, admin, fake, session_fabrik):
    tool, freigaben = baue()
    with pytest.raises(ToolFehler, match="darf in Asana nichts löschen"):
        await _anfrage(freigaben, tool, user)
    async with session_fabrik() as session:
        assert list(await session.scalars(select(Approval))) == []
    # Andere Änderungen bleiben für die Rolle möglich.
    await _anfrage(freigaben, tool, user, [{"operation": "aufgabe_erledigen", "gid": "8"}])
    # Mit freigeschalteter Rolle geht auch das Löschen.
    tool, freigaben = baue(asana_delete_roles=frozenset({"admin", "user"}))
    await _anfrage(freigaben, tool, user)
    assert _geschrieben(fake) == []


async def test_abgeschaltetes_loeschen_gilt_auch_fuer_admins(baue, admin, fake, session_fabrik):
    tool, freigaben = baue(asana_delete_enabled=False)
    with pytest.raises(ToolFehler, match="Löschen in Asana ist abgeschaltet"):
        await _anfrage(freigaben, tool, admin)
    async with session_fabrik() as session:
        assert list(await session.scalars(select(Approval))) == []
    assert fake.anfragen == []


async def test_abschalten_nach_der_anfrage_verhindert_die_ausfuehrung(
    baue, kontext, admin, fake, session_fabrik
):
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    # Neustart mit ASANA_DELETE_ENABLED=false zwischen Rückfrage und Bestätigung.
    _, gesperrt = baue(asana_delete_enabled=False)
    ergebnis = await gesperrt.entscheiden(
        anfrage.approval_id, ADMIN_ID, genehmigt=True, bestaetigt=True
    )
    assert "abgeschaltet" in ergebnis.text
    assert _geschrieben(fake) == []


async def test_zu_viele_loeschungen_werden_abgelehnt(baue, admin, fake):
    tool, freigaben = baue(asana_max_deletes_per_changeset=1)
    with pytest.raises(ToolFehler, match="2 Löschoperationen, erlaubt sind höchstens 1"):
        await _anfrage(freigaben, tool, admin)
    await _anfrage(freigaben, tool, admin, LOESCHSATZ[:2])
    assert _geschrieben(fake) == []


async def test_loeschen_nie_ueber_platzhalter_oder_namen(baue, admin):
    tool, freigaben = baue()
    with pytest.raises(ToolFehler, match="nie mit Platzhalter"):
        await _anfrage(
            freigaben,
            tool,
            admin,
            [
                {"operation": "aufgabe_anlegen", "name": "x", "platzhalter": "$a1"},
                {"operation": "aufgabe_loeschen", "gid": "$a1"},
            ],
        )
    with pytest.raises(ToolFehler, match="gültige Asana-GID"):
        await _anfrage(freigaben, tool, admin, [{"operation": "aufgabe_loeschen", "gid": "Budget"}])


async def test_fehler_beim_loeschen_bricht_ab(baue, admin, fake):
    fake.route(
        "DELETE", "/tasks/7", httpx.Response(403, json={"errors": [{"message": "Forbidden"}]})
    )
    tool, freigaben = baue()
    anfrage = await _anfrage(freigaben, tool, admin)
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    ergebnis = await freigaben.entscheiden(
        anfrage.approval_id, ADMIN_ID, genehmigt=True, bestaetigt=True
    )
    assert "abgebrochen bei Operation 2 von 3" in ergebnis.text
    assert "Asana-Zugriff verweigert" in ergebnis.text
    assert fake.aufrufe("DELETE") == [("DELETE", "/tasks/7")]


async def test_telegram_zeigt_die_rueckfrage_mit_eigenen_buttons(
    baue, kontext, admin, fake, session_fabrik, kosten, alarme, monkeypatch
):
    from app.channels.telegram import TelegramKanal
    from tests.test_telegram import FakeQuery, _update

    tool, freigaben = baue()
    kanal = TelegramKanal(
        kontext.settings,
        session_fabrik,
        handler=None,
        freigaben=freigaben,
        kosten=kosten,
        alarme=alarme,
    )
    gesendet = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        gesendet.append((text, reply_markup))

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    anfrage = await _anfrage(freigaben, tool, admin)

    erster = FakeQuery(f"freigabe:{anfrage.approval_id}:ja")
    await kanal._bei_klick(_update(ADMIN_ID, query=erster), None)

    assert erster.buttons_entfernt
    (text, ohne), (frage, buttons) = gesendet
    assert text.startswith("Wirklich löschen? 2 Objekte")
    assert ohne is None and frage.startswith("Löschen bestätigen?")
    assert [(b.text, b.callback_data) for b in buttons.inline_keyboard[0]] == [
        ("🗑 Ja, löschen", f"freigabe:{anfrage.approval_id}:loeschen"),
        ("Abbrechen", f"freigabe:{anfrage.approval_id}:nein"),
    ]
    assert _geschrieben(fake) == []

    zweiter = FakeQuery(f"freigabe:{anfrage.approval_id}:loeschen")
    await kanal._bei_klick(_update(ADMIN_ID, query=zweiter), None)
    assert gesendet[-1][0].startswith("✅ Asana-Änderungssatz ausgeführt")
    assert len(fake.aufrufe("DELETE")) == 2


async def test_agent_schleife_fuehrt_auch_loeschsaetze_nicht_ohne_freigabe_aus(
    baue, kontext, admin, fake, session_fabrik, kosten
):
    from app.agent.loop import WARTET_AUF_FREIGABE_TEXT, Agent
    from app.channels.base import EingehendeNachricht
    from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    client = FakeAnthropic(
        claude_antwort(tool_use_block(NAME, {"operationen": LOESCHSATZ})),
        claude_antwort(text_block("Bitte bestätige das Löschen.")),
    )
    agent = Agent(
        akontext.settings, session_fabrik, client, registry, Freigaben(akontext, registry), kosten
    )
    antwort = await agent.beantworte(
        EingehendeNachricht(chat_id=1, absender_id=ADMIN_ID, absender_name="A", text="lösch"),
        admin,
    )
    (anfrage,) = antwort.freigaben
    assert "2 🗑 löschen" in anfrage.vorschau_text
    assert _geschrieben(fake) == []
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["content"] == WARTET_AUF_FREIGABE_TEXT


async def test_abgelehnter_loeschvorschlag_geht_als_fehler_an_claude(
    baue, kontext, user, fake, session_fabrik, kosten
):
    from app.agent.loop import Agent
    from app.channels.base import EingehendeNachricht
    from tests.conftest import ERLAUBT_ID
    from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    client = FakeAnthropic(
        claude_antwort(tool_use_block(NAME, {"operationen": LOESCHSATZ})),
        claude_antwort(text_block("Das darfst du nicht.")),
    )
    agent = Agent(
        akontext.settings, session_fabrik, client, registry, Freigaben(akontext, registry), kosten
    )
    antwort = await agent.beantworte(
        EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="U", text="lösch"),
        user,
    )
    assert antwort.freigaben == ()
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["is_error"] is True
    assert "darf in Asana nichts löschen" in ergebnis["content"]
    assert fake.anfragen == []
