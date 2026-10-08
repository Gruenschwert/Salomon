"""Mitglieder, Teams, Projekt-Einstellungen und die weiteren Aufgaben-Operationen."""

import pytest

from app.auth.approvals import Freigaben
from app.tools.base import ToolFehler
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext
from tests.test_asana_schreiben import aufgabe

NAME = "asana_aenderungen_ausfuehren"
NUTZER = [
    {"gid": "501", "name": "Max Muster", "email": "max@gruenschwert.de"},
    {"gid": "502", "name": "Lea Beispiel", "email": "lea@gruenschwert.de"},
]


def _abschnitt(gid: str, name: str) -> dict:
    return {"gid": gid, "name": name, "project": {"gid": "100", "name": "Launch"}}


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/users", NUTZER)
    fake.route("GET", "/users/501", NUTZER[0])
    fake.route("GET", "/users/me", {"gid": "501", "name": "Max Muster"})
    fake.route(
        "GET",
        "/projects/100",
        {
            "gid": "100",
            "name": "Launch",
            "privacy_setting": "public_to_workspace",
            "default_view": "list",
        },
    )
    fake.route("GET", "/projects/300", {"gid": "300", "name": "Messe"})
    fake.route("GET", "/sections/201", _abschnitt("201", "Offen"))
    fake.route("GET", "/sections/202", _abschnitt("202", "In Arbeit"))
    fake.route("GET", "/tasks/7", aufgabe())
    fake.route("GET", "/tasks/8", aufgabe("8", "Budget"))
    fake.route(
        "GET",
        "/tasks/9",
        aufgabe("9", "Angebot", parent={"gid": "8", "name": "Budget"}, memberships=[]),
    )
    fake.route("GET", "/teams/77", {"gid": "77", "name": "Marketing", "visibility": "secret"})
    return fake


@pytest.fixture
def baue(kontext, fake):
    def _baue(**settings_werte):
        akontext = asana_kontext(kontext, fake, **settings_werte)
        registry = lade_registry(akontext)
        return registry.hole(NAME), Freigaben(akontext, registry)

    return _baue


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


async def _vorschau(werkzeug, nutzer, operationen) -> list[str]:
    tool, freigaben = werkzeug
    anfrage = await freigaben.anfragen(nutzer, tool, {"operationen": operationen})
    return anfrage.vorschau_text.splitlines()


# ---------------------------------------------------------------- Projekte


async def test_projekt_mitglieder_und_follower_mit_namen_statt_gid(baue, user, fake):
    for pfad in ("addMembers", "removeMembers", "addFollowers", "removeFollowers"):
        fake.route("POST", f"/projects/100/{pfad}", {"gid": "100"})
    zeilen = await _ausfuehren(
        baue(),
        user,
        [
            {
                "operation": "projekt_mitglied_hinzufuegen",
                "projekt": "100",
                "nutzer": ["lea beispiel", "max@gruenschwert.de"],
            },
            {"operation": "projekt_mitglied_entfernen", "projekt": "100", "nutzer": ["501"]},
            {"operation": "projekt_follower_hinzufuegen", "projekt": "100", "nutzer": ["me"]},
            {"operation": "projekt_follower_entfernen", "projekt": "100", "nutzer": ["Lea"]},
        ],
    )
    assert zeilen == [
        "Asana-Änderungssatz: 4 ändern",
        "1. Mitglied hinzufügen: Lea Beispiel, Max Muster zum Projekt „Launch“",
        "2. Mitglied entfernen: Max Muster aus dem Projekt „Launch“",
        "3. Follower hinzufügen: Max Muster zum Projekt „Launch“",
        "4. Follower entfernen: Lea Beispiel aus dem Projekt „Launch“",
    ]
    assert fake.koerper("POST", "/projects/100/addMembers") == [{"members": "502,501"}]
    assert fake.koerper("POST", "/projects/100/removeMembers") == [{"members": "501"}]
    assert fake.koerper("POST", "/projects/100/addFollowers") == [{"followers": "501"}]
    assert fake.koerper("POST", "/projects/100/removeFollowers") == [{"followers": "502"}]


