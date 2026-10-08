"""Übergreifende Tests der Asana-Erweiterung: Vollständigkeit, Vorschau ohne Markdown,
Rate Limit bei neuen Operationen."""

import httpx
import pytest

from app.auth.approvals import Freigaben
from app.tools.asana_operationen import KATEGORIE_LOESCHEN, OP_TYPEN, ZUSATZ_BESCHREIBUNG
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext
from tests.test_asana_schreiben import aufgabe

NAME = "asana_aenderungen_ausfuehren"
NEUE_TOOLS = {
    "asana_felder_anzeigen",
    "asana_vorlagen_anzeigen",
    "asana_teams_anzeigen",
    "asana_portfolios_anzeigen",
    "asana_ziele_anzeigen",
    "asana_anhaenge_anzeigen",
    "asana_zeiteintraege_anzeigen",
    "asana_statusmeldungen_anzeigen",
    "asana_anhang_ansehen",
    "asana_api_aufruf",
}
NEUE_OPERATIONEN = {
    "anhang_hinzufuegen",
    "anhang_loeschen",
    "feld_anlegen",
    "feld_aendern",
    "feld_loeschen",
    "feldoption_hinzufuegen",
    "feld_zu_projekt_hinzufuegen",
    "feld_aus_projekt_entfernen",
    "projekt_aus_vorlage",
    "aufgabe_aus_vorlage",
    "aufgabe_duplizieren",
    "projekt_duplizieren",
    "projekt_mitglied_hinzufuegen",
    "projekt_mitglied_entfernen",
    "projekt_follower_hinzufuegen",
    "projekt_follower_entfernen",
    "team_anlegen",
    "team_aendern",
    "team_mitglied_hinzufuegen",
    "team_mitglied_entfernen",
    "aufgabe_zu_projekt_hinzufuegen",
    "aufgabe_aus_projekt_entfernen",
    "unteraufgabe_umhaengen",
    "unteraufgabe_zu_aufgabe_machen",
    "reihenfolge_aendern",
    "kommentar_bearbeiten",
    "kommentar_loeschen",
    "aufgabe_genehmigung",
    "aufgabe_gefaellt_mir",
    "follower_hinzufuegen",
    "follower_entfernen",
    "wiederholung_setzen",
    "wiederholung_entfernen",
    "zeit_erfassen",
    "zeit_aendern",
    "zeit_loeschen",
    "statusmeldung_erstellen",
    "statusmeldung_loeschen",
    "projektbriefing_setzen",
    "projektbriefing_loeschen",
    "portfolio_anlegen",
    "portfolio_aendern",
    "portfolio_loeschen",
    "portfolio_projekt_hinzufuegen",
    "portfolio_projekt_entfernen",
    "ziel_anlegen",
    "ziel_aendern",
    "ziel_loeschen",
    "ziel_aufgabe_oder_projekt_verknuepfen",
    "api_aufruf",
}
# Was laut Vorgabe als Löschung zählt (zweite Bestätigung, Lösch-Rolle, Lösch-Limit).
LOESCHUNGEN = {
    "anhang_loeschen",
    "feld_loeschen",
    "portfolio_loeschen",
    "ziel_loeschen",
    "statusmeldung_loeschen",
    "kommentar_loeschen",
    "projektbriefing_loeschen",
    "zeit_loeschen",
    "team_mitglied_entfernen",
}
TEAM_OPERATIONEN = {
    "team_anlegen",
    "team_aendern",
    "team_mitglied_hinzufuegen",
    "team_mitglied_entfernen",
}


def test_alle_neuen_tools_und_operationen_sind_da(kontext):
    registry = lade_registry(kontext)
    assert NEUE_TOOLS <= {tool.name for tool in registry.alle()}
    assert NEUE_OPERATIONEN <= set(OP_TYPEN)
    schema = registry.hole(NAME).parameter_schema["properties"]["operationen"]["items"]
    assert set(schema["properties"]["operation"]["enum"]) == set(OP_TYPEN)
    # Jedes Feld, das eine Operation kennt, ist im Schema für Claude beschrieben.
    felder = {feld for typ in OP_TYPEN.values() for feld in typ.pflicht | typ.optional}
    assert felder <= set(schema["properties"])


def test_loeschungen_und_rollen_sind_richtig_zugeordnet():
    for art in LOESCHUNGEN:
        assert OP_TYPEN[art].kategorie == KATEGORIE_LOESCHEN, art
    for art in NEUE_OPERATIONEN - LOESCHUNGEN:
        assert OP_TYPEN[art].kategorie != KATEGORIE_LOESCHEN, art
    for art in TEAM_OPERATIONEN:
        assert OP_TYPEN[art].rollen == "asana_team_verwaltung_roles", art
    assert OP_TYPEN["api_aufruf"].rollen == "asana_api_aufruf_roles"
    andere = NEUE_OPERATIONEN - TEAM_OPERATIONEN - {"api_aufruf"}
    assert all(OP_TYPEN[art].rollen is None for art in andere)


