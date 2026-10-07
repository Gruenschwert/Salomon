import httpx
import pytest

from app.tools.asana_lesen import (
    AsanaAbschnitteAnzeigen,
    AsanaAufgabeDetails,
    AsanaAufgabenSuchen,
    AsanaNutzerSuchen,
    AsanaProjekteSuchen,
    AsanaTagsAnzeigen,
)
from app.tools.base import ToolFehler
from app.tools.registry import fuehre_tool_aus, lade_registry
from tests.asana_fake import ASANA_TOKEN, FakeAsana, asana_kontext

LESE_TOOLS = [
    "asana_abschnitte_anzeigen",
    "asana_aufgabe_details",
    "asana_aufgaben_suchen",
    "asana_nutzer_suchen",
    "asana_projekte_suchen",
    "asana_tags_anzeigen",
]
SUCHE = "/workspaces/ws1/tasks/search"


def _aufgabe(gid: str, name: str, faellig: str | None = None, **extra) -> dict:
    return {
        "gid": gid,
        "name": name,
        "completed": False,
        "due_on": faellig,
        "due_at": None,
        "assignee": {"gid": "u1", "name": "Max"},
        "memberships": [
            {"project": {"gid": "p1", "name": "Launch"}, "section": {"gid": "s1", "name": "Offen"}}
        ],
        **extra,
    }


@pytest.fixture
def fake() -> FakeAsana:
    return FakeAsana()


def _letzte(fake: FakeAsana, pfad: str):
    return [a for a in fake.anfragen if a.url.path == f"/api/1.0{pfad}"][-1]


@pytest.fixture
def akontext(kontext, fake):
    return asana_kontext(kontext, fake)


def test_registry_findet_alle_lese_tools(kontext):
    registry = lade_registry(kontext)
    for name in LESE_TOOLS:
        tool = registry.hole(name)
        assert tool is not None, name
        assert not tool.schreibend
        assert tool.erlaubte_rollen == {"admin", "user"}


async def test_projekte_suchen_filtert_nach_namen(akontext, fake):
    fake.route(
        "GET",
        "/projects",
        [
            {
                "gid": "p1",
                "name": "Kiffkraut Launch",
                "team": {"gid": "t1", "name": "Marketing"},
                "owner": {"gid": "u1", "name": "Theis"},
                "due_on": "2026-11-01",
                "archived": False,
                "permalink_url": "https://app.asana.com/0/p1",
            },
            {"gid": "p2", "name": "Lager", "team": None, "owner": None},
        ],
    )
    ergebnis = await AsanaProjekteSuchen(akontext).ausfuehren(suche="launch")

    assert ergebnis["eintraege"] == [
        {
            "gid": "p1",
            "name": "Kiffkraut Launch",
            "team": "Marketing",
            "besitzer": "Theis",
            "faellig": "2026-11-01",
            "archiviert": False,
            "link": "https://app.asana.com/0/p1",
        }
    ]
    params = fake.anfragen[0].url.params
    assert params["workspace"] == "ws1"
    assert params["archived"] == "false"
    assert "notes" not in params["opt_fields"]


async def test_projekte_suchen_mit_archivierten(akontext, fake):
    fake.route("GET", "/projects", [{"gid": "p2", "name": "Alt", "archived": True}])
    ergebnis = await AsanaProjekteSuchen(akontext).ausfuehren(nur_aktive=False)
    assert ergebnis["eintraege"][0]["archiviert"] is True
    assert "archived" not in fake.anfragen[0].url.params


async def test_projekte_liste_wird_auf_30_gekuerzt(akontext, fake):
    fake.route("GET", "/projects", [{"gid": str(n), "name": f"P{n}"} for n in range(45)])
    ergebnis = await AsanaProjekteSuchen(akontext).ausfuehren()
    assert ergebnis["anzahl"] == 30
    assert "hinweis" in ergebnis