async def test_mehrdeutiger_nutzername_wird_nicht_geraten(baue, user, fake):
    fake.route("GET", "/users", [*NUTZER, {"gid": "503", "name": "Max Zweit", "email": "m2@x.de"}])
    with pytest.raises(ToolFehler, match="„Max“ ist nicht eindeutig: Max Muster .501., Max Zweit"):
        await _vorschau(
            baue(),
            user,
            [{"operation": "projekt_mitglied_hinzufuegen", "projekt": "100", "nutzer": ["Max"]}],
        )
    assert fake.aufrufe("POST") == []


async def test_projekt_sichtbarkeit_ansicht_und_notizen(baue, user, fake):
    fake.route("PUT", "/projects/100", {"gid": "100"})
    zeilen = await _ausfuehren(
        baue(),
        user,
        [
            {
                "operation": "projekt_aendern",
                "gid": "100",
                "sichtbarkeit": "privat",
                "standardansicht": "board",
                "notizen": "Alles zum Launch",
                "farbe": "dark-green",
            }
        ],
    )
    assert zeilen[1] == (
        "1. Ändern: Projekt „Launch“: Beschreibung geändert; "
        "Sichtbarkeit public_to_workspace → private; Standardansicht list → board; "
        "Farbe (leer) → dark-green"
    )
    assert fake.koerper("PUT", "/projects/100") == [
        {
            "notes": "Alles zum Launch",
            "privacy_setting": "private",
            "default_view": "board",
            "color": "dark-green",
        }
    ]
    with pytest.raises(ToolFehler, match="„gantt“ ist in „standardansicht“ nicht erlaubt"):
        await _vorschau(
            baue(),
            user,
            [{"operation": "projekt_aendern", "gid": "100", "standardansicht": "gantt"}],
        )


# ---------------------------------------------------------------- Teams


async def test_team_verwaltung_nur_fuer_die_erlaubten_rollen(baue, user, admin, fake):
    fake.route("POST", "/teams", {"gid": "88"})
    fake.route("POST", "/teams/88/addUser", {})
    fake.route("PUT", "/teams/77", {"gid": "77"})
    anlegen = [
        {
            "operation": "team_anlegen",
            "name": "Vertrieb",
            "team_sichtbarkeit": "auf_anfrage",
            "platzhalter": "$m1",
        },
        {"operation": "team_mitglied_hinzufuegen", "team_gid": "$m1", "nutzer": ["Lea", "Max"]},
        {"operation": "team_aendern", "team_gid": "77", "team_sichtbarkeit": "oeffentlich"},
    ]
    with pytest.raises(ToolFehler, match="team_anlegen ist dieser Rolle nicht erlaubt"):
        await _vorschau(baue(), user, anlegen)
    assert fake.aufrufe("POST") == []

    fake.route("GET", "/teams/88", {"gid": "88", "name": "Vertrieb"})
    zeilen = await _ausfuehren(baue(), admin, anlegen)
    assert zeilen == [
        "Asana-Änderungssatz: 1 anlegen, 2 ändern",
        "1. Anlegen: Team „Vertrieb“ (auf_anfrage)",
        "2. Mitglied hinzufügen: Lea Beispiel, Max Muster zum Team „Vertrieb“ (neu)",
        "3. Ändern: Team „Marketing“: Sichtbarkeit secret → public",
    ]
    assert fake.koerper("POST", "/teams") == [
        {"name": "Vertrieb", "visibility": "request_to_join", "organization": "ws1"}
    ]
    # Je Nutzer ein Aufruf, wie es der Endpunkt verlangt.
    assert fake.koerper("POST", "/teams/88/addUser") == [{"user": "502"}, {"user": "501"}]
    assert fake.koerper("PUT", "/teams/77") == [{"visibility": "public"}]
    # Mit freigeschalteter Rolle darf auch ein normaler Nutzer.
    offen = baue(asana_team_verwaltung_roles=frozenset({"admin", "user"}))
    await _vorschau(offen, user, anlegen[:1])


