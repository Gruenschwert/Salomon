import httpx
import pytest
from sqlalchemy import select

from app.agent.history import lade_verlauf
from app.agent.loop import Agent
from app.auth.approvals import STATUS_BEREITS_ENTSCHIEDEN, Freigaben
from app.channels.base import EingehendeNachricht, FreigabeAnfrage
from app.channels.telegram import TELEGRAM_MAX_ZEICHEN, TelegramKanal, teile_text
from app.db.models import Approval, AsanaOperation, AuditLog
from app.tools.asana_schreiben import AsanaAenderungenAusfuehren
from app.tools.base import ToolFehler, aktuelle_freigabe, aktueller_nutzer
from app.tools.registry import lade_registry
from tests.asana_fake import ASANA_TOKEN, FakeAsana, asana_kontext
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

NAME = "asana_aenderungen_ausfuehren"
SCHREIBEND = ("POST", "PUT", "DELETE")

PLAN = [
    {"operation": "projekt_anlegen", "platzhalter": "$p1", "name": "Messe 2026"},
    {"operation": "abschnitt_anlegen", "platzhalter": "$s1", "projekt": "$p1", "name": "Planung"},
    {
        "operation": "aufgabe_anlegen",
        "platzhalter": "$a1",
        "name": "Stand buchen",
        "abschnitt": "$s1",
        "faellig": "2026-10-12",
        "zustaendig_gid": "501",
    },
    {
        "operation": "aufgabe_anlegen",
        "name": "Angebot einholen",
        "uebergeordnet": "$a1",
        "faellig_um": "2026-10-15T14:30",
    },
]


def aufgabe(gid: str = "7", name: str = "Etiketten", **extra) -> dict:
    return {
        "gid": gid,
        "name": name,
        "notes": "Alte Beschreibung",
        "completed": False,
        "due_on": "2026-10-12",
        "due_at": None,
        "start_on": None,
        "resource_subtype": "default_task",
        "assignee": {"gid": "501", "name": "Max"},
        "parent": None,
        "memberships": [
            {
                "project": {"gid": "100", "name": "Launch"},
                "section": {"gid": "201", "name": "Offen"},
            }
        ],
        "permalink_url": f"https://app.asana.com/0/100/{gid}",
        **extra,
    }


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/users/501", {"gid": "501", "name": "Max"})
    fake.route("GET", "/tasks/7", aufgabe())
    fake.route("GET", "/sections/201", {"gid": "201", "name": "Offen", "project": _projekt("100")})
    fake.route(
        "GET", "/sections/202", {"gid": "202", "name": "In Arbeit", "project": _projekt("100")}
    )
    fake.route("GET", "/projects/100", {**_projekt("100"), "archived": False})
    return fake


def _projekt(gid: str, name: str = "Launch") -> dict:
    return {"gid": gid, "name": name}


def _plan_routen(fake: FakeAsana) -> None:
    fake.route("POST", "/projects", {"gid": "900", "permalink_url": "https://app.asana.com/0/900"})
    fake.route("POST", "/projects/900/sections", {"gid": "910"})
    fake.route(
        "POST",
        "/tasks",
        {"gid": "920", "permalink_url": "https://app.asana.com/0/900/920"},
        {"gid": "921", "permalink_url": "https://app.asana.com/0/900/921"},
    )
    fake.route(
        "GET", "/sections/910", {"gid": "910", "name": "Planung", "project": _projekt("900")}
    )


@pytest.fixture
def akontext(kontext, fake):
    return asana_kontext(kontext, fake)


@pytest.fixture
def aregistry(akontext):
    return lade_registry(akontext)


@pytest.fixture
def tool(aregistry) -> AsanaAenderungenAusfuehren:
    return aregistry.hole(NAME)


@pytest.fixture
def afreigaben(akontext, aregistry) -> Freigaben:
    return Freigaben(akontext, aregistry)


async def _anfrage(afreigaben, tool, user, operationen) -> FreigabeAnfrage:
    return await afreigaben.anfragen(user, tool, {"operationen": operationen})


async def _freigeben(afreigaben, tool, user, operationen):
    anfrage = await _anfrage(afreigaben, tool, user, operationen)
    return await afreigaben.entscheiden(anfrage.approval_id, user.telegram_id, genehmigt=True)


