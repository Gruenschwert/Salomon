import base64

import httpx
import pytest
from sqlalchemy import select

from app.agent.loop import Agent
from app.auth.approvals import Freigaben
from app.channels.base import EingehendeNachricht
from app.config import Settings
from app.db.models import AuditLog
from app.tools.asana_lesen import AsanaAufgabeDetails, AsanaAufgabenSuchen
from app.tools.asana_lesen_mehr import (
    AsanaAnhaengeAnzeigen,
    AsanaAnhangAnsehen,
    AsanaFelderAnzeigen,
    AsanaPortfoliosAnzeigen,
    AsanaStatusmeldungenAnzeigen,
    AsanaTeamsAnzeigen,
    AsanaVorlagenAnzeigen,
    AsanaZeiteintraegeAnzeigen,
    AsanaZieleAnzeigen,
)
from app.tools.base import ToolFehler
from app.tools.registry import fuehre_tool_aus, lade_registry
from tests.asana_fake import ASANA_TOKEN, FakeAsana, asana_kontext
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

# Jeder Test handelt als Mitarbeiter mit eigenem, verbundenem Asana-Zugang.
pytestmark = pytest.mark.usefixtures("als_nutzer")

NEUE_LESE_TOOLS = [
    "asana_anhaenge_anzeigen",
    "asana_anhang_ansehen",
    "asana_felder_anzeigen",
    "asana_portfolios_anzeigen",
    "asana_statusmeldungen_anzeigen",
    "asana_teams_anzeigen",
    "asana_vorlagen_anzeigen",
    "asana_zeiteintraege_anzeigen",
    "asana_ziele_anzeigen",
]
PNG = b"\x89PNG\r\n\x1a\n" + b"bildinhalt" * 30
PDF = b"%PDF-1.7\n" + b"pdfinhalt" * 30
DOWNLOAD = "https://asana-user-private.example.com/datei/abc?signatur=xyz"

FELDER = [
    {
        "gid": "f1",
        "name": "Priorität",
        "resource_subtype": "enum",
        "enum_options": [
            {"gid": "o1", "name": "Hoch", "enabled": True},
            {"gid": "o2", "name": "Alt", "enabled": False},
        ],
    },
    {"gid": "f2", "name": "Budget", "resource_subtype": "number", "precision": 2},
]


@pytest.fixture
def fake() -> FakeAsana:
    return FakeAsana()


@pytest.fixture
def akontext(kontext, fake):
    return asana_kontext(kontext, fake)


def test_registry_findet_die_neuen_lese_tools(kontext):
    registry = lade_registry(kontext)
    for name in NEUE_LESE_TOOLS:
        tool = registry.hole(name)
        assert tool is not None, name
        assert not tool.schreibend
        assert "**" not in tool.beschreibung


async def test_felder_des_workspace_mit_typ_und_aktiven_optionen(akontext, fake):
    fake.route("GET", "/workspaces/ws1/custom_fields", FELDER)
    ergebnis = await AsanaFelderAnzeigen(akontext).ausfuehren()
    assert ergebnis["eintraege"] == [
        {
            "gid": "f1",
            "name": "Priorität",
            "typ": "enum",
            "optionen": [{"gid": "o1", "name": "Hoch"}],
        },
        {"gid": "f2", "name": "Budget", "typ": "number", "nachkommastellen": 2},
    ]
    assert "enum_options.name" in fake.anfragen[0].url.params["opt_fields"]


async def test_felder_eines_projekts(akontext, fake):
    fake.route(
        "GET",
        "/projects/100/custom_field_settings",
        [{"gid": "s1", "custom_field": FELDER[1]}],
    )
    ergebnis = await AsanaFelderAnzeigen(akontext).ausfuehren(projekt_gid="100", suche="bud")
    assert [f["name"] for f in ergebnis["eintraege"]] == ["Budget"]
    assert "custom_field.resource_subtype" in fake.anfragen[0].url.params["opt_fields"]


