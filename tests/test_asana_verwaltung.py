"""Zeiterfassung, Statusmeldungen, Projekt-Briefing, Portfolios und Ziele."""

import httpx
import pytest

from app.auth.approvals import Freigaben
from app.tools.base import ToolFehler
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext
from tests.test_asana_schreiben import aufgabe

# Jeder Test handelt als Mitarbeiter mit eigenem, verbundenem Asana-Zugang.
pytestmark = pytest.mark.usefixtures("als_nutzer")

NAME = "asana_aenderungen_ausfuehren"
LOESCHUNGEN = [
    {"operation": "zeit_loeschen", "zeiteintrag_gid": "71"},
    {"operation": "statusmeldung_loeschen", "statusmeldung_gid": "81"},
    {"operation": "projektbriefing_loeschen", "projekt": "100"},
    {"operation": "portfolio_loeschen", "portfolio_gid": "60"},
    {"operation": "ziel_loeschen", "ziel_gid": "90"},
]


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/tasks/7", aufgabe())
    fake.route(
        "GET",
        "/projects/100",
        lambda anfrage: httpx.Response(
            200,
            json={
                "data": {
                    "gid": "100",
                    "name": "Launch",
                    "permalink_url": "https://app.asana.com/0/100",
                    "project_brief": {"gid": "95", "title": "Briefing"},
                }
            },
        ),
    )
    fake.route("GET", "/projects/300", {"gid": "300", "name": "Messe", "project_brief": None})
    fake.route(
        "GET",
        "/time_tracking_entries/71",
        {
            "gid": "71",
            "duration_minutes": 90,
            "entered_on": "2026-10-05",
            "task": {"name": "Etiketten"},
        },
    )
    fake.route(
        "GET",
        "/status_updates/81",
        {"gid": "81", "title": "KW 40", "status_type": "on_track", "parent": {"name": "Launch"}},
    )
    fake.route(
        "GET",
        "/portfolios/60",
        {"gid": "60", "name": "2026", "color": "light-blue", "public": False},
    )
    fake.route(
        "GET",
        "/goals/90",
        {"gid": "90", "name": "Umsatz", "due_on": "2026-12-31", "status": "green", "owner": None},
    )
    fake.route("GET", "/goals/91", {"gid": "91", "name": "B2B"})
    fake.route("GET", "/users/501", {"gid": "501", "name": "Max"})
    return fake


@pytest.fixture
def werkzeug(kontext, fake):
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    return registry.hole(NAME), Freigaben(akontext, registry)


async def _vorschau(werkzeug, nutzer, operationen) -> list[str]:
    tool, freigaben = werkzeug
    anfrage = await freigaben.anfragen(nutzer, tool, {"operationen": operationen})
    return anfrage.vorschau_text.splitlines()


async def _ausfuehren(werkzeug, nutzer, operationen, bestaetigt: bool = False) -> list[str]:
    tool, freigaben = werkzeug
    anfrage = await freigaben.anfragen(nutzer, tool, {"operationen": operationen})
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, nutzer.telegram_id, True)
    if bestaetigt:
        assert entscheidung.rueckfrage, entscheidung.text
        entscheidung = await freigaben.entscheiden(
            anfrage.approval_id, nutzer.telegram_id, True, bestaetigt=True
        )
    assert entscheidung.text.startswith("✅"), entscheidung.text
    return anfrage.vorschau_text.splitlines()


