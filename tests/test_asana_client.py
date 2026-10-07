import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.tools.asana_client import (
    LIMIT_TEXT,
    MAX_WIEDERHOLUNGEN,
    NICHT_ERREICHBAR_TEXT,
    NICHT_KONFIGURIERT_TEXT,
    TARIF_TEXT,
    UNKLAR_TEXT,
    ZUGRIFF_VERWEIGERT_TEXT,
    AsanaClient,
    AsanaFehler,
    begrenze,
    kuerze_text,
)
from tests.asana_fake import ASANA_TOKEN, FakeAsana, asana_kontext


@pytest.fixture
def fake() -> FakeAsana:
    return FakeAsana()


@pytest.fixture
def pausen() -> list[float]:
    return []


@pytest.fixture
def baue_client(kontext, fake, pausen):
    async def schlaf(sekunden: float) -> None:
        pausen.append(sekunden)

    def _baue(**settings_werte) -> AsanaClient:
        return AsanaClient(asana_kontext(kontext, fake, **settings_werte), schlaf=schlaf)

    return _baue


def _limit(retry_after: str = "7") -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": retry_after}, json={"errors": []})


async def test_get_liest_aus_data_und_schickt_bearer_token(baue_client, fake):
    fake.route("GET", "/tasks/1", {"gid": "1", "name": "Etiketten"})
    daten = await baue_client().get("/tasks/1", felder=("name", "due_on"))

    assert daten == {"gid": "1", "name": "Etiketten"}
    (anfrage,) = fake.anfragen
    assert str(anfrage.url).startswith("https://app.asana.com/api/1.0/tasks/1")
    assert anfrage.headers["Authorization"] == f"Bearer {ASANA_TOKEN}"
    assert anfrage.url.params["opt_fields"] == "name,due_on"


async def test_schreibende_aufrufe_verpacken_den_koerper_in_data(baue_client, fake):
    fake.route("POST", "/tasks", {"gid": "9"})
    fake.route("PUT", "/tasks/9", {"gid": "9"})
    fake.route("DELETE", "/tasks/9", {})
    client = baue_client()

    assert await client.post("/tasks", {"name": "Neu"}) == {"gid": "9"}
    await client.put("/tasks/9", {"completed": True})
    await client.delete("/tasks/9")

    assert json.loads(fake.anfragen[0].content) == {"data": {"name": "Neu"}}
    assert json.loads(fake.anfragen[1].content) == {"data": {"completed": True}}
    assert fake.anfragen[2].content == b""


async def test_liste_folgt_der_paginierung(baue_client, fake):
    fake.route(
        "GET",
        "/projects",
        httpx.Response(200, json={"data": [{"gid": "1"}], "next_page": {"offset": "abc"}}),
        httpx.Response(200, json={"data": [{"gid": "2"}], "next_page": None}),
    )
    eintraege, weitere = await baue_client().liste("/projects", {"workspace": "ws1"})

    assert [e["gid"] for e in eintraege] == ["1", "2"]
    assert weitere is False
    assert "offset" not in fake.anfragen[0].url.params
    assert fake.anfragen[1].url.params["offset"] == "abc"
    assert fake.anfragen[1].url.params["workspace"] == "ws1"


async def test_liste_hoert_bei_der_obergrenze_auf(baue_client, fake):
    seite = httpx.Response(
        200, json={"data": [{"gid": str(n)} for n in range(3)], "next_page": {"offset": "x"}}
    )
    fake.route("GET", "/tags", seite)
    eintraege, weitere = await baue_client().liste("/tags", max_eintraege=2)

    assert len(eintraege) == 2
    assert weitere is True
    assert len(fake.anfragen) == 1
    assert fake.anfragen[0].url.params["limit"] == "3"


async def test_429_wartet_retry_after_und_wiederholt(baue_client, fake, pausen):
    fake.route("GET", "/tasks/1", _limit("7"), {"gid": "1"})
    assert await baue_client().get("/tasks/1") == {"gid": "1"}
    assert pausen == [7.0]
    assert len(fake.anfragen) == 2


async def test_429_bricht_nach_drei_wiederholungen_ab(baue_client, fake, pausen):
    fake.route("POST", "/tasks", _limit("2"))
    with pytest.raises(AsanaFehler, match=LIMIT_TEXT):
        await baue_client().post("/tasks", {"name": "x"})
    assert len(fake.anfragen) == 1 + MAX_WIEDERHOLUNGEN
    assert pausen == [2.0] * MAX_WIEDERHOLUNGEN


async def test_schreibender_aufruf_wird_bei_netzwerkfehler_nicht_wiederholt(baue_client, fake):
    fake.route("POST", "/tasks", httpx.ConnectTimeout("Zeit abgelaufen"))
    with pytest.raises(AsanaFehler) as fehler:
        await baue_client().post("/tasks", {"name": "x"})
    assert str(fehler.value) == UNKLAR_TEXT
    assert len(fake.anfragen) == 1