async def _audit(session_fabrik) -> list[AuditLog]:
    async with session_fabrik() as session:
        return list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))


async def _op_status(session_fabrik) -> list[tuple]:
    async with session_fabrik() as session:
        zeilen = await session.scalars(select(AsanaOperation).order_by(AsanaOperation.position))
        return [(z.position, z.art, z.status, z.gid) for z in zeilen]


def test_registry_findet_das_schreib_tool(tool):
    assert tool.schreibend
    assert tool.ergebnis_im_verlauf


# ---------------------------------------------------------------- Vorschau


async def test_vorschau_eines_plans_mit_platzhaltern(tool, fake):
    text = await tool.bereite_vor(operationen=PLAN)
    assert text.splitlines() == [
        "Asana-Änderungssatz: 4 anlegen",
        "1. Anlegen: Projekt „Messe 2026“",
        "2. Anlegen: Abschnitt „Planung“ in Projekt „Messe 2026“ (neu)",
        "3. Anlegen: Aufgabe „Stand buchen“ in „Messe 2026“ (neu) / „Planung“ (neu), "
        "fällig Mo 12.10.2026, zuständig Max",
        "4. Anlegen: Unteraufgabe „Angebot einholen“ unter „Stand buchen“ (neu), "
        "fällig Do 15.10.2026 14:30, ohne Zuständigen",
    ]
    assert fake.aufrufe() == [("GET", "/users/501")]


async def test_vorschau_zeigt_vorher_und_nachher(tool, fake):
    text = await tool.bereite_vor(
        operationen=[
            {"operation": "aufgabe_aendern", "gid": "7", "faellig": "2026-10-15"},
            {"operation": "aufgabe_aendern", "gid": "7", "zustaendig_gid": None, "name": "Neu"},
            {"operation": "aufgabe_verschieben", "gid": "7", "abschnitt": "202"},
            {"operation": "aufgabe_erledigen", "gid": "7"},
            {"operation": "kommentar_hinzufuegen", "aufgabe_gid": "7", "text": "Bitte prüfen"},
        ]
    )
    assert text.splitlines() == [
        "Asana-Änderungssatz: 3 ändern, 1 verschieben, 1 kommentieren",
        "1. Ändern: „Etiketten“: Fällig Mo 12.10.2026 → Do 15.10.2026",
        "2. Ändern: „Etiketten“: Name → „Neu“; Zuständig Max → (leer)",
        "3. Verschieben: „Etiketten“: Abschnitt „Offen“ → „In Arbeit“",
        "4. Erledigen: „Etiketten“",
        "5. Kommentar zu „Etiketten“: „Bitte prüfen“",
    ]
    # Die Vorschau liest den aktuellen Zustand und schreibt nichts.
    assert ("GET", "/tasks/7") in fake.aufrufe()
    assert {methode for methode, _ in fake.aufrufe()} == {"GET"}


@pytest.mark.parametrize(
    ("operationen", "meldung"),
    [
        ([], "keine Operationen"),
        ([{"operation": "alles_loeschen"}], "Operation 1: Unbekannte Operation"),
        ([{"operation": "aufgabe_anlegen"}], "Das Feld „name“ fehlt"),
        ([{"operation": "aufgabe_anlegen", "name": "x", "prio": "hoch"}], "prio"),
        ([{"operation": "aufgabe_anlegen", "name": "x", "projekt": "$p9"}], r"Platzhalter \$p9"),
        (
            [
                {"operation": "tag_anlegen", "name": "t", "platzhalter": "$t1"},
                {"operation": "aufgabe_anlegen", "name": "x", "projekt": "$t1"},
            ],
            "Operation 2: Der Platzhalter .* steht für einen Tag",
        ),
        (
            [
                {"operation": "tag_anlegen", "name": "t", "platzhalter": "$t1"},
                {"operation": "tag_anlegen", "name": "u", "platzhalter": "$t1"},
            ],
            "doppelt vergeben",
        ),
        ([{"operation": "aufgabe_anlegen", "name": "x", "faellig": "Freitag"}], "JJJJ-MM-TT"),
        ([{"operation": "aufgabe_anlegen", "name": "x", "faellig_um": "2026-10-15"}], "Uhrzeit"),
        (
            [
                {
                    "operation": "aufgabe_anlegen",
                    "name": "x",
                    "faellig": "2026-10-15",
                    "faellig_um": "2026-10-15T10:00",
                }
            ],
            "nicht beide",
        ),
        ([{"operation": "aufgabe_anlegen", "name": "x", "startdatum": "2026-10-01"}], "Startdatum"),
        ([{"operation": "aufgabe_aendern", "gid": "Etiketten"}], "gültige Asana-GID"),
        ([{"operation": "aufgabe_aendern", "gid": "7"}], "mindestens ein"),
        ([{"operation": "aufgabe_erledigen", "gid": "7", "erledigt": "ja"}], "true oder false"),
        ([{"operation": "projekt_anlegen", "name": "x", "farbe": "pink"}], "Unbekannte Farbe"),
    ],
)
async def test_ungueltige_saetze_werden_mit_klarer_meldung_abgelehnt(
    tool, fake, operationen, meldung
):
    with pytest.raises(ToolFehler, match=meldung):
        await tool.bereite_vor(operationen=operationen)
    assert fake.aufrufe("POST") == []