async def test_paginierung_folgt_dem_offset_bis_zur_obergrenze(akontext, fake):
    fake.route(
        "GET",
        "/workspaces/ws1/custom_fields",
        httpx.Response(
            200,
            json={
                "data": [{"gid": str(n), "name": f"F{n}"} for n in range(100)],
                "next_page": {"offset": "weiter"},
            },
        ),
        httpx.Response(200, json={"data": [{"gid": "100", "name": "F100"}], "next_page": None}),
    )
    ergebnis = await AsanaFelderAnzeigen(akontext).ausfuehren(suche="F100")
    assert [f["gid"] for f in ergebnis["eintraege"]] == ["100"]
    assert fake.anfragen[0].url.params["limit"] == "100"
    assert fake.anfragen[1].url.params["offset"] == "weiter"


async def test_projektvorlagen_liste_und_details_mit_datumsvariablen(akontext, fake):
    fake.route(
        "GET",
        "/project_templates",
        [{"gid": "v1", "name": "Launch-Vorlage", "team": {"gid": "t1", "name": "Marketing"}}],
    )
    fake.route(
        "GET",
        "/project_templates/31",
        {
            "gid": "v1",
            "name": "Launch-Vorlage",
            "description": "Für Produkteinführungen",
            "team": {"gid": "t1", "name": "Marketing"},
            "requested_dates": [{"gid": "1", "name": "Start Date", "description": "Projektbeginn"}],
            "requested_roles": [{"gid": "r1", "name": "Designer"}],
        },
    )
    tool = AsanaVorlagenAnzeigen(akontext)

    liste = await tool.ausfuehren()
    assert liste["eintraege"] == [
        {"gid": "v1", "name": "Launch-Vorlage", "art": "projektvorlage", "team": "Marketing"}
    ]
    assert fake.anfragen[0].url.params["workspace"] == "ws1"

    details = await tool.ausfuehren(vorlage_gid="31")
    assert details["datumsvariablen"] == [
        {"gid": "1", "name": "Start Date", "beschreibung": "Projektbeginn"}
    ]
    assert details["rollen"] == [{"gid": "r1", "name": "Designer"}]

    await tool.ausfuehren(team_gid="77")
    assert fake.anfragen[-1].url.params["team"] == "77"


async def test_aufgabenvorlagen_eines_projekts(akontext, fake):
    fake.route("GET", "/task_templates", [{"gid": "a1", "name": "Bug melden"}])
    ergebnis = await AsanaVorlagenAnzeigen(akontext).ausfuehren(projekt_gid="100")
    assert ergebnis["eintraege"] == [{"gid": "a1", "name": "Bug melden", "art": "aufgabenvorlage"}]
    assert fake.anfragen[0].url.params["project"] == "100"


async def test_teams_und_mitglieder(akontext, fake):
    fake.route("GET", "/workspaces/ws1/teams", [{"gid": "t1", "name": "Marketing"}])
    fake.route(
        "GET", "/teams/1/users", [{"gid": "u1", "name": "Max", "email": "max@gruenschwert.de"}]
    )
    tool = AsanaTeamsAnzeigen(akontext)
    assert (await tool.ausfuehren())["eintraege"] == [{"gid": "t1", "name": "Marketing"}]
    assert (await tool.ausfuehren(team_gid="1"))["eintraege"] == [
        {"gid": "u1", "name": "Max", "email": "max@gruenschwert.de"}
    ]


async def test_portfolios_und_inhalt(akontext, fake):
    fake.route(
        "GET",
        "/portfolios",
        [{"gid": "p1", "name": "2026", "resource_type": "portfolio", "owner": {"name": "Theis"}}],
    )
    fake.route(
        "GET",
        "/portfolios/1/items",
        [{"gid": "100", "name": "Launch", "resource_type": "project", "due_on": "2026-11-01"}],
    )
    tool = AsanaPortfoliosAnzeigen(akontext)
    liste = await tool.ausfuehren()
    assert liste["eintraege"][0] == {
        "gid": "p1",
        "name": "2026",
        "art": "portfolio",
        "besitzer": "Theis",
        "faellig": None,
    }
    assert dict(fake.anfragen[0].url.params)["owner"] == "me"
    inhalt = await tool.ausfuehren(portfolio_gid="1")
    assert inhalt["eintraege"][0]["art"] == "projekt"