def test_beschreibungen_fuer_claude_enthalten_kein_markdown(kontext):
    for tool in lade_registry(kontext).alle():
        assert "**" not in tool.beschreibung, tool.name
        assert "`" not in tool.beschreibung, tool.name
    assert all("**" not in zeile for zeile in ZUSATZ_BESCHREIBUNG)


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/tasks/7", aufgabe())
    fake.route(
        "GET", "/tasks/8", {**aufgabe("8", "Wochenbericht"), "recurrence": {"type": "weekly"}}
    )
    fake.route("GET", "/projects/100", {"gid": "100", "name": "Launch", "project_brief": None})
    fake.route("GET", "/users", [{"gid": "501", "name": "Max Muster", "email": "max@x.de"}])
    fake.route("GET", "/attachments/55", {"gid": "55", "name": "alt.pdf", "parent": {"name": "X"}})
    for methode, pfad in (
        ("POST", "/attachments"),
        ("POST", "/custom_fields"),
        ("POST", "/projects/100/duplicate"),
        ("POST", "/projects/100/addMembers"),
        ("POST", "/tasks/7/time_tracking_entries"),
        ("POST", "/status_updates"),
        ("POST", "/projects/100/project_briefs"),
        ("PUT", "/tasks/7"),
        ("PUT", "/tags/41"),
        ("POST", "/tasks"),
        ("DELETE", "/attachments/55"),
    ):
        fake.route(
            methode,
            pfad,
            {"gid": "900", "status": "succeeded", "new_project": {"gid": "901", "name": "Kopie"}},
        )
    return fake


async def test_vorschau_rueckfrage_und_ergebnis_enthalten_kein_markdown(kontext, fake, admin):
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    freigaben = Freigaben(akontext, registry)
    operationen = [
        {"operation": "aufgabe_anlegen", "name": "Neu", "projekt": "100", "faellig": "2026-10-15"},
        {
            "operation": "anhang_hinzufuegen",
            "aufgabe_gid": "7",
            "url": "https://x.de/a",
            "name": "Link",
        },
        {"operation": "feld_anlegen", "name": "Status", "typ": "enum", "optionen": ["A", "B"]},
        {"operation": "projekt_duplizieren", "gid": "100", "name": "Kopie"},
        {"operation": "projekt_mitglied_hinzufuegen", "projekt": "100", "nutzer": ["Max"]},
        {"operation": "zeit_erfassen", "aufgabe_gid": "7", "minuten": 45},
        {
            "operation": "statusmeldung_erstellen",
            "projekt": "100",
            "stand": "on_track",
            "titel": "KW 41",
        },
        {"operation": "projektbriefing_setzen", "projekt": "100", "text": "Ziel des Projekts"},
        {"operation": "wiederholung_setzen", "gid": "7", "vorlage_aufgabe_gid": "8"},
        {"operation": "aufgabe_genehmigung", "gid": "7", "status": "genehmigt"},
        {
            "operation": "api_aufruf",
            "methode": "PUT",
            "pfad": "/tags/41",
            "body": {"name": "eilig"},
            "begruendung": "Tag umbenennen",
        },
        {"operation": "anhang_loeschen", "anhang_gid": "55"},
    ]
    anfrage = await freigaben.anfragen(admin, registry.hole(NAME), {"operationen": operationen})
    rueckfrage = await freigaben.entscheiden(anfrage.approval_id, admin.telegram_id, True)
    ergebnis = await freigaben.entscheiden(
        anfrage.approval_id, admin.telegram_id, True, bestaetigt=True
    )

    assert len(anfrage.vorschau_text.splitlines()) == len(operationen) + 1
    assert rueckfrage.rueckfrage and ergebnis.text.startswith("✅"), ergebnis.text
    for text in (anfrage.vorschau_text, rueckfrage.text, ergebnis.text):
        assert "**" not in text
        assert "__" not in text
        assert "`" not in text
        assert not any(zeile.lstrip().startswith("#") for zeile in text.splitlines())


async def test_rate_limit_gilt_auch_fuer_neue_operationen(kontext, fake, user, monkeypatch):
    from app.tools.asana_client import AsanaClient

    pausen: list[float] = []

    async def schlaf(sekunden: float) -> None:
        pausen.append(sekunden)

    original = AsanaClient.__init__

    def init(self, kontext, schlaf_funktion=schlaf):
        original(self, kontext, schlaf=schlaf_funktion)

    monkeypatch.setattr(AsanaClient, "__init__", init)
    limit = httpx.Response(429, headers={"Retry-After": "3"}, json={"errors": []})
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    freigaben = Freigaben(akontext, registry)

    # Erst abgelehnt, dann erfolgreich: Der Upload wird nach der Wartezeit wiederholt.
    fake.route("POST", "/attachments", limit, {"gid": "900"})
    op = {
        "operation": "anhang_hinzufuegen",
        "aufgabe_gid": "7",
        "url": "https://x.de/a",
        "name": "L",
    }
    anfrage = await freigaben.anfragen(user, registry.hole(NAME), {"operationen": [op]})
    ergebnis = await freigaben.entscheiden(anfrage.approval_id, user.telegram_id, True)
    assert ergebnis.text.startswith("✅")
    assert pausen == [3.0]
    assert fake.aufrufe("POST") == [("POST", "/attachments")] * 2

    # Dauerhaft 429: nach drei Wiederholungen sauberer Fehler, der Satz bricht ab.
    pausen.clear()
    fake.route("POST", "/tasks/7/time_tracking_entries", limit)
    anfrage = await freigaben.anfragen(
        user,
        registry.hole(NAME),
        {"operationen": [{"operation": "zeit_erfassen", "aufgabe_gid": "7", "minuten": 5}]},
    )
    ergebnis = await freigaben.entscheiden(anfrage.approval_id, user.telegram_id, True)
    assert "Das Asana-Abfragelimit ist erreicht" in ergebnis.text
    assert pausen == [3.0, 3.0, 3.0]
    assert fake.aufrufe("POST").count(("POST", "/tasks/7/time_tracking_entries")) == 4
