"""Wiederkehrende Aufgaben: Die Struktur wird nie gebaut, nur 1:1 übernommen."""

import copy

import httpx
import pytest

from app.auth.approvals import Freigaben
from app.tools.base import ToolFehler
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext

# Jeder Test handelt als Mitarbeiter mit eigenem, verbundenem Asana-Zugang.
pytestmark = pytest.mark.usefixtures("als_nutzer")

NAME = "asana_aenderungen_ausfuehren"
# So könnte Asana eine von Hand eingerichtete Wiederholung liefern. Der Aufbau ist bewusst
# ungewöhnlich: Das Tool darf nichts davon kennen, verändern oder weglassen.
WOECHENTLICH = {
    "type": "weekly",
    "data": {"days_of_week": [1, 4], "frequency": 2, "unbekanntes_feld": {"tief": [None, 0.5]}},
    "next_due": None,
}


def _aufgabe(gid: str, name: str, wiederholung) -> dict:
    return {"gid": gid, "name": name, "recurrence": wiederholung}


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/tasks/7", _aufgabe("7", "Inventur", None))
    fake.route("GET", "/tasks/8", _aufgabe("8", "Wochenbericht", copy.deepcopy(WOECHENTLICH)))
    fake.route("GET", "/tasks/9", _aufgabe("9", "Einmalig", {"type": "never", "data": None}))
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    fake.route("PUT", "/tasks/8", {"gid": "8"})
    return fake


@pytest.fixture
def werkzeug(kontext, fake):
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    return registry.hole(NAME), Freigaben(akontext, registry)


async def _freigeben(werkzeug, user, operationen):
    tool, freigaben = werkzeug
    anfrage = await freigaben.anfragen(user, tool, {"operationen": operationen})
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, user.telegram_id, True)
    return anfrage.vorschau_text.splitlines(), entscheidung.text


async def test_struktur_wird_unveraendert_aus_der_lese_antwort_uebernommen(werkzeug, user, fake):
    zeilen, ergebnis = await _freigeben(
        werkzeug,
        user,
        [{"operation": "wiederholung_setzen", "gid": "7", "vorlage_aufgabe_gid": "8"}],
    )
    assert zeilen[1].startswith(
        "1. Wiederholung (experimentell): „Inventur“ bekommt die Wiederholung von "
        "„Wochenbericht“: {"
    )
    assert ergebnis.startswith("✅")
    assert fake.koerper("PUT", "/tasks/7") == [{"recurrence": WOECHENTLICH}]
    lesen = [a for a in fake.anfragen if a.method == "GET" and a.url.path.endswith("/tasks/8")]
    assert "recurrence" in lesen[0].url.params["opt_fields"]


async def test_eine_frei_beschriebene_wiederholung_gibt_es_nicht(werkzeug, user, fake):
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match="kennt die Felder wiederholung nicht"):
        await tool.bereite_vor(
            operationen=[
                {"operation": "wiederholung_setzen", "gid": "7", "wiederholung": {"type": "daily"}}
            ]
        )
    with pytest.raises(ToolFehler, match="Das Feld „vorlage_aufgabe_gid“ fehlt"):
        await tool.bereite_vor(operationen=[{"operation": "wiederholung_setzen", "gid": "7"}])
    assert fake.aufrufe("PUT") == []


async def test_vorlage_ohne_wiederholung_wird_abgelehnt(werkzeug, user, fake):
    tool, _ = werkzeug
    fake.route("GET", "/tasks/10", _aufgabe("10", "Leer", None))
    with pytest.raises(ToolFehler, match="keine Wiederholung eingerichtet"):
        await tool.bereite_vor(
            operationen=[
                {"operation": "wiederholung_setzen", "gid": "7", "vorlage_aufgabe_gid": "10"}
            ]
        )


async def test_fehlschlag_nennt_den_grund_und_den_weg_in_asana(werkzeug, user, fake):
    fake.route(
        "PUT",
        "/tasks/7",
        httpx.Response(400, json={"errors": [{"message": "recurrence: Invalid"}]}),
    )
    _, ergebnis = await _freigeben(
        werkzeug,
        user,
        [{"operation": "wiederholung_setzen", "gid": "7", "vorlage_aufgabe_gid": "8"}],
    )
    assert "abgebrochen bei Operation 1 von 1" in ergebnis
    assert "Asana lehnt die Anfrage ab: recurrence: Invalid" in ergebnis
    assert "experimentell" in ergebnis
    assert "auf das Datum klicken, dann „Wiederholen“" in ergebnis
    assert fake.aufrufe("PUT") == [("PUT", "/tasks/7")]


async def test_kann_asana_die_wiederholung_nicht_lesen_wird_das_gesagt(werkzeug, user, fake):
    fake.route("GET", "/tasks/8", httpx.Response(400, json={"errors": [{"message": "Unknown"}]}))
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match="liefert die Wiederholung nicht.*experimentell"):
        await tool.bereite_vor(
            operationen=[
                {"operation": "wiederholung_setzen", "gid": "7", "vorlage_aufgabe_gid": "8"}
            ]
        )


async def test_entfernen_uebernimmt_den_wert_einer_aufgabe_ohne_wiederholung(werkzeug, user, fake):
    zeilen, ergebnis = await _freigeben(
        werkzeug,
        user,
        [
            {"operation": "wiederholung_entfernen", "gid": "8", "vorlage_aufgabe_gid": "9"},
            {"operation": "wiederholung_entfernen", "gid": "7"},
        ],
    )
    assert zeilen[1].startswith(
        "1. Wiederholung entfernen (experimentell): „Wochenbericht“, bisher {"
    )
    assert zeilen[2] == "2. Wiederholung entfernen (experimentell): „Inventur“"
    assert ergebnis.startswith("✅")
    assert fake.koerper("PUT", "/tasks/8") == [{"recurrence": {"type": "never", "data": None}}]
    assert fake.koerper("PUT", "/tasks/7") == [{"recurrence": None}]