async def test_ziele_und_teilziele(akontext, fake):
    fake.route(
        "GET",
        "/goals",
        [
            {
                "gid": "z1",
                "name": "Umsatz verdoppeln",
                "owner": {"name": "Theis"},
                "due_on": "2026-12-31",
                "status": "green",
                "time_period": {"display_name": "FY26"},
            }
        ],
    )
    fake.route(
        "GET",
        "/goal_relationships",
        [
            {
                "resource_subtype": "subgoal",
                "supporting_resource": {"gid": "z2", "name": "B2B", "resource_type": "goal"},
            },
            {
                "resource_subtype": "supporting_work",
                "supporting_resource": {"gid": "100", "name": "Launch", "resource_type": "project"},
            },
        ],
    )
    tool = AsanaZieleAnzeigen(akontext)
    liste = await tool.ausfuehren()
    assert liste["eintraege"][0]["zeitraum"] == "FY26"
    assert fake.anfragen[0].url.params["workspace"] == "ws1"
    teile = await tool.ausfuehren(ziel_gid="1")
    assert [(t["name"], t["art"]) for t in teile["eintraege"]] == [
        ("B2B", "teilziel"),
        ("Launch", "project"),
    ]
    assert fake.anfragen[1].url.params["supported_goal"] == "1"


async def test_anhaenge_einer_aufgabe(akontext, fake):
    fake.route(
        "GET",
        "/attachments",
        [
            {
                "gid": "a1",
                "name": "Angebot.PDF",
                "resource_subtype": "asana",
                "size": 2_621_440,
                "created_at": "2026-10-01T08:00:00Z",
            },
            {
                "gid": "a2",
                "name": "Briefing",
                "resource_subtype": "external",
                "view_url": "https://example.com/briefing",
            },
        ],
    )
    ergebnis = await AsanaAnhaengeAnzeigen(akontext).ausfuehren(objekt_gid="7")
    assert ergebnis["eintraege"] == [
        {
            "gid": "a1",
            "name": "Angebot.PDF",
            "art": "asana",
            "dateityp": "pdf",
            "erstellt_am": "2026-10-01T08:00:00Z",
            "groesse": "2,5 MB",
        },
        {
            "gid": "a2",
            "name": "Briefing",
            "art": "external",
            "dateityp": "",
            "erstellt_am": None,
            "link": "https://example.com/briefing",
        },
    ]
    assert fake.anfragen[0].url.params["parent"] == "7"


async def test_zeiteintraege_mit_summe(akontext, fake):
    fake.route(
        "GET",
        "/tasks/7/time_tracking_entries",
        [
            {"gid": "e1", "duration_minutes": 90, "entered_on": "2026-10-05"},
            {"gid": "e2", "duration_minutes": 30, "entered_on": "2026-10-06"},
        ],
    )
    ergebnis = await AsanaZeiteintraegeAnzeigen(akontext).ausfuehren(aufgabe_gid="7")
    assert ergebnis["summe_minuten"] == 120
    assert ergebnis["eintraege"][0] == {
        "gid": "e1",
        "datum": "2026-10-05",
        "minuten": 90,
        "von": "",
    }


async def test_statusmeldungen(akontext, fake):
    fake.route(
        "GET",
        "/status_updates",
        [
            {
                "gid": "s1",
                "title": "KW 41",
                "status_type": "at_risk",
                "text": "x" * 900,
                "author": {"name": "Lea"},
                "created_at": "2026-10-06T09:00:00Z",
            }
        ],
    )
    ergebnis = await AsanaStatusmeldungenAnzeigen(akontext).ausfuehren(objekt_gid="100")
    (meldung,) = ergebnis["eintraege"]
    assert (meldung["titel"], meldung["status"], meldung["von"]) == ("KW 41", "at_risk", "Lea")
    assert len(meldung["text"]) == 500


@pytest.mark.parametrize("status", [402, 403])
async def test_tarif_fehler_wird_verstaendlich_uebersetzt(akontext, fake, user, status):
    fake.route(
        "GET", "/portfolios", httpx.Response(status, json={"errors": [{"message": "Payment"}]})
    )
    ergebnis = await fuehre_tool_aus(AsanaPortfoliosAnzeigen(akontext), {}, user, akontext)
    assert ergebnis.fehler
    assert "in eurem Asana-Tarif nicht verfügbar oder dein Token darf das nicht" in ergebnis.text
    assert ASANA_TOKEN not in ergebnis.text


# ---------------------------------------------------------------- Aufgaben: Felder, Anhänge