async def test_aufgaben_suchen_ueber_die_suche(akontext, fake):
    fake.route(
        "GET",
        SUCHE,
        [
            _aufgabe("2", "Etiketten drucken", "2026-10-12"),
            _aufgabe("1", "Etiketten bestellen", "2026-10-09"),
            _aufgabe("3", "Etiketten prüfen", "2026-10-20"),
        ],
    )
    ergebnis = await AsanaAufgabenSuchen(akontext).ausfuehren(
        text="etiketten",
        zustaendig_gid="me",
        faellig_von="2026-10-09",
        faellig_bis="2026-10-12",
    )

    assert [a["gid"] for a in ergebnis["eintraege"]] == ["1", "2"]
    assert ergebnis["eintraege"][0] == {
        "gid": "1",
        "name": "Etiketten bestellen",
        "faellig": "2026-10-09",
        "faellig_um": None,
        "zustaendig": "Max",
        "projekte": [{"projekt": "Launch", "abschnitt": "Offen"}],
        "erledigt": False,
    }
    params = fake.anfragen[0].url.params
    assert params["assignee.any"] == "me"
    assert params["text"] == "etiketten"
    assert params["completed"] == "false"
    assert params["due_on.after"] == "2026-10-08"
    assert params["due_on.before"] == "2026-10-13"


async def test_aufgaben_suchen_weicht_ohne_bezahlten_tarif_auf_die_liste_aus(akontext, fake):
    fake.route("GET", SUCHE, httpx.Response(402, json={"errors": [{"message": "Payment"}]}))
    fake.route(
        "GET",
        "/tasks",
        [_aufgabe("1", "Etiketten"), _aufgabe("2", "Karton"), _aufgabe("3", "Etikett alt")],
    )
    tool = AsanaAufgabenSuchen(akontext)

    ergebnis = await tool.ausfuehren(text="etikett", projekt_gid="77")
    assert [a["gid"] for a in ergebnis["eintraege"]] == ["1", "3"]
    liste = _letzte(fake, "/tasks").url.params
    assert liste["project"] == "77"
    assert liste["completed_since"] == "now"

    # Beim zweiten Aufruf wird die Suche gar nicht mehr versucht.
    await tool.ausfuehren(zustaendig_gid="me")
    assert fake.aufrufe().count(("GET", SUCHE)) == 1
    assert _letzte(fake, "/tasks").url.params["assignee"] == "me"
    assert _letzte(fake, "/tasks").url.params["workspace"] == "ws1"


async def test_aufgaben_suchen_ohne_suche_braucht_projekt_oder_zustaendigen(akontext, fake):
    fake.route("GET", SUCHE, httpx.Response(402, json={"errors": []}))
    with pytest.raises(ToolFehler, match="projekt_gid oder zustaendig_gid"):
        await AsanaAufgabenSuchen(akontext).ausfuehren(text="x")


async def test_aufgaben_suchen_prueft_eingaben(akontext, fake):
    tool = AsanaAufgabenSuchen(akontext)
    with pytest.raises(ToolFehler, match="mindestens einen Filter"):
        await tool.ausfuehren()
    with pytest.raises(ToolFehler, match="JJJJ-MM-TT"):
        await tool.ausfuehren(faellig_von="nächsten Freitag")
    with pytest.raises(ToolFehler, match="gültige Asana-GID"):
        await tool.ausfuehren(projekt_gid="1/../../users")
    assert fake.anfragen == []


async def test_aufgaben_suchen_limit(akontext, fake):
    fake.route("GET", SUCHE, [_aufgabe(str(n), f"A{n}") for n in range(50)])
    tool = AsanaAufgabenSuchen(akontext)
    assert (await tool.ausfuehren(projekt_gid="77", limit=99))["anzahl"] == 30
    assert (await tool.ausfuehren(projekt_gid="77", limit=5))["anzahl"] == 5


