"""Benutzerdefinierte Felder: Operationen, Namensauflösung, Wertformate je Typ."""

import pytest

from app.auth.approvals import Freigaben
from app.tools.base import ToolFehler
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext
from tests.test_asana_schreiben import aufgabe

NAME = "asana_aenderungen_ausfuehren"

PRIO = {
    "gid": "11",
    "name": "Priorität",
    "resource_subtype": "enum",
    "display_value": "Niedrig",
    "enum_options": [
        {"gid": "111", "name": "Hoch", "enabled": True},
        {"gid": "112", "name": "Niedrig", "enabled": True},
        {"gid": "113", "name": "Uralt", "enabled": False},
    ],
}
KANAELE = {
    "gid": "12",
    "name": "Kanäle",
    "resource_subtype": "multi_enum",
    "enum_options": [{"gid": "121", "name": "Shop"}, {"gid": "122", "name": "Amazon"}],
}
BUDGET = {"gid": "13", "name": "Budget", "resource_subtype": "number", "display_value": "100"}
NOTIZ = {"gid": "14", "name": "Notiz", "resource_subtype": "text"}
TERMIN = {"gid": "15", "name": "Liefertermin", "resource_subtype": "date"}
TEAM = {"gid": "16", "name": "Beteiligte", "resource_subtype": "people"}
FORMEL = {"gid": "17", "name": "Summe", "resource_subtype": "reference"}
ALLE = [PRIO, KANAELE, BUDGET, NOTIZ, TERMIN, TEAM, FORMEL]
NUR_IM_WORKSPACE = {"gid": "20", "name": "Lieferant", "resource_subtype": "text"}


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/tasks/7", aufgabe(custom_fields=ALLE))
    fake.route("GET", "/projects/100", {"gid": "100", "name": "Launch"})
    fake.route(
        "GET",
        "/projects/100/custom_field_settings",
        [{"gid": "s", "custom_field": f} for f in ALLE],
    )
    fake.route("GET", "/workspaces/ws1/custom_fields", [*ALLE, NUR_IM_WORKSPACE])
    fake.route(
        "GET",
        "/users",
        [
            {"gid": "501", "name": "Max Muster", "email": "max@gruenschwert.de"},
            {"gid": "502", "name": "Lea Beispiel", "email": "lea@gruenschwert.de"},
            {"gid": "503", "name": "Max Zweit", "email": "max2@gruenschwert.de"},
        ],
    )
    fake.route("GET", "/users/501", {"gid": "501", "name": "Max Muster"})
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    fake.route("POST", "/tasks", {"gid": "930"})
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
        entscheidung = await freigaben.entscheiden(
            anfrage.approval_id, nutzer.telegram_id, True, bestaetigt=True
        )
    assert entscheidung.text.startswith("✅"), entscheidung.text
    return anfrage.vorschau_text.splitlines()


def _aendern(**felder) -> dict:
    return {"operation": "aufgabe_aendern", "gid": "7", "felder": felder}


# ---------------------------------------------------------------- Werte an Aufgaben


async def test_wertformate_je_typ_und_namensaufloesung(werkzeug, user, fake):
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            _aendern(
                **{
                    "priorität": "hoch",
                    "Kanäle": ["Shop", "122"],
                    "Budget": "1250,5",
                    "14": "Bitte prüfen",
                    "Liefertermin": "2026-10-15",
                    "Beteiligte": ["Lea Beispiel", "501"],
                }
            )
        ],
    )
    assert zeilen[1] == (
        "1. Ändern: „Etiketten“: Feld „Priorität“: Niedrig → Hoch; "
        "Feld „Kanäle“: (leer) → Shop, Amazon; Feld „Budget“: 100 → 1250,5; "
        "Feld „Notiz“: (leer) → Bitte prüfen; Feld „Liefertermin“: (leer) → 15.10.2026; "
        "Feld „Beteiligte“: (leer) → Lea Beispiel, Max Muster"
    )
    assert fake.koerper("PUT", "/tasks/7") == [
        {
            "custom_fields": {
                "11": "111",
                "12": ["121", "122"],
                "13": 1250.5,
                "14": "Bitte prüfen",
                "15": {"date": "2026-10-15"},
                "16": ["502", "501"],
            }
        }
    ]