async def test_team_mitglied_entfernen_zaehlt_als_loeschung(baue, user, admin, fake):
    fake.route("POST", "/teams/77/removeUser", {})
    op = {"operation": "team_mitglied_entfernen", "team_gid": "77", "nutzer": ["Lea"]}
    zeilen = await _ausfuehren(baue(), admin, [op], bestaetigt=True)
    assert zeilen == [
        "Asana-Änderungssatz: 1 🗑 löschen",
        "1. 🗑 Löschen: Mitgliedschaft von Lea Beispiel im Team „Marketing“",
    ]
    assert fake.koerper("POST", "/teams/77/removeUser") == [{"user": "502"}]
    # Das Limit für Löschungen gilt auch hier.
    knapp = baue(asana_max_deletes_per_changeset=1)
    with pytest.raises(ToolFehler, match="2 Löschoperationen, erlaubt sind höchstens 1"):
        await _vorschau(knapp, admin, [op, op])
    with pytest.raises(ToolFehler, match="abgeschaltet"):
        await _vorschau(baue(asana_delete_enabled=False), admin, [op])


# ---------------------------------------------------------------- Aufgaben


async def test_mehrfachzuordnung_mit_abschnitt_und_position(baue, user, fake):
    fake.route("GET", "/sections/301", {"gid": "301", "name": "Stand", "project": {"gid": "300"}})
    fake.route("POST", "/tasks/7/addProject", {})
    fake.route("POST", "/tasks/7/removeProject", {})
    zeilen = await _ausfuehren(
        baue(),
        user,
        [
            {
                "operation": "aufgabe_zu_projekt_hinzufuegen",
                "gid": "7",
                "projekt": "300",
                "abschnitt": "301",
                "nach_aufgabe_gid": "8",
            },
            {"operation": "aufgabe_aus_projekt_entfernen", "gid": "7", "projekt": "100"},
        ],
    )
    assert zeilen[1] == (
        "1. Zuordnen: „Etiketten“ zusätzlich zum Projekt „Messe“ / „Stand“, nach „Budget“"
    )
    assert zeilen[2] == (
        "2. Zuordnung lösen: „Etiketten“ aus dem Projekt „Launch“ "
        "– ⚠️ danach liegt sie in keinem Projekt mehr"
    )
    assert fake.koerper("POST", "/tasks/7/addProject") == [
        {"project": "300", "section": "301", "insert_after": "8"}
    ]
    assert fake.koerper("POST", "/tasks/7/removeProject") == [{"project": "100"}]
    with pytest.raises(ToolFehler, match="gehört nicht zum Projekt"):
        await _vorschau(
            baue(),
            user,
            [
                {
                    "operation": "aufgabe_zu_projekt_hinzufuegen",
                    "gid": "7",
                    "projekt": "300",
                    "abschnitt": "201",
                }
            ],
        )


async def test_unteraufgabe_umhaengen_und_herausloesen(baue, user, fake):
    fake.route("POST", "/tasks/9/setParent", {"gid": "9"})
    fake.route("POST", "/tasks/9/addProject", {})
    zeilen = await _ausfuehren(
        baue(),
        user,
        [
            {
                "operation": "unteraufgabe_umhaengen",
                "gid": "9",
                "uebergeordnet": "7",
                "vor_aufgabe_gid": "8",
            },
            {"operation": "unteraufgabe_zu_aufgabe_machen", "gid": "9", "abschnitt": "202"},
        ],
    )
    assert zeilen[0] == "Asana-Änderungssatz: 2 verschieben"
    assert zeilen[1] == "1. Umhängen: „Angebot“ von „Budget“ unter „Etiketten“, vor „Budget“"
    assert zeilen[2] == (
        "2. Herauslösen: „Angebot“ wird eine eigenständige Aufgabe (bisher unter „Budget“), "
        "im Projekt „Launch“ / „In Arbeit“"
    )
    assert fake.koerper("POST", "/tasks/9/setParent") == [
        {"parent": "7", "insert_before": "8"},
        {"parent": None},
    ]
    assert fake.koerper("POST", "/tasks/9/addProject") == [{"project": "100", "section": "202"}]
    with pytest.raises(ToolFehler, match="ist keine Unteraufgabe"):
        await _vorschau(baue(), user, [{"operation": "unteraufgabe_zu_aufgabe_machen", "gid": "7"}])