async def test_aufgabe_details(akontext, fake):
    fake.route(
        "GET",
        "/tasks/5",
        _aufgabe(
            "5",
            "Messe vorbereiten",
            "2026-10-15",
            notes="x" * 5000,
            start_on="2026-10-10",
            resource_subtype="milestone",
            parent={"gid": "4", "name": "Messe"},
            tags=[{"gid": "g1", "name": "dringend"}],
            followers=[{"gid": "u2", "name": "Lea"}],
            permalink_url="https://app.asana.com/0/p1/5",
        ),
    )
    fake.route(
        "GET",
        "/tasks/5/subtasks",
        [{"gid": "6", "name": "Stand buchen", "completed": True, "due_on": None, "assignee": None}],
    )
    fake.route("GET", "/tasks/5/dependencies", [{"gid": "7", "name": "Budget", "completed": False}])
    fake.route(
        "GET",
        "/tasks/5/stories",
        [{"type": "system", "text": "hat die Aufgabe erstellt"}]
        + [
            {
                "type": "comment",
                "text": f"Kommentar {n}",
                "created_at": "2026-10-01T08:00:00Z",
                "created_by": {"gid": "u2", "name": "Lea"},
            }
            for n in range(12)
        ],
    )
    ergebnis = await AsanaAufgabeDetails(akontext).ausfuehren(aufgabe_gid="5")

    assert ergebnis["name"] == "Messe vorbereiten"
    assert ergebnis["meilenstein"] is True
    assert ergebnis["startdatum"] == "2026-10-10"
    assert len(ergebnis["beschreibung"]) == 2000
    assert ergebnis["uebergeordnet"] == {"gid": "4", "name": "Messe"}
    assert ergebnis["tags"] == [{"gid": "g1", "name": "dringend"}]
    assert ergebnis["follower"] == [{"gid": "u2", "name": "Lea"}]
    assert ergebnis["unteraufgaben"][0]["erledigt"] is True
    assert ergebnis["haengt_ab_von"] == [{"gid": "7", "name": "Budget", "erledigt": False}]
    assert [k["text"] for k in ergebnis["kommentare"]] == [f"Kommentar {n}" for n in range(2, 12)]
    assert ergebnis["kommentare"][0]["von"] == "Lea"


async def test_abschnitte_mit_anzahl_offener_aufgaben(akontext, fake):
    fake.route(
        "GET", "/projects/1/sections", [{"gid": "10", "name": "Offen"}, {"gid": "11", "name": "OK"}]
    )
    fake.route("GET", "/sections/10/tasks", [{"gid": "a"}, {"gid": "b"}])
    fake.route("GET", "/sections/11/tasks", [])
    ergebnis = await AsanaAbschnitteAnzeigen(akontext).ausfuehren(projekt_gid="1")

    assert ergebnis["eintraege"] == [
        {"gid": "10", "name": "Offen", "offene_aufgaben": 2},
        {"gid": "11", "name": "OK", "offene_aufgaben": 0},
    ]
    assert fake.anfragen[1].url.params["completed_since"] == "now"


async def test_nutzer_suchen_nach_name_oder_email(akontext, fake):
    fake.route(
        "GET",
        "/users",
        [
            {"gid": "u1", "name": "Max Muster", "email": "max@gruenschwert.de"},
            {"gid": "u2", "name": "Lea Beispiel", "email": "lea@gruenschwert.de"},
        ],
    )
    tool = AsanaNutzerSuchen(akontext)
    assert [n["gid"] for n in (await tool.ausfuehren(suche="MAX"))["eintraege"]] == ["u1"]
    assert [n["gid"] for n in (await tool.ausfuehren(suche="lea@"))["eintraege"]] == ["u2"]
    assert (await tool.ausfuehren(suche="gruenschwert"))["anzahl"] == 2
    with pytest.raises(ToolFehler, match="Suchbegriff"):
        await tool.ausfuehren(suche=" ")


async def test_tags_anzeigen(akontext, fake):
    fake.route("GET", "/tags", [{"gid": "g1", "name": "dringend"}, {"gid": "g2", "name": "Messe"}])
    tool = AsanaTagsAnzeigen(akontext)
    assert (await tool.ausfuehren())["anzahl"] == 2
    assert (await tool.ausfuehren(suche="mes"))["eintraege"] == [{"gid": "g2", "name": "Messe"}]


async def test_lese_tool_ueber_die_registry_verraet_keinen_token(akontext, fake, user):
    fake.route("GET", "/tags", httpx.Response(401, json={"errors": [{"message": ASANA_TOKEN}]}))
    ergebnis = await fuehre_tool_aus(AsanaTagsAnzeigen(akontext), {}, user, akontext)
    assert ergebnis.fehler
    assert "Asana-Zugriff verweigert" in ergebnis.text
    assert ASANA_TOKEN not in ergebnis.text


async def test_lange_antworten_werden_fuer_claude_gekuerzt(akontext, fake, user):
    fake.route("GET", "/projects", [{"gid": str(n), "name": "N" * 900} for n in range(30)])
    ergebnis = await fuehre_tool_aus(AsanaProjekteSuchen(akontext), {}, user, akontext)
    assert len(ergebnis.text) <= 8000