async def test_zeit_erfassen_und_aendern(werkzeug, user, fake):
    fake.route("POST", "/tasks/7/time_tracking_entries", {"gid": "72"})
    fake.route("PUT", "/time_tracking_entries/71", {"gid": "71"})
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            {
                "operation": "zeit_erfassen",
                "aufgabe_gid": "7",
                "minuten": 90,
                "datum": "2026-10-06",
            },
            {"operation": "zeit_erfassen", "aufgabe_gid": "7", "minuten": 20},
            {"operation": "zeit_aendern", "zeiteintrag_gid": "71", "minuten": 120},
        ],
    )
    assert zeilen[1] == "1. Zeit erfassen: 1 h 30 min für „Etiketten“ am Di 06.10.2026"
    assert zeilen[2] == "2. Zeit erfassen: 20 min für „Etiketten“ (heute)"
    assert zeilen[3] == "3. Zeiteintrag ändern („Etiketten“): 1 h 30 min → 2 h 0 min"
    assert fake.koerper("POST", "/tasks/7/time_tracking_entries") == [
        {"duration_minutes": 90, "entered_on": "2026-10-06"},
        {"duration_minutes": 20},
    ]
    assert fake.koerper("PUT", "/time_tracking_entries/71") == [{"duration_minutes": 120}]
    for minuten in (0, -5, 1.5):
        with pytest.raises(ToolFehler, match="ganze Zahl größer als 0"):
            await _vorschau(
                werkzeug,
                user,
                [{"operation": "zeit_erfassen", "aufgabe_gid": "7", "minuten": minuten}],
            )


async def test_statusmeldung_fuer_projekt_portfolio_und_ziel(werkzeug, user, fake):
    fake.route("POST", "/status_updates", {"gid": "82"})
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            {
                "operation": "statusmeldung_erstellen",
                "projekt": "100",
                "stand": "at_risk",
                "titel": "KW 41",
                "text": "Lieferant verspätet",
            },
            {
                "operation": "statusmeldung_erstellen",
                "ziel_gid": "90",
                "stand": "achieved",
                "titel": "Q4",
            },
        ],
    )
    assert zeilen[1] == (
        "1. Statusmeldung für Projekt „Launch“: at_risk, Titel „KW 41“, Text: „Lieferant verspätet“"
    )
    assert zeilen[2] == "2. Statusmeldung für Ziel „Umsatz“: achieved, Titel „Q4“"
    assert fake.koerper("POST", "/status_updates") == [
        {
            "parent": "100",
            "status_type": "at_risk",
            "title": "KW 41",
            "text": "Lieferant verspätet",
        },
        {"parent": "90", "status_type": "achieved", "title": "Q4"},
    ]
    with pytest.raises(ToolFehler, match="Für ein Projekt ist der Stand „achieved“ nicht möglich"):
        await _vorschau(
            werkzeug,
            user,
            [
                {
                    "operation": "statusmeldung_erstellen",
                    "projekt": "100",
                    "stand": "achieved",
                    "titel": "x",
                }
            ],
        )
    with pytest.raises(ToolFehler, match="genau eines angeben"):
        await _vorschau(
            werkzeug,
            user,
            [{"operation": "statusmeldung_erstellen", "stand": "on_track", "titel": "x"}],
        )


async def test_projektbriefing_anlegen_und_ersetzen_als_html(werkzeug, user, fake):
    fake.route("POST", "/projects/300/project_briefs", {"gid": "96"})
    fake.route("PUT", "/project_briefs/95", {"gid": "95"})
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            {
                "operation": "projektbriefing_setzen",
                "projekt": "300",
                "text": "Ziel: Stand <A12> & Team",
            },
            {
                "operation": "projektbriefing_setzen",
                "projekt": "100",
                "text": "Neu",
                "titel": "Überblick",
            },
        ],
    )
    assert zeilen[1].startswith("1. Projekt-Briefing anlegen: „Messe“")
    assert zeilen[2].startswith("2. Projekt-Briefing ersetzen: „Launch“")
    assert fake.koerper("POST", "/projects/300/project_briefs") == [
        {"html_text": "<body>Ziel: Stand &lt;A12&gt; &amp; Team</body>"}
    ]
    assert fake.koerper("PUT", "/project_briefs/95") == [
        {"html_text": "<body>Neu</body>", "title": "Überblick"}
    ]


