"""Allgemeiner API-Aufruf: Pfadprüfung, Sperrliste, Freigabe, zweite Bestätigung, Rollen."""

import logging

import httpx
import pytest
from sqlalchemy import select

from app.agent.loop import WARTET_AUF_FREIGABE_TEXT, Agent
from app.auth.approvals import Freigaben
from app.channels.base import EingehendeNachricht
from app.db.models import Approval, AuditLog
from app.tools.asana_ops_api import (
    GESPERRTE_WORTTEILE,
    SPERRLISTE,
    pruefe_aufruf,
    pruefe_pfad,
    pruefe_sperrliste,
)
from app.tools.base import ToolFehler
from app.tools.registry import fuehre_tool_aus, lade_registry
from tests.asana_fake import ASANA_TOKEN, FakeAsana, asana_kontext
from tests.conftest import ADMIN_ID, ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

NAME = "asana_api_aufruf"
SATZ = "asana_aenderungen_ausfuehren"


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/tasks/7/stories", [{"gid": "s1", "text": "Hallo"}])
    fake.route("GET", "/tasks/7", {"gid": "7", "name": "Etiketten"})
    fake.route("POST", "/tasks/7/addDependents", {"gid": "7"})
    fake.route("PUT", "/tags/41", {"gid": "41", "name": "eilig"})
    fake.route("DELETE", "/tags/41", {})
    return fake


@pytest.fixture
def baue(kontext, fake):
    def _baue(**settings_werte):
        akontext = asana_kontext(kontext, fake, **settings_werte)
        registry = lade_registry(akontext)
        return akontext, registry, Freigaben(akontext, registry)

    return _baue


def _geschrieben(fake: FakeAsana) -> list[tuple[str, str]]:
    return [a for a in fake.aufrufe() if a[0] != "GET"]


def _aufruf(methode: str, pfad: str, **extra) -> dict:
    return {"methode": methode, "pfad": pfad, "begruendung": "Test", **extra}


# ---------------------------------------------------------------- Pfadprüfung


@pytest.mark.parametrize(
    "pfad",
    [
        "https://evil.example",
        "https://evil.example/tasks/1",
        "//evil",
        "//evil.example/tasks",
        "/../",
        "/tasks/../webhooks",
        "/tasks/1/..",
        "/tasks/./1",
        "tasks/1",
        "",
        "/",
        "/tasks/",
        "/tasks//1",
        "/tasks/1?opt_fields=name",
        "/tasks/1#x",
        "/tasks/1\n/webhooks",
        "/tasks/1\n",
        "/webhooks\n",
        "/tasks/1\r\nHost: evil.example",
        "/tasks/\x00",
        "/tasks/1\t",
        "/tasks/ 1",
        "/tasks/1%2f..%2fwebhooks",
        "/tasks/%2e%2e/webhooks",
        "/tasks\\..\\webhooks",
        "/tasks/1;x=1",
        "/tasks/1@evil.example",
        "/tasks/1:8080",
        "/täsks/1",
        "/tasks/" + "1" * 400,
        None,
        123,
        ["/tasks/1"],
    ],
)
def test_gefaehrliche_pfade_werden_abgelehnt(pfad):
    with pytest.raises(ToolFehler):
        pruefe_pfad(pfad)


@pytest.mark.parametrize(
    "pfad", ["/tasks/123", "/tasks/123/stories", "/project_templates", "/users/me", "/a-b/c_d"]
)
def test_normale_pfade_sind_erlaubt(pfad):
    assert pruefe_pfad(pfad) == pfad