async def test_ganze_zahl_bleibt_ganz_und_datum_mit_uhrzeit_geht_in_utc(werkzeug, user, fake):
    await _ausfuehren(werkzeug, user, [_aendern(Budget=3, Liefertermin="2026-01-15T14:30")])
    assert fake.koerper("PUT", "/tasks/7") == [
        {"custom_fields": {"13": 3, "15": {"date_time": "2026-01-15T13:30:00Z"}}}
    ]


async def test_leerer_wert_leert_das_feld(werkzeug, user, fake):
    zeilen = await _ausfuehren(werkzeug, user, [_aendern(Priorität=None, Kanäle=[])])
    assert "Feld „Priorität“: Niedrig → (leer)" in zeilen[1]
    assert fake.koerper("PUT", "/tasks/7") == [{"custom_fields": {"11": None, "12": None}}]


async def test_felder_zusammen_mit_anderen_aenderungen_und_vorher_im_audit(
    werkzeug, user, fake, session_fabrik
):
    from sqlalchemy import select

    from app.db.models import AuditLog

    await _ausfuehren(
        werkzeug,
        user,
        [{"operation": "aufgabe_aendern", "gid": "7", "name": "Neu", "felder": {"Budget": 5}}],
    )
    assert fake.koerper("PUT", "/tasks/7") == [{"name": "Neu", "custom_fields": {"13": 5}}]
    async with session_fabrik() as session:
        eintraege = list(await session.scalars(select(AuditLog)))
    detail = next(e for e in eintraege if "freigabe" in e.parameter)
    assert detail.parameter["operationen"][0]["vorher"]["felder"] == {"Budget": "100"}


@pytest.mark.parametrize(
    ("felder", "meldung"),
    [
        ({"Priorität": "Mittel"}, "keine Option „Mittel“. Vorhanden: Hoch, Niedrig"),
        ({"Priorität": "Uralt"}, "keine Option „Uralt“"),
        ({"Priorität": ["Hoch", "Niedrig"]}, "erlaubt nur eine Option"),
        ({"Budget": "viel"}, "erwartet eine Zahl"),
        ({"Budget": True}, "erwartet eine Zahl"),
        ({"Liefertermin": "nächste Woche"}, "erwartet ein Datum"),
        ({"Beteiligte": ["Max"]}, "nicht eindeutig: Max Muster \\(501\\), Max Zweit \\(503\\)"),
        ({"Beteiligte": ["Niemand"]}, "Einen Asana-Nutzer „Niemand“ gibt es nicht"),
        ({"Summe": "1"}, "hat den Typ reference"),
        ({"Gibtsnicht": "x"}, "Das Feld „Gibtsnicht“ gibt es nicht"),
        (
            {"Lieferant": "ACME"},
            "„Lieferant“ ist an dieser Aufgabe nicht eingerichtet.*feld_zu_projekt_hinzufuegen "
            "\\(feld_gid 20\\)",
        ),
    ],
)
async def test_ungueltige_feldwerte_fallen_in_der_vorschau_auf(
    werkzeug, user, fake, felder, meldung
):
    with pytest.raises(ToolFehler, match=meldung):
        await _vorschau(werkzeug, user, [_aendern(**felder)])
    assert fake.aufrufe("PUT") == []