async def test_satz_ueber_dem_limit_wird_abgelehnt(kontext, fake, user):
    akontext = asana_kontext(kontext, fake, asana_max_ops_per_changeset=2)
    tool = AsanaAenderungenAusfuehren(akontext)
    drei = [{"operation": "tag_anlegen", "name": f"t{n}"} for n in range(3)]
    with pytest.raises(ToolFehler, match="höchstens 2.*aufteilen"):
        await tool.bereite_vor(operationen=drei)
    assert "2 anlegen" in await tool.bereite_vor(operationen=drei[:2])
    marke = aktuelle_freigabe.set(1)
    try:
        with pytest.raises(ToolFehler, match="höchstens 2"):
            await tool.ausfuehren(operationen=drei)
    finally:
        aktuelle_freigabe.reset(marke)
    assert fake.anfragen == []


async def test_verschieben_aus_projekt_nur_wenn_eindeutig(tool, fake):
    fake.route("GET", "/projects/300", _projekt("300", "Archiv"))
    op = {"operation": "aufgabe_verschieben", "gid": "7", "projekt": "300"}
    assert (await tool.bereite_vor(operationen=[op])).splitlines()[1] == (
        "1. Verschieben: „Etiketten“: zusätzlich in Projekt „Archiv“"
    )
    op["aus_projekt_entfernen"] = True
    assert (await tool.bereite_vor(operationen=[op])).splitlines()[1] == (
        "1. Verschieben: „Etiketten“: Projekt „Launch“ → „Archiv“"
    )
    fake.route(
        "GET",
        "/tasks/7",
        aufgabe(
            memberships=[
                {"project": _projekt("100"), "section": None},
                {"project": _projekt("101", "Messe"), "section": None},
            ]
        ),
    )
    with pytest.raises(ToolFehler, match="mehreren Projekten.*„Launch“, „Messe“"):
        await tool.bereite_vor(operationen=[op])


# ---------------------------------------------------------------- Freigabe und Ausführung


async def test_ohne_freigabe_wird_nichts_ausgefuehrt(
    akontext, aregistry, afreigaben, tool, kosten, session_fabrik, user, fake
):
    _plan_routen(fake)
    client = FakeAnthropic(
        claude_antwort(tool_use_block(NAME, {"operationen": PLAN})),
        claude_antwort(text_block("Bitte gib den Plan frei.")),
    )
    agent = Agent(akontext.settings, session_fabrik, client, aregistry, afreigaben, kosten)
    nachricht = EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="p")

    antwort = await agent.beantworte(nachricht, user)

    (anfrage,) = antwort.freigaben
    assert anfrage.vorschau_text.startswith("Asana-Änderungssatz: 4 anlegen")
    assert not [a for a in fake.aufrufe() if a[0] in SCHREIBEND]
    async with session_fabrik() as session:
        assert (await session.get(Approval, anfrage.approval_id)).status == "offen"
    assert await _op_status(session_fabrik) == []
    # Auch ein direkter Aufruf ohne Freigabe schreibt nichts.
    marke = aktueller_nutzer.set(user)
    try:
        with pytest.raises(ToolFehler, match="nur nach einer Freigabe"):
            await tool.ausfuehren(operationen=PLAN)
    finally:
        aktueller_nutzer.reset(marke)
    assert not [a for a in fake.aufrufe() if a[0] in SCHREIBEND]