@pytest.mark.parametrize(
    ("methode", "pfad"),
    [
        ("PUT", "/users/501"),
        ("PUT", "/Users/501"),
        ("POST", "/users/501/favorites"),
        ("DELETE", "/users/501"),
        ("PUT", "/workspaces/ws1"),
        ("PUT", "/WORKSPACES/ws1"),
        ("POST", "/workspaces/1/removeUser"),
        ("POST", "/workspace_memberships"),
        ("GET", "/roles"),
        ("POST", "/roles"),
        ("PUT", "/Roles/5"),
        ("GET", "/budgets"),
        ("DELETE", "/budgets/5"),
        ("GET", "/access_requests"),
        ("POST", "/access_requests/5/approve"),
        ("GET", "/webhooks"),
        ("POST", "/webhooks"),
        ("POST", "/WebHooks"),
        ("DELETE", "/webhooks/5"),
        ("GET", "/organization_exports/5"),
        ("GET", "/audit_log_events"),
        ("POST", "/attachments"),
        ("POST", "/oauth_token"),
        ("GET", "/OAuth/authorize"),
        ("POST", "/tasks/1/token"),
        ("GET", "/personal_access_tokens"),
        ("POST", "/users/me/authentication"),
    ],
)
def test_sperrliste_greift_auch_bei_gross_und_kleinschreibung(methode, pfad):
    with pytest.raises(ToolFehler, match="gesperrt"):
        pruefe_sperrliste(methode, pruefe_pfad(pfad))


@pytest.mark.parametrize(
    "pfad",
    [
        "/webhooks?x=1",
        "/tasks/1/../../webhooks",
        "/tasks/1%2f..%2f..%2fwebhooks",
        "/webhooks/",
        "//webhooks",
    ],
)
def test_querystring_und_pfad_tricks_erreichen_die_sperrliste_nicht(pfad):
    """Solche Pfade scheitern schon an der Pfadprüfung, bevor irgendetwas gesendet wird."""
    with pytest.raises(ToolFehler):
        pruefe_aufruf(_aufruf("GET", pfad))


def test_lesen_von_nutzern_und_workspaces_bleibt_erlaubt():
    for pfad in ("/users/me", "/users/501", "/workspaces/1", "/attachments/5"):
        pruefe_sperrliste("GET", pfad)
    pruefe_sperrliste("DELETE", "/attachments/5")


def test_sperrliste_ist_als_konstante_festgeschrieben():
    """Wer die Sperrliste ändert, muss diesen Test bewusst mit ändern."""
    assert {(tuple(sorted(methoden)), anfang) for methoden, anfang, _ in SPERRLISTE} == {
        (("DELETE", "POST", "PUT"), "users"),
        (("DELETE", "POST", "PUT"), "workspaces"),
        (("DELETE", "POST", "PUT"), "workspace_memberships"),
        (("DELETE", "GET", "POST", "PUT"), "roles"),
        (("DELETE", "GET", "POST", "PUT"), "budgets"),
        (("DELETE", "GET", "POST", "PUT"), "access_requests"),
        (("DELETE", "GET", "POST", "PUT"), "webhooks"),
        (("DELETE", "GET", "POST", "PUT"), "organization_exports"),
        (("DELETE", "GET", "POST", "PUT"), "audit_log_events"),
        (("POST",), "attachments"),
    }
    assert isinstance(SPERRLISTE, tuple) and isinstance(GESPERRTE_WORTTEILE, tuple)
    assert {"oauth", "token", "auth"} <= set(GESPERRTE_WORTTEILE)


async def test_das_modell_kann_die_sperrliste_nicht_umgehen(baue, admin, fake):
    akontext, registry, freigaben = baue()
    tool = registry.hole(NAME)
    for params in (
        _aufruf("POST", "/webhooks", body={"resource": "1", "target": "https://evil.example"}),
        _aufruf("PUT", "/users/501", body={"name": "x"}),
        _aufruf("POST", "/attachments", body={"parent": "7"}),
        _aufruf("POST", "https://evil.example/api/1.0/tasks"),
        _aufruf("POST", "/tasks", sperrliste=[], body={}),
    ):
        with pytest.raises((ToolFehler, TypeError)):
            await freigaben.anfragen(admin, tool, params)
    ergebnis = await fuehre_tool_aus(tool, _aufruf("GET", "/webhooks"), admin, akontext)
    assert ergebnis.fehler and "gesperrt" in ergebnis.text
    assert fake.anfragen == []


# ---------------------------------------------------------------- GET


