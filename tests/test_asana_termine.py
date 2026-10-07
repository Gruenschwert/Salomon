"""Start und Fälligkeit: nur Datum, nur Fälligkeit mit Uhrzeit, Zeitfenster, Zeitzonen."""

from zoneinfo import ZoneInfo

import pytest

from app.auth.approvals import Freigaben
from app.tools.asana_operationen import _asana_zeitpunkt, _termin_daten
from app.tools.base import ToolFehler
from app.tools.registry import lade_registry
from tests.asana_fake import FakeAsana, asana_kontext
from tests.test_asana_schreiben import aufgabe

NAME = "asana_aenderungen_ausfuehren"
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("POST", "/tasks", {"gid": "950"})
    fake.route("PUT", "/tasks/7", {"gid": "7"})
    return fake


@pytest.fixture
def werkzeug(kontext, fake):
    akontext = asana_kontext(kontext, fake)
    registry = lade_registry(akontext)
    return registry.hole(NAME), Freigaben(akontext, registry)


async def _ausfuehren(werkzeug, user, operationen) -> str:
    tool, freigaben = werkzeug
    anfrage = await freigaben.anfragen(user, tool, {"operationen": operationen})
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, user.telegram_id, True)
    assert entscheidung.text.startswith("✅"), entscheidung.text
    return anfrage.vorschau_text.splitlines()[1]


def _anlegen(**felder) -> dict:
    return {"operation": "aufgabe_anlegen", "name": "Termin", **felder}


def _aendern(**felder) -> dict:
    return {"operation": "aufgabe_aendern", "gid": "7", **felder}


# ---------------------------------------------------------------- Zeitzonen


@pytest.mark.parametrize(
    ("ortszeit", "utc"),
    [
        # Sommerzeit: Berlin ist UTC+2
        ("2026-07-15T14:30", "2026-07-15T12:30:00Z"),
        # Winterzeit: Berlin ist UTC+1
        ("2026-01-15T14:30", "2026-01-15T13:30:00Z"),
        # Nach Mitternacht fällt der UTC-Zeitpunkt auf den Vortag
        ("2026-07-15T00:30", "2026-07-14T22:30:00Z"),
        ("2026-01-01T00:30", "2025-12-31T23:30:00Z"),
        # Tage der Umstellung: 29.03.2026 ab 3 Uhr Sommerzeit, 25.10.2026 ab 3 Uhr Winterzeit
        ("2026-03-29T01:30", "2026-03-29T00:30:00Z"),
        ("2026-03-29T03:30", "2026-03-29T01:30:00Z"),
        ("2026-10-25T01:30", "2026-10-24T23:30:00Z"),
        ("2026-10-25T03:30", "2026-10-25T02:30:00Z"),
        # Eine ausdrücklich angegebene Zeitzone wird respektiert
        ("2026-07-15T14:30+00:00", "2026-07-15T14:30:00Z"),
        ("2026-07-15T12:30:00Z", "2026-07-15T12:30:00Z"),
    ],
)
def test_ortszeit_wird_nach_utc_umgerechnet(ortszeit, utc):
    assert _asana_zeitpunkt(ortszeit, BERLIN) == utc


# ---------------------------------------------------------------- Anlegen


async def test_nur_datum(werkzeug, user, fake):
    zeile = await _ausfuehren(werkzeug, user, [_anlegen(faellig="2026-10-15")])
    assert "fällig Do 15.10.2026," in zeile
    assert fake.koerper("POST", "/tasks") == [
        {"name": "Termin", "due_on": "2026-10-15", "workspace": "ws1"}
    ]


async def test_ganze_tage_von_bis(werkzeug, user, fake):
    zeile = await _ausfuehren(
        werkzeug, user, [_anlegen(startdatum="2026-10-12", faellig="2026-10-15")]
    )
    assert "Start Mo 12.10.2026, fällig Do 15.10.2026" in zeile
    (koerper,) = fake.koerper("POST", "/tasks")
    assert (koerper["start_on"], koerper["due_on"]) == ("2026-10-12", "2026-10-15")
    assert "start_at" not in koerper and "due_at" not in koerper


async def test_nur_faelligkeit_mit_uhrzeit_sendet_nur_due_at(werkzeug, user, fake):
    zeile = await _ausfuehren(werkzeug, user, [_anlegen(faellig_um="2026-01-15T14:30")])
    assert "fällig Do 15.01.2026 14:30" in zeile
    (koerper,) = fake.koerper("POST", "/tasks")
    assert koerper == {"name": "Termin", "due_at": "2026-01-15T13:30:00Z", "workspace": "ws1"}