def _aufgabe(**extra) -> dict:
    return {
        "gid": "7",
        "name": "Etiketten",
        "completed": False,
        "due_on": "2026-10-12",
        "custom_fields": [
            {"gid": "f1", "name": "Priorität", "resource_subtype": "enum", "display_value": "Hoch"},
            {"gid": "f2", "name": "Budget", "resource_subtype": "number", "display_value": None},
        ],
        **extra,
    }


async def test_details_liefern_felder_anhaenge_und_wiederholung(akontext, fake):
    wiederholung = {"type": "weekly", "data": {"days_of_week": [1]}}
    fake.route("GET", "/tasks/7", _aufgabe(recurrence=wiederholung, approval_status="pending"))
    for pfad in ("subtasks", "dependencies"):
        fake.route("GET", f"/tasks/7/{pfad}", [])
    fake.route(
        "GET", "/tasks/7/stories", [{"gid": "k1", "type": "comment", "text": "Bitte prüfen"}]
    )
    fake.route("GET", "/attachments", [{"gid": "a1"}, {"gid": "a2"}])

    ergebnis = await AsanaAufgabeDetails(akontext).ausfuehren(aufgabe_gid="7")

    assert ergebnis["felder"] == {"Priorität": "Hoch"}
    assert ergebnis["benutzerfelder"] == [
        {"gid": "f1", "name": "Priorität", "typ": "enum", "wert": "Hoch"},
        {"gid": "f2", "name": "Budget", "typ": "number", "wert": None},
    ]
    assert ergebnis["anhaenge"] == 2
    assert ergebnis["wiederholung"] == wiederholung
    assert ergebnis["genehmigung"] == "pending"
    assert ergebnis["kommentare"][0]["gid"] == "k1"
    assert "recurrence" in fake.anfragen[0].url.params["opt_fields"]


async def test_suche_liefert_felder_und_anzahl_der_anhaenge(akontext, fake):
    fake.route("GET", "/workspaces/ws1/tasks/search", [_aufgabe(), _aufgabe(gid="8", name="B")])
    fake.route(
        "GET",
        "/attachments",
        lambda anfrage: httpx.Response(
            200, json={"data": [{"gid": "a"}] if anfrage.url.params["parent"] == "7" else []}
        ),
    )
    ergebnis = await AsanaAufgabenSuchen(akontext).ausfuehren(projekt_gid="100", details=True)
    erste, zweite = ergebnis["eintraege"]
    assert (erste["anhaenge"], zweite["anhaenge"]) == (1, 0)
    assert erste["felder"] == {"Priorität": "Hoch"}
    assert "wiederholung" not in erste


async def test_lehnt_asana_das_wiederholungsfeld_ab_geht_es_ohne_weiter(akontext, fake):
    def antwort(anfrage: httpx.Request) -> httpx.Response:
        if "recurrence" in anfrage.url.params["opt_fields"]:
            return httpx.Response(400, json={"errors": [{"message": "Unknown field"}]})
        return httpx.Response(200, json={"data": [_aufgabe()]})

    fake.route("GET", "/workspaces/ws1/tasks/search", antwort)
    tool = AsanaAufgabenSuchen(akontext)
    assert (await tool.ausfuehren(projekt_gid="100", details=True))["anzahl"] == 1
    assert (await tool.ausfuehren(projekt_gid="100", details=True))["anzahl"] == 1
    suchen = [a for a in fake.anfragen if a.url.path.endswith("/tasks/search")]
    # Erst mit, dann ohne das Feld; beim zweiten Aufruf wird es gar nicht mehr angefragt.
    assert ["recurrence" in a.url.params["opt_fields"] for a in suchen] == [True, False, False]


# ---------------------------------------------------------------- Anhang ansehen


def _anhang_routen(fake: FakeAsana, daten: bytes, **felder) -> list[httpx.Request]:
    downloads: list[httpx.Request] = []
    fake.route(
        "GET",
        "/attachments/55",
        {
            "gid": "55",
            "name": "plan.png",
            "resource_subtype": "asana",
            "size": len(daten),
            "download_url": DOWNLOAD,
            "parent": {"gid": "7", "name": "Etiketten"},
            **felder,
        },
    )

    def download(anfrage: httpx.Request) -> httpx.Response:
        downloads.append(anfrage)
        return httpx.Response(200, content=daten)

    fake.route("GET", "/datei/abc", download)
    return downloads