async def test_verwerfen_legt_nichts_an(afreigaben, tool, user, fake, session_fabrik):
    _plan_routen(fake)
    anfrage = await _anfrage(afreigaben, tool, user, PLAN)
    entscheidung = await afreigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=False)
    assert entscheidung.text == "❌ Verworfen: Asana-Änderungssatz: 4 anlegen"
    assert entscheidung.im_verlauf
    assert not [a for a in fake.aufrufe() if a[0] in SCHREIBEND]
    assert await _op_status(session_fabrik) == []


async def test_freigabe_fuehrt_aus_und_ersetzt_platzhalter(
    afreigaben, tool, user, fake, session_fabrik
):
    _plan_routen(fake)
    entscheidung = await _freigeben(afreigaben, tool, user, PLAN)

    assert entscheidung.text == (
        "✅ Asana-Änderungssatz ausgeführt: 4 angelegt.\nhttps://app.asana.com/0/900"
    )
    assert entscheidung.im_verlauf
    assert fake.aufrufe("POST") == [
        ("POST", "/projects"),
        ("POST", "/projects/900/sections"),
        ("POST", "/tasks"),
        ("POST", "/tasks"),
    ]
    assert fake.koerper("POST", "/projects") == [{"workspace": "ws1", "name": "Messe 2026"}]
    erste, zweite = fake.koerper("POST", "/tasks")
    assert erste == {
        "name": "Stand buchen",
        "due_on": "2026-10-12",
        "assignee": "501",
        "memberships": [{"project": "900", "section": "910"}],
    }
    # 14:30 Uhr in Berlin (Sommerzeit) sind 12:30 Uhr UTC.
    assert zweite == {
        "name": "Angebot einholen",
        "due_at": "2026-10-15T12:30:00Z",
        "parent": "920",
    }
    assert await _op_status(session_fabrik) == [
        (1, "projekt_anlegen", "erledigt", "900"),
        (2, "abschnitt_anlegen", "erledigt", "910"),
        (3, "aufgabe_anlegen", "erledigt", "920"),
        (4, "aufgabe_anlegen", "erledigt", "921"),
    ]


async def test_aendern_erledigen_verschieben_kommentieren(afreigaben, tool, user, fake):
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    fake.route("POST", "/tasks/7/addProject", {})
    fake.route("POST", "/tasks/7/stories", {"gid": "s"})
    entscheidung = await _freigeben(
        afreigaben,
        tool,
        user,
        [
            {"operation": "aufgabe_aendern", "gid": "7", "faellig": "", "zustaendig_gid": None},
            {"operation": "aufgabe_erledigen", "gid": "7"},
            {"operation": "aufgabe_verschieben", "gid": "7", "abschnitt": "202"},
            {"operation": "kommentar_hinzufuegen", "aufgabe_gid": "7", "text": "Erledigt"},
        ],
    )
    assert entscheidung.text.startswith(
        "✅ Asana-Änderungssatz ausgeführt: 2 geändert, 1 verschoben, 1 kommentiert."
    )
    assert fake.koerper("PUT", "/tasks/7") == [
        {"due_on": None, "assignee": None},
        {"completed": True},
    ]
    assert fake.koerper("POST", "/tasks/7/addProject") == [{"project": "100", "section": "202"}]
    assert fake.koerper("POST", "/tasks/7/stories") == [{"text": "Erledigt"}]