async def test_portfolio_anlegen_fuellen_und_aendern(werkzeug, user, fake):
    fake.route(
        "POST", "/portfolios", {"gid": "61", "permalink_url": "https://app.asana.com/0/p/61"}
    )
    fake.route("GET", "/portfolios/61", {"gid": "61", "name": "2027"})
    fake.route("POST", "/portfolios/61/addItem", {})
    fake.route("POST", "/portfolios/60/removeItem", {})
    fake.route("PUT", "/portfolios/60", {"gid": "60"})
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            {
                "operation": "portfolio_anlegen",
                "name": "2027",
                "platzhalter": "$o1",
                "oeffentlich": True,
            },
            {
                "operation": "portfolio_projekt_hinzufuegen",
                "portfolio_gid": "$o1",
                "projekt": "100",
            },
            {"operation": "portfolio_projekt_entfernen", "portfolio_gid": "60", "projekt": "100"},
            {
                "operation": "portfolio_aendern",
                "portfolio_gid": "60",
                "name": "Archiv 2026",
                "farbe": "dark-red",
                "oeffentlich": True,
            },
        ],
    )
    assert zeilen[1] == "1. Anlegen: Portfolio „2027“ (für alle im Workspace sichtbar)"
    assert zeilen[2] == "2. Projekt „Launch“ ins Portfolio „2027“ (neu) aufnehmen"
    assert zeilen[3] == "3. Projekt „Launch“ aus dem Portfolio „2026“ herausnehmen"
    assert zeilen[4] == (
        "4. Ändern: Portfolio „2026“: Name → „Archiv 2026“; Farbe light-blue → dark-red; "
        "wird für alle sichtbar"
    )
    assert fake.koerper("POST", "/portfolios") == [
        {"name": "2027", "public": True, "workspace": "ws1"}
    ]
    assert fake.koerper("POST", "/portfolios/61/addItem") == [{"item": "100"}]
    assert fake.koerper("POST", "/portfolios/60/removeItem") == [{"item": "100"}]
    assert fake.koerper("PUT", "/portfolios/60") == [
        {"name": "Archiv 2026", "color": "dark-red", "public": True}
    ]


async def test_ziel_anlegen_aendern_und_verknuepfen(werkzeug, user, fake):
    fake.route("POST", "/goals", {"gid": "92"})
    fake.route("GET", "/goals/92", {"gid": "92", "name": "Kundenzahl"})
    fake.route("PUT", "/goals/90", {"gid": "90"})
    for gid in ("90", "92"):
        fake.route("POST", f"/goals/{gid}/addSupportingRelationship", {"gid": "r"})
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            {
                "operation": "ziel_anlegen",
                "name": "Kundenzahl",
                "faellig": "2026-12-31",
                "startdatum": "2026-10-01",
                "besitzer_gid": "501",
                "platzhalter": "$z1",
            },
            {"operation": "ziel_aendern", "ziel_gid": "90", "startdatum": "2026-10-01"},
            {
                "operation": "ziel_aufgabe_oder_projekt_verknuepfen",
                "ziel_gid": "$z1",
                "projekt": "100",
            },
            {
                "operation": "ziel_aufgabe_oder_projekt_verknuepfen",
                "ziel_gid": "90",
                "aufgabe_gid": "7",
            },
            {
                "operation": "ziel_aufgabe_oder_projekt_verknuepfen",
                "ziel_gid": "90",
                "teilziel_gid": "$z1",
            },
        ],
    )
    assert zeilen[1] == (
        "1. Anlegen: Ziel „Kundenzahl“ (Start Do 01.10.2026, fällig Do 31.12.2026, Besitzer Max)"
    )
    assert zeilen[2] == "2. Ändern: Ziel „Umsatz“: Start (leer) → Do 01.10.2026"
    assert zeilen[3] == "3. Verknüpfen: Projekt „Launch“ unterstützt das Ziel „Kundenzahl“ (neu)"
    assert zeilen[5] == "5. Verknüpfen: Teilziel „Kundenzahl“ (neu) unterstützt das Ziel „Umsatz“"
    assert fake.koerper("POST", "/goals") == [
        {
            "name": "Kundenzahl",
            "due_on": "2026-12-31",
            "start_on": "2026-10-01",
            "owner": "501",
            "workspace": "ws1",
        }
    ]
    # Der Start geht nur zusammen mit der Fälligkeit; sie kommt aus dem aktuellen Stand.
    assert fake.koerper("PUT", "/goals/90") == [{"start_on": "2026-10-01", "due_on": "2026-12-31"}]
    assert fake.koerper("POST", "/goals/92/addSupportingRelationship") == [
        {"supporting_resource": "100"}
    ]
    assert fake.koerper("POST", "/goals/90/addSupportingRelationship") == [
        {"supporting_resource": "7"},
        {"supporting_resource": "92"},
    ]
    with pytest.raises(ToolFehler, match="braucht bei einem Ziel auch eine Fälligkeit"):
        await _vorschau(
            werkzeug, user, [{"operation": "ziel_anlegen", "name": "x", "startdatum": "2026-10-01"}]
        )
    with pytest.raises(ToolFehler, match="genau eines angeben"):
        await _vorschau(
            werkzeug,
            user,
            [{"operation": "ziel_aufgabe_oder_projekt_verknuepfen", "ziel_gid": "90"}],
        )