async def test_anhang_ansehen_gibt_das_bild_an_claude_und_nie_den_token_an_den_speicher(
    akontext, fake, user, session_fabrik, kosten
):
    downloads = _anhang_routen(fake, PNG)
    registry = lade_registry(akontext)
    client = FakeAnthropic(
        claude_antwort(tool_use_block("asana_anhang_ansehen", {"anhang_gid": "55"})),
        claude_antwort(text_block("Auf dem Plan stehen drei Aufgaben.")),
    )
    agent = Agent(
        akontext.settings, session_fabrik, client, registry, Freigaben(akontext, registry), kosten
    )
    await agent.beantworte(
        EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="zeig"),
        user,
    )

    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    text, bild = ergebnis["content"]
    assert '"medientyp": "image/png"' in text["text"]
    assert "_ansicht" not in text["text"]
    assert bild == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(PNG).decode(),
        },
    }
    (download,) = downloads
    assert download.url.host == "asana-user-private.example.com"
    assert "authorization" not in download.headers
    assert ASANA_TOKEN not in str(download.url) + str(dict(download.headers))
    async with session_fabrik() as session:
        (eintrag,) = list(await session.scalars(select(AuditLog)))
    assert "bildinhalt" not in str(eintrag.parameter) + eintrag.ergebnis_kurz
    assert base64.standard_b64encode(PNG).decode()[:30] not in eintrag.ergebnis_kurz


async def test_pdf_anhang_geht_als_dokument(akontext, fake, user):
    _anhang_routen(fake, PDF, name="angebot.pdf")
    ergebnis = await fuehre_tool_aus(
        AsanaAnhangAnsehen(akontext), {"anhang_gid": "55"}, user, akontext
    )
    (block,) = ergebnis.bloecke
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"


async def test_zu_grosser_anhang_wird_nicht_geladen(kontext, fake):
    akontext = asana_kontext(kontext, fake, asana_attachment_view_max_mb=0.0001)
    downloads = _anhang_routen(fake, PNG)
    with pytest.raises(ToolFehler, match="größer als 0.0001 MB"):
        await AsanaAnhangAnsehen(akontext).ausfuehren(anhang_gid="55")
    assert downloads == []
    # Auch wenn Asana keine Größe nennt, bricht der Download an der Grenze ab.
    _anhang_routen(fake, PNG, size=None)
    with pytest.raises(ToolFehler, match="größer als 0.0001 MB"):
        await AsanaAnhangAnsehen(akontext).ausfuehren(anhang_gid="55")


async def test_andere_formate_und_links_werden_abgelehnt(akontext, fake):
    _anhang_routen(fake, b"PK\x03\x04 eine docx-Datei")
    with pytest.raises(ToolFehler, match="nur Bilder und PDFs"):
        await AsanaAnhangAnsehen(akontext).ausfuehren(anhang_gid="55")
    _anhang_routen(fake, PNG, download_url=None, resource_subtype="external")
    with pytest.raises(ToolFehler, match="lässt sich nicht herunterladen"):
        await AsanaAnhangAnsehen(akontext).ausfuehren(anhang_gid="55")
    _anhang_routen(fake, PNG, download_url="http://unsicher.example.com/x")
    with pytest.raises(ToolFehler, match="keine gültige Download-Adresse"):
        await AsanaAnhangAnsehen(akontext).ausfuehren(anhang_gid="55")


# ---------------------------------------------------------------- Config


def test_leere_werte_in_der_env_bedeuten_standardwert(monkeypatch):
    from tests.test_config import PFLICHT

    for name, wert in PFLICHT.items():
        monkeypatch.setenv(name, wert)
    for name in (
        "ASANA_MAX_OPS_PER_CHANGESET",
        "ASANA_DELETE_ENABLED",
        "ASANA_DELETE_ROLES",
        "ASANA_ATTACHMENT_VIEW_MAX_MB",
        "PHOTO_MAX_MB",
        "MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)
    assert settings.asana_max_ops_per_changeset == 100
    assert settings.asana_delete_enabled is True
    assert settings.asana_delete_roles == {"admin"}
    assert settings.asana_attachment_view_max_mb == 5
    assert settings.photo_max_mb == 5
    assert settings.max_output_tokens == 8000