@pytest.mark.parametrize(
    ("start", "ende", "start_utc", "ende_utc"),
    [
        ("2026-07-15T10:00", "2026-07-15T12:00", "08:00", "10:00"),
        ("2026-01-15T10:00", "2026-01-15T12:00", "09:00", "11:00"),
    ],
)
async def test_zeitfenster_sendet_start_at_und_due_at(
    werkzeug, user, fake, start, ende, start_utc, ende_utc
):
    zeile = await _ausfuehren(werkzeug, user, [_anlegen(startzeit=start, faellig_um=ende)])
    tag = start[:10]
    tag_text = f"{tag[8:]}.{tag[5:7]}.2026"
    assert f"{tag_text} 10:00, fällig" in zeile
    assert f"{tag_text} 12:00" in zeile
    (koerper,) = fake.koerper("POST", "/tasks")
    assert koerper["start_at"] == f"{tag}T{start_utc}:00Z"
    assert koerper["due_at"] == f"{tag}T{ende_utc}:00Z"
    # Genau die Kombination, die Asana ablehnt, darf nie entstehen.
    assert "start_on" not in koerper and "due_on" not in koerper


@pytest.mark.parametrize(
    ("felder", "meldung"),
    [
        # Der gemeldete Fehler: Startdatum zusammen mit Fälligkeitsuhrzeit
        (
            {"startdatum": "2026-10-15", "faellig_um": "2026-10-15T12:00"},
            "beide mit oder beide ohne",
        ),
        ({"startzeit": "2026-10-15T10:00", "faellig": "2026-10-15"}, "beide mit oder beide ohne"),
        ({"startzeit": "2026-10-15T10:00"}, "braucht in Asana auch eine Fälligkeit"),
        ({"startdatum": "2026-10-15"}, "braucht in Asana auch eine Fälligkeit"),
        ({"startzeit": "2026-10-15T12:00", "faellig_um": "2026-10-15T10:00"}, "vor der Fälligkeit"),
        ({"startzeit": "2026-10-15T10:00", "faellig_um": "2026-10-15T10:00"}, "vor der Fälligkeit"),
        ({"startdatum": "2026-10-16", "faellig": "2026-10-15"}, "vor der Fälligkeit"),
        (
            {"startdatum": "2026-10-15", "startzeit": "2026-10-15T10:00", "faellig": "2026-10-16"},
            "nur „startdatum“ oder „startzeit“",
        ),
        (
            {"startzeit": "2026-10-15", "faellig_um": "2026-10-15T12:00"},
            "„startzeit“ muss Datum und",
        ),
    ],
)
async def test_ungueltige_kombination_faellt_schon_in_der_vorschau_auf(
    werkzeug, fake, felder, meldung
):
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match=f"Operation 1: .*{meldung}"):
        await tool.bereite_vor(operationen=[_anlegen(**felder)])
    assert fake.aufrufe("POST") == []


async def test_gleicher_tag_von_bis_ohne_uhrzeit_ist_erlaubt(werkzeug, user, fake):
    await _ausfuehren(werkzeug, user, [_anlegen(startdatum="2026-10-15", faellig="2026-10-15")])


# ---------------------------------------------------------------- Ändern


FENSTER = {
    "start_on": "2026-07-15",
    "start_at": "2026-07-15T08:00:00Z",
    "due_on": "2026-07-15",
    "due_at": "2026-07-15T10:00:00Z",
}
TAGE = {"start_on": "2026-10-12", "start_at": None, "due_on": "2026-10-15", "due_at": None}


async def test_nur_ende_geaendert_uebernimmt_den_start_aus_dem_bestand(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe(**FENSTER))
    zeile = await _ausfuehren(werkzeug, user, [_aendern(faellig_um="2026-07-15T13:00")])
    assert zeile == "1. Ändern: „Etiketten“: Fällig Mi 15.07.2026 12:00 → Mi 15.07.2026 13:00"
    assert fake.koerper("PUT", "/tasks/7") == [
        {"due_at": "2026-07-15T11:00:00Z", "start_at": "2026-07-15T08:00:00Z"}
    ]