async def test_mehrdeutiger_feldname_wird_gemeldet(werkzeug, user, fake):
    doppelt = {**NOTIZ, "gid": "99"}
    fake.route("GET", "/tasks/7", aufgabe(custom_fields=[NOTIZ, doppelt]))
    with pytest.raises(ToolFehler, match="„Notiz“ ist mehrdeutig: Notiz \\(14\\), Notiz \\(99\\)"):
        await _vorschau(werkzeug, user, [_aendern(Notiz="x")])
    # Über die GID bleibt es eindeutig.
    await _vorschau(werkzeug, user, [_aendern(**{"99": "x"})])


async def test_anlegen_prueft_die_felder_des_projekts(werkzeug, user, fake):
    anlegen = {
        "operation": "aufgabe_anlegen",
        "name": "Muster",
        "projekt": "100",
        "felder": {"Priorität": "Hoch"},
    }
    zeilen = await _ausfuehren(werkzeug, user, [anlegen])
    assert zeilen[1].endswith("ohne Zuständigen, Feld „Priorität“: Hoch")
    assert fake.koerper("POST", "/tasks") == [
        {"name": "Muster", "projects": ["100"], "custom_fields": {"11": "111"}}
    ]
    with pytest.raises(ToolFehler, match="im Projekt der Aufgabe nicht eingerichtet"):
        await _vorschau(werkzeug, user, [{**anlegen, "felder": {"Lieferant": "ACME"}}])


# ---------------------------------------------------------------- Felder verwalten


async def test_feld_anlegen_im_projekt_einrichten_und_gleich_benutzen(werkzeug, user, fake):
    fake.route("POST", "/custom_fields", {"gid": "40"})
    fake.route("POST", "/projects/900/addCustomFieldSetting", {})
    fake.route("POST", "/projects", {"gid": "900"})
    # So sieht Asana nach dem Anlegen aus: Die Optionen haben jetzt echte GIDs.
    status = {
        "gid": "40",
        "name": "Status",
        "resource_subtype": "enum",
        "enum_options": [{"gid": "401", "name": "Offen"}, {"gid": "402", "name": "Fertig"}],
    }
    fake.route("GET", "/custom_fields/40", status)
    fake.route("GET", "/projects/900", {"gid": "900", "name": "Messe"})
    fake.route("GET", "/projects/900/custom_field_settings", [{"gid": "s", "custom_field": status}])
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            {"operation": "projekt_anlegen", "name": "Messe", "platzhalter": "$p1"},
            {
                "operation": "feld_anlegen",
                "name": "Status",
                "typ": "enum",
                "optionen": ["Offen", "Fertig"],
                "platzhalter": "$f1",
            },
            {
                "operation": "feld_zu_projekt_hinzufuegen",
                "projekt": "$p1",
                "feld_gid": "$f1",
                "wichtig": True,
            },
            {
                "operation": "aufgabe_anlegen",
                "name": "Stand buchen",
                "projekt": "$p1",
                "felder": {"$f1": "Fertig"},
            },
        ],
    )
    assert zeilen[2] == "2. Anlegen: Feld „Status“ (Typ enum, Optionen: Offen, Fertig)"
    assert zeilen[3] == "3. Feld „Status“ (neu) im Projekt „Messe“ (neu) einrichten"
    assert zeilen[4].endswith("Feld „Status“: Fertig")
    assert fake.koerper("POST", "/custom_fields") == [
        {
            "workspace": "ws1",
            "name": "Status",
            "resource_subtype": "enum",
            "enum_options": [{"name": "Offen"}, {"name": "Fertig"}],
        }
    ]
    assert fake.koerper("POST", "/projects/900/addCustomFieldSetting") == [
        {"custom_field": "40", "is_important": True}
    ]
    assert fake.koerper("POST", "/tasks")[0]["custom_fields"] == {"40": "402"}


async def test_zahlenfeld_mit_nachkommastellen(werkzeug, user, fake):
    fake.route("POST", "/custom_fields", {"gid": "41"})
    await _ausfuehren(
        werkzeug,
        user,
        [{"operation": "feld_anlegen", "name": "Preis", "typ": "number", "nachkommastellen": 2}],
    )
    assert fake.koerper("POST", "/custom_fields")[0] == {
        "workspace": "ws1",
        "name": "Preis",
        "resource_subtype": "number",
        "precision": 2,
    }