async def test_reihenfolge_von_aufgaben_und_abschnitten(baue, user, fake):
    fake.route("POST", "/sections/201/addTask", {})
    fake.route("POST", "/projects/100/sections/insert", {})
    zeilen = await _ausfuehren(
        baue(),
        user,
        [
            {
                "operation": "reihenfolge_aendern",
                "abschnitt": "201",
                "aufgabe_gid": "7",
                "vor_aufgabe_gid": "8",
            },
            {"operation": "reihenfolge_aendern", "abschnitt": "202", "vor_abschnitt_gid": "201"},
        ],
    )
    assert zeilen[1] == "1. Umsortieren: „Etiketten“ im Abschnitt „Offen“ vor „Budget“"
    assert zeilen[2] == "2. Umsortieren: Abschnitt „In Arbeit“ vor „Offen“"
    assert fake.koerper("POST", "/sections/201/addTask") == [{"task": "7", "insert_before": "8"}]
    assert fake.koerper("POST", "/projects/100/sections/insert") == [
        {"section": "202", "before_section": "201"}
    ]
    for op, meldung in (
        ({"abschnitt": "201"}, "vor_abschnitt_gid“ oder „nach_abschnitt_gid"),
        ({"abschnitt": "201", "aufgabe_gid": "7"}, "vor_aufgabe_gid“ oder „nach_aufgabe_gid"),
        (
            {
                "abschnitt": "201",
                "aufgabe_gid": "7",
                "vor_aufgabe_gid": "8",
                "nach_aufgabe_gid": "8",
            },
            "nicht beide",
        ),
    ):
        with pytest.raises(ToolFehler, match=meldung):
            await _vorschau(baue(), user, [{"operation": "reihenfolge_aendern", **op}])


def _kommentar(autor: str = "501") -> dict:
    return {
        "gid": "61",
        "type": "comment",
        "text": "Bitte bis Freitag",
        "created_by": {"gid": autor, "name": "Max Muster" if autor == "501" else "Lea Beispiel"},
        "target": {"gid": "7", "name": "Etiketten"},
    }


async def test_eigenen_kommentar_bearbeiten_und_loeschen(baue, admin, fake):
    fake.route("GET", "/stories/61", _kommentar())
    fake.route("PUT", "/stories/61", {"gid": "61"})
    fake.route("DELETE", "/stories/61", {})
    zeilen = await _ausfuehren(
        baue(),
        admin,
        [{"operation": "kommentar_bearbeiten", "kommentar_gid": "61", "text": "Bitte bis Montag"}],
    )
    assert zeilen[1] == (
        "1. Kommentar ändern: „Bitte bis Freitag“ an „Etiketten“ → „Bitte bis Montag“"
    )
    assert fake.koerper("PUT", "/stories/61") == [{"text": "Bitte bis Montag"}]
    zeilen = await _ausfuehren(
        baue(), admin, [{"operation": "kommentar_loeschen", "kommentar_gid": "61"}], bestaetigt=True
    )
    assert zeilen[1] == "1. 🗑 Löschen: Kommentar „Bitte bis Freitag“ an „Etiketten“"
    assert fake.aufrufe("DELETE") == [("DELETE", "/stories/61")]