async def test_nur_start_geaendert_uebernimmt_das_ende_aus_dem_bestand(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe(**FENSTER))
    zeile = await _ausfuehren(werkzeug, user, [_aendern(startzeit="2026-07-15T09:00")])
    assert zeile == "1. Ändern: „Etiketten“: Start Mi 15.07.2026 10:00 → Mi 15.07.2026 09:00"
    assert fake.koerper("PUT", "/tasks/7") == [
        {"due_at": "2026-07-15T10:00:00Z", "start_at": "2026-07-15T07:00:00Z"}
    ]


async def test_datumsbereich_nur_ende_geaendert(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe(**TAGE))
    await _ausfuehren(werkzeug, user, [_aendern(faellig="2026-10-20")])
    assert fake.koerper("PUT", "/tasks/7") == [{"due_on": "2026-10-20", "start_on": "2026-10-12"}]


async def test_aufgabe_ohne_start_bekommt_keinen_start_gesendet(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe())
    await _ausfuehren(werkzeug, user, [_aendern(faellig_um="2026-10-15T09:00")])
    assert fake.koerper("PUT", "/tasks/7") == [{"due_at": "2026-10-15T07:00:00Z"}]


async def test_zeitfenster_fuer_bestehende_aufgabe_mit_faelligkeitsuhrzeit(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe(due_on="2026-01-15", due_at="2026-01-15T11:00:00Z"))
    zeile = await _ausfuehren(werkzeug, user, [_aendern(startzeit="2026-01-15T10:00")])
    assert zeile == "1. Ändern: „Etiketten“: Start (leer) → Do 15.01.2026 10:00"
    assert fake.koerper("PUT", "/tasks/7") == [
        {"due_at": "2026-01-15T11:00:00Z", "start_at": "2026-01-15T09:00:00Z"}
    ]


async def test_von_ganzen_tagen_auf_zeitfenster_umstellen(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe(**TAGE))
    await _ausfuehren(
        werkzeug, user, [_aendern(startzeit="2026-10-15T10:00", faellig_um="2026-10-15T12:00")]
    )
    assert fake.koerper("PUT", "/tasks/7") == [
        {"due_at": "2026-10-15T10:00:00Z", "start_at": "2026-10-15T08:00:00Z"}
    ]


@pytest.mark.parametrize(
    ("bestand", "felder", "meldung"),
    [
        # Bestand hat ein Startdatum ohne Uhrzeit; eine Fälligkeitsuhrzeit passt nicht dazu.
        (TAGE, {"faellig_um": "2026-10-15T12:00"}, "beide mit oder beide ohne.*aktuellen Stand"),
        (FENSTER, {"faellig": "2026-07-16"}, "beide mit oder beide ohne.*aktuellen Stand"),
        (FENSTER, {"startdatum": "2026-07-14"}, "beide mit oder beide ohne.*aktuellen Stand"),
        (FENSTER, {"faellig_um": "2026-07-15T09:00"}, "vor der Fälligkeit.*aktuellen Stand"),
        (
            {"due_on": None, "due_at": None},
            {"startzeit": "2026-07-15T09:00"},
            "braucht in Asana auch eine Fälligkeit",
        ),
    ],
)
async def test_aenderung_die_nicht_zum_bestand_passt_faellt_in_der_vorschau_auf(
    werkzeug, fake, bestand, felder, meldung
):
    fake.route("GET", "/tasks/7", aufgabe(**bestand))
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match=meldung):
        await tool.bereite_vor(operationen=[_aendern(**felder)])
    assert fake.aufrufe("PUT") == []


async def test_faelligkeit_loeschen_loescht_auch_den_start(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe(**FENSTER))
    zeile = await _ausfuehren(werkzeug, user, [_aendern(faellig_um=None)])
    assert zeile == (
        "1. Ändern: „Etiketten“: Start Mi 15.07.2026 10:00 → (leer); "
        "Fällig Mi 15.07.2026 12:00 → (leer)"
    )
    assert fake.koerper("PUT", "/tasks/7") == [{"due_at": None, "start_at": None}]


async def test_start_loeschen_behaelt_die_faelligkeit(werkzeug, user, fake):
    fake.route("GET", "/tasks/7", aufgabe(**FENSTER))
    await _ausfuehren(werkzeug, user, [_aendern(startzeit=None)])
    assert fake.koerper("PUT", "/tasks/7") == [{"due_at": "2026-07-15T10:00:00Z", "start_at": None}]