@pytest.mark.parametrize(
    ("op", "meldung"),
    [
        ({"name": "x", "typ": "enum"}, "braucht mindestens eine Option"),
        ({"name": "x", "typ": "text", "optionen": ["a"]}, "nur bei den Typen enum"),
        ({"name": "x", "typ": "text", "nachkommastellen": 2}, "nur beim Typ number"),
        ({"name": "x", "typ": "number", "nachkommastellen": 9}, "von 0 bis 6"),
        ({"name": "x", "typ": "formel"}, "„formel“ ist in „typ“ nicht erlaubt"),
    ],
)
async def test_feld_anlegen_prueft_die_angaben(werkzeug, user, fake, op, meldung):
    with pytest.raises(ToolFehler, match=meldung):
        await _vorschau(werkzeug, user, [{"operation": "feld_anlegen", **op}])
    assert fake.aufrufe("POST") == []


async def test_feld_aendern_option_hinzufuegen_und_aus_projekt_entfernen(werkzeug, user, fake):
    fake.route("GET", "/custom_fields/11", {**PRIO, "precision": None})
    fake.route("GET", "/custom_fields/13", {**BUDGET, "precision": 0})
    fake.route("PUT", "/custom_fields/13", {"gid": "13"})
    fake.route("POST", "/custom_fields/11/enum_options", {"gid": "114", "name": "Mittel"})
    fake.route("POST", "/projects/100/removeCustomFieldSetting", {})
    zeilen = await _ausfuehren(
        werkzeug,
        user,
        [
            {"operation": "feld_aendern", "feld_gid": "13", "name": "Etat", "nachkommastellen": 2},
            {"operation": "feldoption_hinzufuegen", "feld_gid": "11", "name": "Mittel"},
            {"operation": "feld_aus_projekt_entfernen", "projekt": "100", "feld_gid": "11"},
        ],
    )
    assert zeilen[0] == "Asana-Änderungssatz: 3 ändern"
    assert zeilen[1] == "1. Ändern: Feld „Budget“: Name → „Etat“; Nachkommastellen 0 → 2"
    assert zeilen[2] == "2. Option „Mittel“ zum Feld „Priorität“ hinzufügen"
    assert zeilen[3].startswith("3. Feld „Priorität“ aus dem Projekt „Launch“ entfernen")
    assert fake.koerper("PUT", "/custom_fields/13") == [{"name": "Etat", "precision": 2}]
    assert fake.koerper("POST", "/custom_fields/11/enum_options") == [{"name": "Mittel"}]
    assert fake.koerper("POST", "/projects/100/removeCustomFieldSetting") == [
        {"custom_field": "11"}
    ]
    with pytest.raises(ToolFehler, match="ist kein Auswahlfeld"):
        await _vorschau(
            werkzeug, user, [{"operation": "feldoption_hinzufuegen", "feld_gid": "13", "name": "x"}]
        )


async def test_feld_loeschen_ist_eine_loeschung_mit_zweiter_bestaetigung(
    werkzeug, user, admin, fake
):
    fake.route("GET", "/custom_fields/14", NOTIZ)
    fake.route("DELETE", "/custom_fields/14", {})
    op = {"operation": "feld_loeschen", "feld_gid": "14"}
    with pytest.raises(ToolFehler, match="darf in Asana nichts löschen"):
        await _vorschau(werkzeug, user, [op])
    zeilen = await _ausfuehren(werkzeug, admin, [op], bestaetigt=True)
    assert zeilen == [
        "Asana-Änderungssatz: 1 🗑 löschen",
        "1. 🗑 Löschen: Feld „Notiz“ im ganzen Workspace samt allen Werten",
    ]
    assert fake.aufrufe("DELETE") == [("DELETE", "/custom_fields/14")]