async def test_fremde_kommentare_bleiben_unangetastet(baue, admin, fake):
    fake.route("GET", "/stories/61", _kommentar(autor="502"))
    for op in (
        {"operation": "kommentar_bearbeiten", "kommentar_gid": "61", "text": "x"},
        {"operation": "kommentar_loeschen", "kommentar_gid": "61"},
    ):
        with pytest.raises(ToolFehler, match="stammt von Lea Beispiel.*nur eigene"):
            await _vorschau(baue(), admin, [op])
    fake.route("GET", "/stories/61", {**_kommentar(), "type": "system"})
    with pytest.raises(ToolFehler, match="kein Kommentar"):
        await _vorschau(baue(), admin, [{"operation": "kommentar_loeschen", "kommentar_gid": "61"}])
    assert fake.aufrufe("PUT") == [] and fake.aufrufe("DELETE") == []


async def test_genehmigung_anlegen_und_entscheiden(baue, user, fake):
    fake.route("POST", "/tasks", {"gid": "930"})
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    fake.route("PUT", "/tasks/8", {"gid": "8"})
    fake.route("GET", "/tasks/8", aufgabe("8", "Budget", resource_subtype="approval"))
    zeilen = await _ausfuehren(
        baue(),
        user,
        [
            {"operation": "aufgabe_anlegen", "name": "Flyer freigeben", "genehmigung": True},
            {"operation": "aufgabe_genehmigung", "gid": "7", "status": "aenderungen_noetig"},
            {"operation": "aufgabe_genehmigung", "gid": "8", "status": "genehmigt"},
        ],
    )
    assert zeilen[1].startswith("1. Anlegen: Genehmigung „Flyer freigeben“")
    assert zeilen[2] == (
        "2. Genehmigung: „Etiketten“: wird zur Genehmigung; Stand → aenderungen_noetig"
    )
    assert zeilen[3] == "3. Genehmigung: „Budget“: Stand → genehmigt"
    assert fake.koerper("POST", "/tasks")[0]["resource_subtype"] == "approval"
    assert fake.koerper("PUT", "/tasks/7") == [
        {"resource_subtype": "approval", "approval_status": "changes_requested"}
    ]
    assert fake.koerper("PUT", "/tasks/8") == [{"approval_status": "approved"}]
    with pytest.raises(ToolFehler, match="entweder Meilenstein oder Genehmigung"):
        await _vorschau(
            baue(),
            user,
            [
                {
                    "operation": "aufgabe_anlegen",
                    "name": "x",
                    "genehmigung": True,
                    "meilenstein": True,
                }
            ],
        )


async def test_follower_an_aufgaben_und_gefaellt_mir(baue, user, fake):
    fake.route("POST", "/tasks/7/addFollowers", {"gid": "7"})
    fake.route("POST", "/tasks/7/removeFollowers", {"gid": "7"})
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    zeilen = await _ausfuehren(
        baue(),
        user,
        [
            {"operation": "follower_hinzufuegen", "aufgabe_gid": "7", "nutzer": ["Lea"]},
            {"operation": "follower_entfernen", "aufgabe_gid": "7", "nutzer": ["Max", "Lea"]},
            {"operation": "aufgabe_gefaellt_mir", "gid": "7"},
        ],
    )
    assert zeilen[1] == "1. Follower hinzufügen: Lea Beispiel bei „Etiketten“"
    assert zeilen[2] == "2. Follower entfernen: Max Muster, Lea Beispiel bei „Etiketten“"
    assert zeilen[3] == "3. „Gefällt mir“ setzen: „Etiketten“"
    assert fake.koerper("POST", "/tasks/7/addFollowers") == [{"followers": ["502"]}]
    assert fake.koerper("POST", "/tasks/7/removeFollowers") == [{"followers": ["501", "502"]}]
    assert fake.koerper("PUT", "/tasks/7") == [{"liked": True}]


async def test_limit_gilt_auch_fuer_neue_operationen(baue, user, fake):
    knapp = baue(asana_max_ops_per_changeset=2)
    ops = [{"operation": "aufgabe_gefaellt_mir", "gid": "7"}] * 3
    with pytest.raises(ToolFehler, match="hat 3 Operationen, erlaubt sind höchstens 2"):
        await _vorschau(knapp, user, ops)
    await _vorschau(knapp, user, ops[:2])