async def test_get_laeuft_ohne_freigabe_ueber_die_agent_schleife(
    baue, admin, fake, session_fabrik, kosten
):
    akontext, registry, freigaben = baue()
    client = FakeAnthropic(
        claude_antwort(
            tool_use_block(NAME, _aufruf("get", "/tasks/7/stories", abfrage={"opt_fields": "text"}))
        ),
        claude_antwort(text_block("Ein Kommentar.")),
    )
    agent = Agent(akontext.settings, session_fabrik, client, registry, freigaben, kosten)
    antwort = await agent.beantworte(
        EingehendeNachricht(chat_id=1, absender_id=ADMIN_ID, absender_name="A", text="x"), admin
    )

    assert antwort.freigaben == ()
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["is_error"] is False
    assert '"text": "Hallo"' in ergebnis["content"]
    (anfrage,) = fake.anfragen
    assert str(anfrage.url).startswith("https://app.asana.com/api/1.0/tasks/7/stories?")
    assert anfrage.url.params["opt_fields"] == "text"
    assert anfrage.url.params["limit"] == "50"
    assert anfrage.headers["Authorization"] == f"Bearer {ASANA_TOKEN}"
    async with session_fabrik() as session:
        (eintrag,) = list(await session.scalars(select(AuditLog)))
    assert eintrag.parameter["pfad"] == "/tasks/7/stories"
    assert ASANA_TOKEN not in f"{eintrag.parameter}{eintrag.ergebnis_kurz}"


async def test_get_ergebnis_wird_gekuerzt_und_nennt_die_naechste_seite(baue, admin, fake):
    akontext, registry, _ = baue()
    fake.route(
        "GET",
        "/tasks",
        httpx.Response(
            200,
            json={
                "data": [{"gid": str(n), "name": "N" * 500} for n in range(100)],
                "next_page": {"offset": "weiter123"},
            },
        ),
    )
    tool = registry.hole(NAME)
    ergebnis = await fuehre_tool_aus(
        tool, _aufruf("GET", "/tasks", abfrage={"project": "100", "limit": 5000}), admin, akontext
    )
    assert len(ergebnis.text) <= 8000
    assert ergebnis.daten["naechste_seite"] == "weiter123"
    assert fake.anfragen[0].url.params["limit"] == "100"
    # Ein einzelnes Objekt bekommt kein Limit mitgeschickt.
    await fuehre_tool_aus(tool, _aufruf("GET", "/tasks/7"), admin, akontext)
    assert "limit" not in fake.anfragen[-1].url.params


async def test_get_mit_body_oder_schlechter_abfrage_wird_abgelehnt(baue, admin, fake):
    akontext, registry, _ = baue()
    tool = registry.hole(NAME)
    for params in (
        _aufruf("GET", "/tasks/7", body={"name": "x"}),
        _aufruf("GET", "/tasks/7", abfrage={"opt fields": "x"}),
        _aufruf("GET", "/tasks/7", abfrage={"filter": {"tief": 1}}),
        _aufruf("TRACE", "/tasks/7"),
        {"methode": "GET", "pfad": "/tasks/7", "begruendung": " "},
    ):
        ergebnis = await fuehre_tool_aus(tool, params, admin, akontext)
        assert ergebnis.fehler, params
    assert fake.anfragen == []


# ---------------------------------------------------------------- POST, PUT


async def test_post_braucht_vorschau_und_freigabe(baue, admin, fake, session_fabrik, kosten):
    akontext, registry, freigaben = baue()
    params = _aufruf(
        "POST",
        "/tasks/7/addDependents",
        body={"dependents": ["8", "9"]},
        begruendung="Aufgaben 8 und 9 warten auf 7",
    )
    client = FakeAnthropic(
        claude_antwort(tool_use_block(NAME, params)), claude_antwort(text_block("Bitte freigeben."))
    )
    agent = Agent(akontext.settings, session_fabrik, client, registry, freigaben, kosten)
    antwort = await agent.beantworte(
        EingehendeNachricht(chat_id=1, absender_id=ADMIN_ID, absender_name="A", text="x"), admin
    )

    (anfrage,) = antwort.freigaben
    assert anfrage.vorschau_text.splitlines() == [
        "Asana-Änderungssatz: 1 ändern",
        "1. API-Aufruf: POST /tasks/7/addDependents, Begründung: Aufgaben 8 und 9 warten auf 7, "
        'Body: {"dependents": ["8", "9"]}',
    ]
    assert "**" not in anfrage.vorschau_text
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["content"] == WARTET_AUF_FREIGABE_TEXT
    assert _geschrieben(fake) == []

    entscheidung = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    assert entscheidung.text == (
        '✅ API-Aufruf POST /tasks/7/addDependents ausgeführt, Antwort: {"gid": "7"}'
    )
    assert entscheidung.im_verlauf
    assert fake.koerper("POST", "/tasks/7/addDependents") == [{"dependents": ["8", "9"]}]
    # Doppelter Klick führt nichts doppelt aus.
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    assert len(_geschrieben(fake)) == 1

    async with session_fabrik() as session:
        eintraege = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    detail = next(e for e in eintraege if "freigabe" in e.parameter)
    assert detail.parameter["operationen"][0] | {} == {
        "nr": 1,
        "operation": "api_aufruf",
        "gid": "7",
        "felder": ["dependents"],
        "vorher": {},
        "methode": "POST",
        "pfad": "/tasks/7/addDependents",
        "body": {"dependents": ["8", "9"]},
        "ergebnis": "erledigt",
    }
    assert all(ASANA_TOKEN not in f"{e.parameter}{e.ergebnis_kurz}{e.fehler}" for e in eintraege)