async def test_weitere_operationen(afreigaben, tool, user, fake):
    fake.route("GET", "/tags/41", {"gid": "41", "name": "dringend"})
    fake.route("GET", "/tasks/8", aufgabe("8", "Budget"))
    fake.route("PUT", "/projects/100", {"gid": "100"})
    fake.route("PUT", "/sections/201", {"gid": "201"})
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    fake.route("POST", "/tags", {"gid": "42"})
    for pfad in ("addTag", "removeTag", "addDependencies", "setParent", "addFollowers"):
        fake.route("POST", f"/tasks/7/{pfad}", {})
    operationen = [
        {"operation": "projekt_aendern", "gid": "100", "name": "Launch 2", "farbe": "dark-green"},
        {"operation": "projekt_archivieren", "gid": "100"},
        {"operation": "abschnitt_umbenennen", "gid": "201", "name": "Backlog"},
        {"operation": "tag_anlegen", "name": "Messe", "platzhalter": "$t1"},
        {"operation": "tag_zuweisen", "aufgabe_gid": "7", "tag_gid": "$t1"},
        {"operation": "tag_zuweisen", "aufgabe_gid": "7", "tag_gid": "41", "entfernen": True},
        {"operation": "abhaengigkeit_setzen", "aufgabe_gid": "7", "haengt_ab_von_gid": "8"},
        {
            "operation": "aufgabe_aendern",
            "gid": "7",
            "meilenstein": True,
            "uebergeordnet": "8",
            "tags": ["41"],
            "follower": ["501"],
        },
    ]
    vorschau = (await tool.bereite_vor(operationen=operationen)).splitlines()
    assert vorschau[0] == "Asana-Änderungssatz: 1 anlegen, 7 ändern"
    assert (
        vorschau[1]
        == "1. Ändern: Projekt „Launch“: Name Launch → Launch 2; Farbe (leer) → dark-green"
    )
    assert vorschau[2] == "2. Archivieren: Projekt „Launch“"
    assert vorschau[3] == "3. Umbenennen: Abschnitt „Offen“ → „Backlog“ (Projekt „Launch“)"
    assert vorschau[5] == "5. Tag „Messe“ (neu) zuweisen: „Etiketten“"
    assert vorschau[6] == "6. Tag „dringend“ entfernen: „Etiketten“"
    assert vorschau[7] == "7. Abhängigkeit: „Etiketten“ wartet auf „Budget“"
    assert vorschau[8] == (
        "8. Ändern: „Etiketten“: wird Meilenstein; wird Unteraufgabe von „Budget“; "
        "+ Tag „dringend“; + Follower Max"
    )

    entscheidung = await _freigeben(afreigaben, tool, user, operationen)
    assert entscheidung.text.startswith("✅")
    assert fake.koerper("PUT", "/projects/100") == [
        {"name": "Launch 2", "color": "dark-green"},
        {"archived": True},
    ]
    assert fake.koerper("PUT", "/sections/201") == [{"name": "Backlog"}]
    assert fake.koerper("POST", "/tasks/7/addTag") == [{"tag": "42"}, {"tag": "41"}]
    assert fake.koerper("POST", "/tasks/7/removeTag") == [{"tag": "41"}]
    assert fake.koerper("POST", "/tasks/7/addDependencies") == [{"dependencies": ["8"]}]
    assert fake.koerper("PUT", "/tasks/7") == [{"resource_subtype": "milestone"}]
    assert fake.koerper("POST", "/tasks/7/setParent") == [{"parent": "8"}]
    assert fake.koerper("POST", "/tasks/7/addFollowers") == [{"followers": ["501"]}]


async def test_verschieben_entfernt_aus_dem_einzigen_anderen_projekt(afreigaben, tool, user, fake):
    fake.route("GET", "/projects/300", _projekt("300", "Archiv"))
    fake.route("POST", "/tasks/7/addProject", {})
    fake.route("POST", "/tasks/7/removeProject", {})
    await _freigeben(
        afreigaben,
        tool,
        user,
        [
            {
                "operation": "aufgabe_verschieben",
                "gid": "7",
                "projekt": "300",
                "aus_projekt_entfernen": True,
            }
        ],
    )
    assert fake.koerper("POST", "/tasks/7/addProject") == [{"project": "300"}]
    assert fake.koerper("POST", "/tasks/7/removeProject") == [{"project": "100"}]