async def test_lesender_aufruf_wird_bei_netzwerkfehler_wiederholt(baue_client, fake):
    fake.route("GET", "/tasks/1", httpx.ConnectError("weg"), {"gid": "1"})
    assert await baue_client().get("/tasks/1") == {"gid": "1"}
    assert len(fake.anfragen) == 2


async def test_lesender_aufruf_gibt_nach_drei_wiederholungen_auf(baue_client, fake):
    fake.route("GET", "/tasks/1", httpx.ConnectError("weg"))
    with pytest.raises(AsanaFehler, match=NICHT_ERREICHBAR_TEXT):
        await baue_client().get("/tasks/1")
    assert len(fake.anfragen) == 1 + MAX_WIEDERHOLUNGEN


@pytest.mark.parametrize(
    ("status", "meldung"),
    [(401, ZUGRIFF_VERWEIGERT_TEXT), (402, TARIF_TEXT), (403, TARIF_TEXT)],
)
async def test_zugriff_verweigert_ohne_details(baue_client, fake, status, meldung):
    fake.route(
        "GET",
        "/tasks/1",
        httpx.Response(status, json={"errors": [{"message": f"Bearer {ASANA_TOKEN} invalid"}]}),
    )
    with pytest.raises(AsanaFehler) as fehler:
        await baue_client().get("/tasks/1")
    assert str(fehler.value) == meldung
    assert "Asana-Zugriff verweigert" in meldung
    assert "Tarif nicht verfügbar oder dein Token darf das nicht" in TARIF_TEXT
    assert fehler.value.status == status


async def test_abgelehnte_anfrage_nennt_grund_aber_nie_den_token(baue_client, fake):
    fake.route(
        "POST",
        "/projects",
        httpx.Response(400, json={"errors": [{"message": f"team: Missing input {ASANA_TOKEN}"}]}),
    )
    with pytest.raises(AsanaFehler) as fehler:
        await baue_client().post("/projects", {"name": "x"})
    assert "team: Missing input" in str(fehler.value)
    assert ASANA_TOKEN not in str(fehler.value)


async def test_token_steht_in_keiner_log_ausgabe(baue_client, fake, caplog):
    fake.route("GET", "/a", httpx.ConnectError(f"Fehler mit {ASANA_TOKEN}"))
    fake.route("GET", "/b", httpx.Response(500))
    client = baue_client()
    with caplog.at_level(logging.DEBUG):
        for pfad in ("/a", "/b"):
            with pytest.raises(AsanaFehler) as fehler:
                await client.get(pfad)
            assert ASANA_TOKEN not in str(fehler.value)
    assert caplog.records
    assert ASANA_TOKEN not in caplog.text


async def test_ohne_token_wird_nichts_gesendet(baue_client, fake):
    with pytest.raises(AsanaFehler, match="nicht konfiguriert"):
        await baue_client(asana_token=SecretStr("")).get("/tasks/1")
    assert NICHT_KONFIGURIERT_TEXT.startswith("Asana ist nicht konfiguriert")
    assert fake.anfragen == []


async def test_konfigurierter_workspace_wird_ohne_abfrage_genutzt(baue_client, fake):
    assert await baue_client(asana_workspace_gid="ws42").workspace_gid() == "ws42"
    assert fake.anfragen == []


async def test_genau_ein_workspace_wird_genutzt_und_gemerkt(baue_client, fake):
    fake.route("GET", "/workspaces", [{"gid": "ws7", "name": "Grünschwert"}])
    client = baue_client(asana_workspace_gid="")
    assert await client.workspace_gid() == "ws7"
    assert await client.workspace_gid() == "ws7"
    assert len(fake.anfragen) == 1


async def test_mehrere_workspaces_ergeben_fehlermeldung_mit_auswahl(baue_client, fake):
    fake.route(
        "GET",
        "/workspaces",
        [{"gid": "ws7", "name": "Grünschwert"}, {"gid": "ws8", "name": "Privat"}],
    )
    with pytest.raises(AsanaFehler) as fehler:
        await baue_client(asana_workspace_gid="").workspace_gid()
    for teil in ("Grünschwert (ws7)", "Privat (ws8)", "ASANA_WORKSPACE_GID"):
        assert teil in str(fehler.value)


async def test_async_with_nutzt_eine_verbindung_fuer_mehrere_aufrufe(baue_client, fake):
    fake.route("GET", "/tasks/1", {"gid": "1"})
    client = baue_client()
    async with client:
        http = client._http
        await client.get("/tasks/1")
        await client.get("/tasks/1")
        assert client._http is http
    assert client._http is None


def test_begrenze_und_kuerze_text():
    voll = begrenze([{"gid": str(n)} for n in range(40)])
    assert voll["anzahl"] == 30
    assert len(voll["eintraege"]) == 30
    assert "hinweis" in voll
    assert "hinweis" not in begrenze([{"gid": "1"}])
    assert "hinweis" in begrenze([{"gid": "1"}], weitere=True)
    assert kuerze_text("  kurz ", 10) == "kurz"
    assert kuerze_text("x" * 50, 10) == "x" * 9 + "…"
    assert kuerze_text(None, 10) == ""