async def test_verwerfen_fuehrt_nichts_aus(baue, admin, fake):
    _, registry, freigaben = baue()
    anfrage = await freigaben.anfragen(
        admin, registry.hole(NAME), _aufruf("PUT", "/tags/41", body={"name": "eilig"})
    )
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=False)
    assert entscheidung.text.startswith("❌ Verworfen")
    assert _geschrieben(fake) == []


async def test_fehler_von_asana_wird_gemeldet_und_im_audit_log_festgehalten(
    baue, admin, fake, session_fabrik, caplog
):
    fake.route(
        "PUT", "/tags/41", httpx.Response(400, json={"errors": [{"message": "name: Too long"}]})
    )
    _, registry, freigaben = baue()
    anfrage = await freigaben.anfragen(
        admin, registry.hole(NAME), _aufruf("PUT", "/tags/41", body={"name": "x" * 50})
    )
    with caplog.at_level(logging.DEBUG):
        entscheidung = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    assert "Asana lehnt die Anfrage ab: name: Too long" in entscheidung.text
    assert ASANA_TOKEN not in entscheidung.text + caplog.text
    async with session_fabrik() as session:
        eintraege = list(await session.scalars(select(AuditLog)))
    detail = next(e for e in eintraege if "freigabe" in e.parameter)
    assert detail.parameter["operationen"][0]["pfad"] == "/tags/41"
    assert detail.fehler == "abgebrochen"


async def test_api_aufruf_als_teil_eines_aenderungssatzes_zaehlt_ins_limit(baue, admin, fake):
    _, registry, freigaben = baue(asana_max_ops_per_changeset=2)
    op = {"operation": "api_aufruf", **_aufruf("PUT", "/tags/41", body={"name": "eilig"})}
    with pytest.raises(ToolFehler, match="hat 3 Operationen, erlaubt sind höchstens 2"):
        await freigaben.anfragen(admin, registry.hole(SATZ), {"operationen": [op, op, op]})
    anfrage = await freigaben.anfragen(admin, registry.hole(SATZ), {"operationen": [op, op]})
    assert anfrage.vorschau_text.splitlines()[0] == "Asana-Änderungssatz: 2 ändern"
    with pytest.raises(ToolFehler, match="GET.*gehören nicht in einen Änderungssatz"):
        await freigaben.anfragen(
            admin,
            registry.hole(SATZ),
            {"operationen": [{"operation": "api_aufruf", **_aufruf("GET", "/tasks/7")}]},
        )
    assert _geschrieben(fake) == []


# ---------------------------------------------------------------- DELETE


async def test_delete_ohne_zweite_bestaetigung_wird_nicht_ausgefuehrt(
    baue, admin, fake, session_fabrik
):
    _, registry, freigaben = baue()
    anfrage = await freigaben.anfragen(admin, registry.hole(NAME), _aufruf("DELETE", "/tags/41"))
    assert anfrage.vorschau_text.splitlines() == [
        "Asana-Änderungssatz: 1 🗑 löschen",
        "1. 🗑 Löschen: API-Aufruf: DELETE /tags/41, Begründung: Test",
    ]

    erste = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    assert erste.rueckfrage
    assert erste.text.splitlines()[0] == "Wirklich löschen? 1 Objekt"
    assert "DELETE /tags/41" in erste.text
    assert fake.anfragen == []
    async with session_fabrik() as session:
        assert (await session.get(Approval, anfrage.approval_id)).status == "bestätigung"

    # Ein zweites normales ✅ zählt nicht als Bestätigung.
    await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    assert fake.anfragen == []

    zweite = await freigaben.entscheiden(
        anfrage.approval_id, ADMIN_ID, genehmigt=True, bestaetigt=True
    )
    assert zweite.text == "✅ API-Aufruf DELETE /tags/41 ausgeführt"
    assert fake.aufrufe() == [("DELETE", "/tags/41")]
    assert fake.anfragen[0].content == b""


