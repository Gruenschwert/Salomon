"""Vorlagen und Kopien: Request-Bodys und Job-Polling."""

import httpx
import pytest

from app.auth.approvals import Freigaben
from app.tools.asana_client import AsanaClient
from app.tools.asana_ops_vorlagen import JOB_ABSTAND_SEKUNDEN, JOB_TIMEOUT_SEKUNDEN
from app.tools.base import ToolFehler
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext
from tests.test_asana_schreiben import aufgabe

NAME = "asana_aenderungen_ausfuehren"
VORLAGE = {
    "gid": "31",
    "name": "Launch-Vorlage",
    "team": {"gid": "77", "name": "Marketing"},
    "requested_dates": [
        {"gid": "1", "name": "Start Date", "description": "Projektbeginn"},
        {"gid": "2", "name": "Due Date", "description": "Projektende"},
    ],
    "requested_roles": [{"gid": "r1", "name": "Designer"}],
}
PROJEKT_AUS_VORLAGE = {
    "operation": "projekt_aus_vorlage",
    "vorlage_gid": "31",
    "name": "Launch Q4",
    "termine": {"Start Date": "2026-10-12", "2": "2026-12-01"},
    "platzhalter": "$p1",
}


def _job(status: str, **neu) -> dict:
    return {"gid": "j1", "resource_type": "job", "status": status, **neu}


FERTIG = _job(
    "succeeded",
    new_project={"gid": "900", "name": "Launch Q4", "permalink_url": "https://app.asana.com/0/900"},
)


@pytest.fixture
def pausen(monkeypatch) -> list[float]:
    """Ersetzt die Wartezeit zwischen zwei Job-Abfragen und merkt sich jede Pause."""
    pausen: list[float] = []

    async def warte(self, sekunden: float) -> None:
        pausen.append(sekunden)

    monkeypatch.setattr(AsanaClient, "warte", warte)
    return pausen


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/project_templates/31", VORLAGE)
    fake.route("GET", "/workspaces/ws1", {"gid": "ws1", "is_organization": True})
    fake.route("GET", "/teams/78", {"gid": "78", "name": "Vertrieb"})
    fake.route("GET", "/users/501", {"gid": "501", "name": "Max"})
    fake.route("GET", "/projects/100", {"gid": "100", "name": "Launch"})
    fake.route("GET", "/tasks/7", aufgabe())
    fake.route("POST", "/project_templates/31/instantiateProject", _job("not_started"))
    return fake


@pytest.fixture
def werkzeug(kontext, fake):
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    return registry.hole(NAME), Freigaben(akontext, registry)


async def _ausfuehren(werkzeug, user, operationen):
    tool, freigaben = werkzeug
    anfrage = await freigaben.anfragen(user, tool, {"operationen": operationen})
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, user.telegram_id, True)
    return anfrage.vorschau_text.splitlines(), entscheidung.text


async def test_projekt_aus_vorlage_pollt_den_job_bis_succeeded(werkzeug, user, fake, pausen):
    fake.route("GET", "/jobs/j1", _job("in_progress"), _job("in_progress"), FERTIG)
    fake.route("POST", "/projects/900/sections", {"gid": "910"})
    zeilen, ergebnis = await _ausfuehren(
        werkzeug,
        user,
        [
            PROJEKT_AUS_VORLAGE,
            {"operation": "abschnitt_anlegen", "projekt": "$p1", "name": "Extra"},
        ],
    )

    assert zeilen[1] == (
        "1. Anlegen: Projekt „Launch Q4“ aus Vorlage „Launch-Vorlage“ "
        "(Start Date Mo 12.10.2026, Due Date Di 01.12.2026)"
    )
    assert zeilen[2] == "2. Anlegen: Abschnitt „Extra“ in Projekt „Launch Q4“ (neu)"
    assert ergebnis == (
        "✅ Asana-Änderungssatz ausgeführt: 2 angelegt.\nhttps://app.asana.com/0/900"
    )
    # Der Body für eine Organisation ohne eigene Team-Angabe: Asana nimmt das Team der Vorlage.
    assert fake.koerper("POST", "/project_templates/31/instantiateProject") == [
        {
            "name": "Launch Q4",
            "requested_dates": [
                {"gid": "1", "value": "2026-10-12"},
                {"gid": "2", "value": "2026-12-01"},
            ],
        }
    ]
    assert fake.aufrufe().count(("GET", "/jobs/j1")) == 3
    assert pausen == [JOB_ABSTAND_SEKUNDEN] * 3
    # Die GID des neuen Projekts steht danach als Platzhalter zur Verfügung.
    assert fake.aufrufe("POST")[-1] == ("POST", "/projects/900/sections")