async def test_teilweiser_fehler_meldet_erledigtes_und_rollt_nichts_zurueck(
    afreigaben, tool, user, fake, session_fabrik
):
    _plan_routen(fake)
    fake.route(
        "POST",
        "/projects/900/sections",
        httpx.Response(400, json={"errors": [{"message": "name: Too long"}]}),
    )
    entscheidung = await _freigeben(afreigaben, tool, user, PLAN)

    assert entscheidung.text.splitlines() == [
        "⚠️ Asana-Änderungssatz abgebrochen bei Operation 2 von 4.",
        "Erledigt (1):",
        "1. Projekt „Messe 2026“ angelegt",
        "Fehlgeschlagen:",
        "2. abschnitt_anlegen „Planung“ – Asana lehnt die Anfrage ab: name: Too long",
        "Nicht mehr ausgeführt: Operation 3–4 (2).",
        "Es wurde nichts wiederholt und nichts zurückgerollt.",
        "https://app.asana.com/0/900",
    ]
    # Genau ein Versuch je Operation, nichts danach, kein Löschen des angelegten Projekts.
    assert [a for a in fake.aufrufe() if a[0] in SCHREIBEND] == [
        ("POST", "/projects"),
        ("POST", "/projects/900/sections"),
    ]
    assert await _op_status(session_fabrik) == [
        (1, "projekt_anlegen", "erledigt", "900"),
        (2, "abschnitt_anlegen", "fehlgeschlagen", None),
        (3, "aufgabe_anlegen", "nicht ausgeführt", None),
        (4, "aufgabe_anlegen", "nicht ausgeführt", None),
    ]


async def test_netzwerkfehler_beim_schreiben_wird_nicht_wiederholt(afreigaben, tool, user, fake):
    fake.route("PUT", "/tasks/7", httpx.ReadTimeout("zu langsam"))
    entscheidung = await _freigeben(
        afreigaben, tool, user, [{"operation": "aufgabe_erledigen", "gid": "7"}]
    )
    assert "Ob die Änderung angekommen ist, ist unklar" in entscheidung.text
    assert fake.aufrufe("PUT") == [("PUT", "/tasks/7")]


async def test_doppelter_klick_fuehrt_nichts_doppelt_aus(
    afreigaben, tool, user, fake, session_fabrik
):
    _plan_routen(fake)
    anfrage = await _anfrage(afreigaben, tool, user, PLAN)
    await afreigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    geschrieben = len(fake.aufrufe("POST"))

    zweite = await afreigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert zweite.status == STATUS_BEREITS_ENTSCHIEDEN

    # Selbst wenn die Ausführung derselben Freigabe noch einmal angestoßen würde:
    marke_nutzer = aktueller_nutzer.set(user)
    marke = aktuelle_freigabe.set(anfrage.approval_id)
    try:
        with pytest.raises(ToolFehler, match="bereits ausgeführt"):
            await tool.ausfuehren(operationen=PLAN)
    finally:
        aktuelle_freigabe.reset(marke)
        aktueller_nutzer.reset(marke_nutzer)
    assert len(fake.aufrufe("POST")) == geschrieben == 4


async def test_audit_log_enthaelt_vorher_werte_aber_keinen_token(
    afreigaben, tool, user, fake, session_fabrik
):
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    fake.route("GET", "/tasks/7", aufgabe(notes="Langer Text " * 50))
    await _freigeben(
        afreigaben,
        tool,
        user,
        [
            {
                "operation": "aufgabe_aendern",
                "gid": "7",
                "faellig": "2026-10-15",
                "zustaendig_gid": None,
                "beschreibung": "Neue Beschreibung",
            }
        ],
    )
    eintraege = await _audit(session_fabrik)
    detail = next(e for e in eintraege if "freigabe" in e.parameter)
    assert detail.user_id == user.id
    assert detail.ergebnis_kurz == "Änderungssatz: 1 von 1 Operationen erledigt"
    (op,) = detail.parameter["operationen"]
    assert op["operation"] == "aufgabe_aendern"
    assert op["gid"] == "7"
    assert op["felder"] == ["beschreibung", "faellig", "zustaendig_gid"]
    assert op["ergebnis"] == "erledigt"
    assert op["vorher"]["due_on"] == "2026-10-12"
    assert op["vorher"]["zustaendig"] == "Max"
    # Aufgabentexte werden nicht vollständig gespeichert.
    assert len(op["vorher"]["beschreibung"]) <= 100
    for eintrag in eintraege:
        assert ASANA_TOKEN not in str(eintrag.parameter) + eintrag.ergebnis_kurz