async def test_delete_ohne_loesch_rolle_wird_abgelehnt(baue, user, admin, fake):
    # Der Nutzer darf den API-Aufruf, aber nicht löschen.
    _, registry, freigaben = baue(asana_api_aufruf_roles=frozenset({"admin", "user"}))
    tool = registry.hole(NAME)
    with pytest.raises(ToolFehler, match="darf in Asana nichts löschen"):
        await freigaben.anfragen(user, tool, _aufruf("DELETE", "/tags/41"))
    await freigaben.anfragen(user, tool, _aufruf("PUT", "/tags/41", body={"name": "x"}))
    # Auch für Admins gelten Abschalter und Limit.
    _, registry, freigaben = baue(asana_delete_enabled=False)
    with pytest.raises(ToolFehler, match="abgeschaltet"):
        await freigaben.anfragen(admin, registry.hole(NAME), _aufruf("DELETE", "/tags/41"))
    _, registry, freigaben = baue(asana_max_deletes_per_changeset=1)
    loeschen = {"operation": "api_aufruf", **_aufruf("DELETE", "/tags/41")}
    with pytest.raises(ToolFehler, match="2 Löschoperationen, erlaubt sind höchstens 1"):
        await freigaben.anfragen(admin, registry.hole(SATZ), {"operationen": [loeschen, loeschen]})
    assert fake.anfragen == []


# ---------------------------------------------------------------- Rollen und Abschalter


async def test_nur_die_erlaubten_rollen_sehen_und_nutzen_das_tool(
    baue, user, admin, fake, session_fabrik, kosten
):
    akontext, registry, freigaben = baue()
    assert NAME in {d["name"] for d in registry.api_definitionen("admin")}
    assert NAME not in {d["name"] for d in registry.api_definitionen("user")}

    client = FakeAnthropic(
        claude_antwort(tool_use_block(NAME, _aufruf("GET", "/tasks/7"))),
        claude_antwort(text_block("Das darf ich nicht.")),
    )
    agent = Agent(akontext.settings, session_fabrik, client, registry, freigaben, kosten)
    await agent.beantworte(
        EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="U", text="x"), user
    )
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["is_error"] is True
    assert fake.anfragen == []
    # Auch als Operation im Änderungssatz ist die Rolle gesperrt.
    with pytest.raises(ToolFehler, match="api_aufruf ist dieser Rolle nicht erlaubt"):
        await freigaben.anfragen(
            user,
            registry.hole(SATZ),
            {
                "operationen": [
                    {"operation": "api_aufruf", **_aufruf("PUT", "/tags/41", body={"a": 1})}
                ]
            },
        )


async def test_abschalter_nimmt_das_tool_fuer_alle_weg(baue, admin, fake):
    akontext, registry, freigaben = baue(asana_api_aufruf_enabled=False)
    assert NAME not in {d["name"] for d in registry.api_definitionen("admin")}
    with pytest.raises(ToolFehler, match="abgeschaltet .ASANA_API_AUFRUF_ENABLED=false."):
        await freigaben.anfragen(
            admin,
            registry.hole(SATZ),
            {
                "operationen": [
                    {"operation": "api_aufruf", **_aufruf("PUT", "/tags/41", body={"a": 1})}
                ]
            },
        )
    assert fake.anfragen == []


def test_standardwerte_der_neuen_einstellungen(settings):
    assert settings.asana_api_aufruf_enabled is True
    assert settings.asana_api_aufruf_roles == {"admin"}
    assert settings.asana_team_verwaltung_roles == {"admin"}
    assert settings.asana_attachment_view_max_mb == 5