async def test_team_und_rollen_in_einer_organisation(werkzeug, user, fake, pausen):
    fake.route("GET", "/jobs/j1", FERTIG)
    zeilen, _ = await _ausfuehren(
        werkzeug,
        user,
        [{**PROJEKT_AUS_VORLAGE, "team_gid": "78", "rollen": {"Designer": "501"}}],
    )
    assert "Team „Vertrieb“" in zeilen[1] and "Designer: Max" in zeilen[1]
    (koerper,) = fake.koerper("POST", "/project_templates/31/instantiateProject")
    assert koerper["team"] == "78"
    assert koerper["requested_roles"] == [{"gid": "r1", "value": "501"}]


async def test_ohne_organisation_gibt_es_kein_team_im_body(werkzeug, user, fake, pausen):
    fake.route("GET", "/workspaces/ws1", {"gid": "ws1", "is_organization": False})
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match="keine Organisation.*„team_gid“ weglassen"):
        await tool.bereite_vor(operationen=[{**PROJEKT_AUS_VORLAGE, "team_gid": "78"}])
    assert fake.aufrufe("POST") == []


@pytest.mark.parametrize(
    ("termine", "meldung"),
    [
        ({"Start Date": "2026-10-12"}, "braucht noch ein Datum für: Due Date \\(2\\)"),
        ({}, "braucht noch ein Datum für: Start Date \\(1\\), Due Date \\(2\\)"),
        ({"1": "2026-10-12", "2": "bald"}, "Datum JJJJ-MM-TT"),
        (
            {"1": "2026-10-12", "2": "2026-12-01", "Ende": "2026-12-02"},
            "kennt die Datumsvariable „Ende“",
        ),
    ],
)
async def test_fehlende_oder_falsche_datumsvariablen_fallen_in_der_vorschau_auf(
    werkzeug, fake, termine, meldung
):
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match=meldung):
        await tool.bereite_vor(operationen=[{**PROJEKT_AUS_VORLAGE, "termine": termine}])
    assert fake.aufrufe("POST") == []


async def test_job_timeout_nach_60_sekunden(werkzeug, user, fake, pausen):
    fake.route("GET", "/jobs/j1", _job("in_progress"))
    _, ergebnis = await _ausfuehren(werkzeug, user, [PROJEKT_AUS_VORLAGE])
    assert "abgebrochen bei Operation 1 von 1" in ergebnis
    assert "nach 60 Sekunden noch nicht fertig (Job j1)" in ergebnis
    assert "Es wurde nichts wiederholt" in ergebnis
    assert sum(pausen) == JOB_TIMEOUT_SEKUNDEN
    # Genau ein Anlege-Aufruf, auch wenn der Job hängt.
    assert fake.aufrufe("POST") == [("POST", "/project_templates/31/instantiateProject")]


async def test_fehlgeschlagener_job_bricht_den_satz_ab(werkzeug, user, fake, pausen):
    fake.route("GET", "/jobs/j1", _job("in_progress"), _job("failed"))
    _, ergebnis = await _ausfuehren(
        werkzeug,
        user,
        [PROJEKT_AUS_VORLAGE, {"operation": "abschnitt_anlegen", "projekt": "$p1", "name": "X"}],
    )
    assert "Asana konnte den Vorgang nicht abschließen (Job fehlgeschlagen)" in ergebnis
    assert "Nicht mehr ausgeführt: Operation 2 (1)." in ergebnis
    assert len(pausen) == 2


async def test_job_der_sofort_fertig_ist_braucht_keine_abfrage(werkzeug, user, fake, pausen):
    fake.route("POST", "/project_templates/31/instantiateProject", FERTIG)
    _, ergebnis = await _ausfuehren(werkzeug, user, [PROJEKT_AUS_VORLAGE])
    assert ergebnis.startswith("✅")
    assert pausen == []
    assert ("GET", "/jobs/j1") not in fake.aufrufe()