# ---------------------------------------------------------------- Telegram


def test_teile_text_trennt_an_zeilenenden():
    zeilen = [f"{n}. " + "x" * 95 for n in range(100)]
    teile = teile_text("\n".join(zeilen))
    assert len(teile) > 1
    assert all(len(teil) <= TELEGRAM_MAX_ZEICHEN for teil in teile)
    assert "\n".join(teile) == "\n".join(zeilen)
    assert teile_text("") == []
    assert teile_text("kurz") == ["kurz"]


@pytest.fixture
def akanal(akontext, session_fabrik, afreigaben, kosten, alarme, monkeypatch):
    kanal = TelegramKanal(
        akontext.settings,
        session_fabrik,
        handler=None,
        freigaben=afreigaben,
        kosten=kosten,
        alarme=alarme,
    )
    kanal.gesendet = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        kanal.gesendet.append((text, reply_markup))

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    return kanal


async def test_lange_vorschau_geht_ueber_mehrere_nachrichten_mit_buttons_am_ende(akanal):
    vorschau = "\n".join(f"{n}. Anlegen: Aufgabe „{'x' * 80}“" for n in range(1, 101))
    await akanal.sende_freigabe_anfrage(5, FreigabeAnfrage(3, vorschau, anzahl=100))

    assert len(akanal.gesendet) > 2
    assert akanal.gesendet[-1][0] == "Freigabe für 100 Änderungen, gültig 15 Minuten"
    assert all(len(text) <= TELEGRAM_MAX_ZEICHEN for text, _ in akanal.gesendet)
    assert all(buttons is None for _, buttons in akanal.gesendet[:-1])
    buttons = akanal.gesendet[-1][1]
    assert [b.callback_data for b in buttons.inline_keyboard[0]] == [
        "freigabe:3:ja",
        "freigabe:3:nein",
    ]


async def test_ergebnis_landet_nach_dem_klick_im_verlauf(
    akanal, afreigaben, tool, user, fake, session_fabrik
):
    from tests.test_telegram import FakeQuery, _update

    _plan_routen(fake)
    anfrage = await _anfrage(afreigaben, tool, user, PLAN)
    query = FakeQuery(f"freigabe:{anfrage.approval_id}:ja")
    await akanal._bei_klick(_update(ERLAUBT_ID, query=query), None)

    assert akanal.gesendet[-1][0].startswith("✅ Asana-Änderungssatz ausgeführt: 4 angelegt.")
    async with session_fabrik() as session:
        from app.db.models import Message

        (nachricht,) = list(await session.scalars(select(Message)))
    assert nachricht.rolle == "assistant"
    assert nachricht.inhalt.startswith("[Ergebnis der Freigabe]\n✅ Asana-Änderungssatz")
    # Ohne vorherige Nutzer-Nachricht bleibt der Verlauf für die API trotzdem gültig.
    assert await lade_verlauf(session_fabrik, user, 5, 20) == []


# ---------------------------------------------------------------- Ergänzungen


async def test_aufgabe_in_bestehendem_abschnitt_findet_das_projekt_selbst(
    afreigaben, tool, user, fake
):
    fake.route("POST", "/tasks", {"gid": "930", "permalink_url": "https://app.asana.com/0/1/930"})
    op = {"operation": "aufgabe_anlegen", "name": "Muster", "abschnitt": "202", "meilenstein": True}
    vorschau = await tool.bereite_vor(operationen=[op])
    assert vorschau.splitlines()[1] == (
        "1. Anlegen: Meilenstein „Muster“ in „Launch“ / „In Arbeit“, ohne Zuständigen"
    )
    await _freigeben(afreigaben, tool, user, [op])
    assert fake.koerper("POST", "/tasks") == [
        {
            "name": "Muster",
            "resource_subtype": "milestone",
            "memberships": [{"project": "100", "section": "202"}],
        }
    ]