async def test_alle_loeschungen_zaehlen_brauchen_die_rolle_und_die_zweite_bestaetigung(
    kontext, werkzeug, user, admin, fake
):
    pfade = [
        "/time_tracking_entries/71",
        "/status_updates/81",
        "/project_briefs/95",
        "/portfolios/60",
        "/goals/90",
    ]
    for pfad in pfade:
        fake.route("DELETE", pfad, {})
    for op in LOESCHUNGEN:
        with pytest.raises(ToolFehler, match="darf in Asana nichts löschen"):
            await _vorschau(werkzeug, user, [op])

    zeilen = await _ausfuehren(werkzeug, admin, LOESCHUNGEN, bestaetigt=True)
    assert zeilen == [
        "Asana-Änderungssatz: 5 🗑 löschen",
        "1. 🗑 Löschen: Zeiteintrag 1 h 30 min vom Mo 05.10.2026 an „Etiketten“",
        "2. 🗑 Löschen: Statusmeldung „KW 40“ von „Launch“",
        "3. 🗑 Löschen: Briefing des Projekts „Launch“",
        "4. 🗑 Löschen: Portfolio „2026“ (die Projekte darin bleiben erhalten)",
        "5. 🗑 Löschen: Ziel „Umsatz“",
    ]
    assert fake.aufrufe("DELETE") == [("DELETE", pfad) for pfad in pfade]

    akontext = asana_kontext(kontext, fake, asana_max_deletes_per_changeset=4)
    registry = lade_registry(akontext)
    with pytest.raises(ToolFehler, match="5 Löschoperationen, erlaubt sind höchstens 4"):
        await _vorschau((registry.hole(NAME), Freigaben(akontext, registry)), admin, LOESCHUNGEN)


async def test_briefing_loeschen_ohne_briefing_wird_gemeldet(werkzeug, admin, fake):
    with pytest.raises(ToolFehler, match="hat kein Briefing"):
        await _vorschau(
            werkzeug, admin, [{"operation": "projektbriefing_loeschen", "projekt": "300"}]
        )


@pytest.mark.parametrize("status", [402, 403])
async def test_tarif_fehler_bricht_den_satz_mit_klarem_satz_ab(werkzeug, user, fake, status):
    fake.route(
        "POST",
        "/tasks/7/time_tracking_entries",
        httpx.Response(status, json={"errors": [{"message": "Not available in your plan"}]}),
    )
    tool, freigaben = werkzeug
    anfrage = await freigaben.anfragen(
        user,
        tool,
        {
            "operationen": [
                {"operation": "zeit_erfassen", "aufgabe_gid": "7", "minuten": 30},
                {"operation": "aufgabe_gefaellt_mir", "gid": "7"},
            ]
        },
    )
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, user.telegram_id, True)
    assert "abgebrochen bei Operation 1 von 2" in entscheidung.text
    assert (
        "in eurem Asana-Tarif nicht verfügbar oder dein Token darf das nicht" in entscheidung.text
    )
    assert "Nicht mehr ausgeführt: Operation 2 (1)." in entscheidung.text
    assert fake.aufrufe("PUT") == []