async def test_tarif_fehler_bei_vorlagen_wird_uebersetzt(werkzeug, user, fake, pausen):
    fake.route(
        "POST",
        "/project_templates/31/instantiateProject",
        httpx.Response(402, json={"errors": [{"message": "Payment Required"}]}),
    )
    _, ergebnis = await _ausfuehren(werkzeug, user, [PROJEKT_AUS_VORLAGE])
    assert "in eurem Asana-Tarif nicht verfügbar oder dein Token darf das nicht" in ergebnis


async def test_aufgabe_aus_vorlage(werkzeug, user, fake, pausen):
    fake.route(
        "GET",
        "/task_templates/41",
        {"gid": "41", "name": "Bug melden", "project": {"name": "Launch"}},
    )
    fake.route("POST", "/task_templates/41/instantiateTask", _job("in_progress"))
    fake.route("GET", "/jobs/j1", _job("succeeded", new_task={"gid": "950", "name": "Bug 12"}))
    zeilen, ergebnis = await _ausfuehren(
        werkzeug,
        user,
        [{"operation": "aufgabe_aus_vorlage", "vorlage_gid": "41", "name": "Bug 12"}],
    )
    assert zeilen[1] == "1. Anlegen: Aufgabe „Bug 12“ aus Vorlage „Bug melden“ im Projekt „Launch“"
    assert ergebnis.startswith("✅")
    assert fake.koerper("POST", "/task_templates/41/instantiateTask") == [{"name": "Bug 12"}]
    assert "new_task.name" in fake.anfragen[-1].url.params["opt_fields"]


async def test_aufgabe_duplizieren(werkzeug, user, fake, pausen):
    fake.route("POST", "/tasks/7/duplicate", _job("succeeded", new_task={"gid": "951"}))
    zeilen, ergebnis = await _ausfuehren(
        werkzeug,
        user,
        [
            {
                "operation": "aufgabe_duplizieren",
                "gid": "7",
                "name": "Etiketten 2",
                "einschliessen": ["subtasks", "assignee", "dates"],
            }
        ],
    )
    assert zeilen[1] == (
        "1. Duplizieren: Aufgabe „Etiketten“ als „Etiketten 2“ (mit subtasks, assignee, dates)"
    )
    assert ergebnis.startswith("✅")
    assert fake.koerper("POST", "/tasks/7/duplicate") == [
        {"name": "Etiketten 2", "include": "subtasks,assignee,dates"}
    ]
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match="kennt hier task_dates nicht"):
        await tool.bereite_vor(
            operationen=[
                {
                    "operation": "aufgabe_duplizieren",
                    "gid": "7",
                    "name": "x",
                    "einschliessen": ["task_dates"],
                }
            ]
        )


async def test_projekt_duplizieren_mit_datumsverschiebung(werkzeug, user, fake, pausen):
    fake.route("POST", "/projects/100/duplicate", _job("in_progress"))
    fake.route("GET", "/jobs/j1", FERTIG)
    zeilen, ergebnis = await _ausfuehren(
        werkzeug,
        user,
        [
            {
                "operation": "projekt_duplizieren",
                "gid": "100",
                "name": "Launch 2027",
                "einschliessen": ["members", "task_subtasks"],
                "termine_ab": "2027-01-11",
            }
        ],
    )
    assert zeilen[1] == (
        "1. Duplizieren: Projekt „Launch“ als „Launch 2027“ mit allen Aufgaben "
        "(mit members, task_subtasks, task_dates; Termine verschoben, erster Start Mo 11.01.2027)"
    )
    assert ergebnis.startswith("✅")
    assert fake.koerper("POST", "/projects/100/duplicate") == [
        {
            "name": "Launch 2027",
            "schedule_dates": {"should_skip_weekends": True, "start_on": "2027-01-11"},
            "include": "members,task_subtasks,task_dates",
        }
    ]
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match="nicht beide"):
        await tool.bereite_vor(
            operationen=[
                {
                    "operation": "projekt_duplizieren",
                    "gid": "100",
                    "name": "x",
                    "termine_ab": "2027-01-11",
                    "termine_bis": "2027-03-01",
                }
            ]
        )