async def test_aufgabe_ohne_projekt_landet_im_workspace(afreigaben, tool, user, fake):
    fake.route("POST", "/tasks", {"gid": "931"})
    op = {"operation": "aufgabe_anlegen", "name": "Lose Aufgabe", "zustaendig_gid": "me"}
    fake.route("GET", "/users/me", {"gid": "501", "name": "Theis"})
    assert "ohne Projekt, zuständig Theis (ich)" in await tool.bereite_vor(operationen=[op])
    await _freigeben(afreigaben, tool, user, [op])
    assert fake.koerper("POST", "/tasks") == [
        {"name": "Lose Aufgabe", "assignee": "me", "workspace": "ws1"}
    ]


async def test_projekt_nutzt_das_standard_team(kontext, fake, user):
    akontext = asana_kontext(kontext, fake, asana_default_team_gid="77")
    registry = lade_registry(akontext)
    tool, freigaben = registry.hole(NAME), Freigaben(akontext, registry)
    fake.route("GET", "/teams/77", {"gid": "77", "name": "Marketing"})
    fake.route("GET", "/teams/78", {"gid": "78", "name": "Vertrieb"})
    fake.route("POST", "/projects", {"gid": "900"})
    standard = {"operation": "projekt_anlegen", "name": "A", "faellig": "2026-11-02"}
    eigenes = {"operation": "projekt_anlegen", "name": "B", "team_gid": "78"}

    vorschau = await tool.bereite_vor(operationen=[standard, eigenes])
    assert "1. Anlegen: Projekt „A“ (Team „Marketing“, fällig Mo 02.11.2026)" in vorschau
    assert "2. Anlegen: Projekt „B“ (Team „Vertrieb“)" in vorschau
    await _freigeben(freigaben, tool, user, [standard, eigenes])
    assert [k["team"] for k in fake.koerper("POST", "/projects")] == ["77", "78"]


async def test_nutzer_gids_koennen_keinen_pfad_einschleusen(tool, fake):
    for feld, wert in (
        ("follower", ["../tasks/7"]),
        ("zustaendig_gid", "501/../../tasks"),
        ("tags", ["41?x=1"]),
    ):
        with pytest.raises(ToolFehler, match="gültige Asana-GID"):
            await tool.bereite_vor(
                operationen=[{"operation": "aufgabe_anlegen", "name": "x", feld: wert}]
            )
    assert fake.anfragen == []


async def test_token_steht_auch_bei_fehlern_in_keinem_log_und_keiner_meldung(
    afreigaben, tool, user, fake, session_fabrik, caplog
):
    import logging

    fake.route("PUT", "/tasks/7", httpx.ConnectError(f"Bearer {ASANA_TOKEN}"))
    fake.route(
        "POST",
        "/tasks/7/stories",
        httpx.Response(401, json={"errors": [{"message": f"Token {ASANA_TOKEN} ungültig"}]}),
    )
    with caplog.at_level(logging.DEBUG):
        erste = await _freigeben(
            afreigaben, tool, user, [{"operation": "aufgabe_erledigen", "gid": "7"}]
        )
        zweite = await _freigeben(
            afreigaben,
            tool,
            user,
            [{"operation": "kommentar_hinzufuegen", "aufgabe_gid": "7", "text": "x"}],
        )
    assert "abgebrochen" in erste.text
    assert "Asana-Zugriff verweigert" in zweite.text
    assert ASANA_TOKEN not in erste.text + zweite.text + caplog.text
    for eintrag in await _audit(session_fabrik):
        assert ASANA_TOKEN not in f"{eintrag.parameter}{eintrag.ergebnis_kurz}{eintrag.fehler}"


async def test_ergebnis_kommt_an_auch_wenn_telegram_den_klick_nicht_mehr_quittiert(
    akanal, afreigaben, tool, user, fake
):
    from telegram.error import BadRequest

    from tests.test_telegram import FakeQuery, _update

    class AlterKlick(FakeQuery):
        async def answer(self, text=None, show_alert=False):
            raise BadRequest("Query is too old and response timeout expired")

    _plan_routen(fake)
    anfrage = await _anfrage(afreigaben, tool, user, PLAN)
    query = AlterKlick(f"freigabe:{anfrage.approval_id}:ja")
    await akanal._bei_klick(_update(ERLAUBT_ID, query=query), None)
    assert akanal.gesendet[-1][0].startswith("✅ Asana-Änderungssatz ausgeführt: 4 angelegt.")