async def test_zeitfenster_fuer_neue_aufgabe_des_satzes(werkzeug, user, fake):
    """Bei einer im Satz neu angelegten Aufgabe gibt es noch keinen Bestand zum Übernehmen."""
    tool, _ = werkzeug
    fake.route("GET", "/tasks/950", aufgabe("950", "Termin", due_on=None))
    fake.route("PUT", "/tasks/950", {"gid": "950"})
    await _ausfuehren(
        werkzeug,
        user,
        [
            {**_anlegen(), "platzhalter": "$a1"},
            {
                "operation": "aufgabe_aendern",
                "gid": "$a1",
                "startzeit": "2026-07-15T10:00",
                "faellig_um": "2026-07-15T12:00",
            },
        ],
    )
    assert fake.koerper("PUT", "/tasks/950") == [
        {"due_at": "2026-07-15T10:00:00Z", "start_at": "2026-07-15T08:00:00Z"}
    ]


def test_ohne_terminfelder_bleibt_der_body_leer():
    assert _termin_daten({"name": "x"}, BERLIN, aufgabe(**FENSTER)) == {}


# ---------------------------------------------------------------- Regeln laut Asana-Doku


async def test_beispiel_aus_der_vorgabe_15_bis_16_uhr(werkzeug, user, fake):
    await _ausfuehren(
        werkzeug, user, [_anlegen(startzeit="2026-10-07T15:00", faellig_um="2026-10-07T16:00")]
    )
    (koerper,) = fake.koerper("POST", "/tasks")
    assert koerper["start_at"] == "2026-10-07T13:00:00Z"
    assert koerper["due_at"] == "2026-10-07T14:00:00Z"


@pytest.mark.parametrize(
    ("bestand", "felder"),
    [
        ({}, {"faellig": "2026-10-20"}),
        ({}, {"faellig_um": "2026-10-20T09:00"}),
        (TAGE, {"faellig": "2026-10-20"}),
        (TAGE, {"startdatum": "2026-10-13"}),
        (TAGE, {"startdatum": None}),
        (TAGE, {"faellig": None}),
        (TAGE, {"startzeit": "2026-10-15T10:00", "faellig_um": "2026-10-15T12:00"}),
        (FENSTER, {"startzeit": "2026-07-15T09:00"}),
        (FENSTER, {"faellig_um": "2026-07-15T13:00"}),
        (FENSTER, {"startzeit": None}),
        (FENSTER, {"faellig_um": None}),
        (FENSTER, {"faellig": None}),
        (FENSTER, {"startdatum": "2026-07-14", "faellig": "2026-07-16"}),
        (FENSTER, {"startzeit": None, "faellig": "2026-07-16"}),
    ],
)
def test_jeder_body_haelt_die_regeln_der_asana_api_ein(bestand, felder):
    daten = _termin_daten(_aendern(**felder), BERLIN, aufgabe(**bestand))
    assert not {"start_on", "start_at"} <= set(daten)
    assert not {"due_on", "due_at"} <= set(daten)
    # Start setzen oder entfernen nur zusammen mit dem Ende im selben Request.
    if "start_at" in daten:
        assert "due_at" in daten
    if "start_on" in daten:
        assert "due_on" in daten or "due_at" in daten
    # Nie Startdatum ohne Uhrzeit zusammen mit einer Fälligkeitsuhrzeit.
    assert not (daten.get("start_on") and daten.get("due_at"))
    for feld in ("start_at", "due_at"):
        if daten.get(feld):
            assert daten[feld].endswith("Z")


async def test_meilenstein_kann_keinen_start_haben(werkzeug, fake):
    tool, _ = werkzeug
    with pytest.raises(ToolFehler, match="Meilenstein.*keinen Start"):
        await tool.bereite_vor(
            operationen=[_anlegen(meilenstein=True, startdatum="2026-10-12", faellig="2026-10-15")]
        )
    fake.route("GET", "/tasks/7", aufgabe(resource_subtype="milestone"))
    with pytest.raises(ToolFehler, match="Meilenstein.*keinen Start"):
        await tool.bereite_vor(operationen=[_aendern(startdatum="2026-10-10")])


async def test_ablehnung_sagt_claude_dass_er_nachfragen_soll(werkzeug, fake):
    tool, _ = werkzeug
    with pytest.raises(ToolFehler) as fehler:
        await tool.bereite_vor(
            operationen=[_anlegen(startdatum="2026-10-07", faellig_um="2026-10-07T16:00")]
        )
    assert "Frage den Nutzer nach der Startuhrzeit" in str(fehler.value)
    assert "nicht ersatzweise in die Beschreibung" in str(fehler.value)
    assert fake.aufrufe("POST") == []
