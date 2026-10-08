"""Zugangsdaten pro Person: Verschlüsselung, /verbinden, Asana mit eigenem Token, Übernahme."""

import asyncio
import base64
import logging
import os
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from app.auth.tresor import (
    Tresor,
    TresorFehler,
    lade_geheimnis,
    rotiere,
    speichere_geheimnis,
    verbundene_dienste,
)
from app.auth.users import finde_erlaubten_nutzer, uebernehme_bestand
from app.auth.zugaenge import KEIN_SCHLUESSEL_TEXT, Zugaenge, uebernehme_asana_token
from app.channels.base import Antwort
from app.channels.befehle import NUR_PRIVAT_TEXT, Befehle
from app.channels.telegram import TelegramKanal
from app.db.models import AuditLog, Base, Message, UserSecret
from app.tools.asana_client import KEIN_SCHLUESSEL_TEXT as ASANA_KEIN_SCHLUESSEL
from app.tools.asana_client import NICHT_VERBUNDEN_TEXT, AsanaClient, AsanaFehler
from app.tools.asana_lesen import AsanaTagsAnzeigen
from app.tools.base import aktueller_nutzer
from app.tools.registry import fuehre_tool_aus
from tests.asana_fake import FakeAsana, asana_kontext
from tests.conftest import ADMIN_ID, ERLAUBT_ID, TEST_HAUPTSCHLUESSEL

TOKEN_A = "2/1111/aaaa-persoenlicher-token-von-lea"
TOKEN_B = "2/2222/bbbb-persoenlicher-token-von-theis"
ALTER_TOKEN = "1/9999/gemeinsamer-token-aus-der-env"
CHAT_ID = 5


def _schluessel() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _tresor(schluessel: str = TEST_HAUPTSCHLUESSEL, version: int = 1, alt: str = "") -> Tresor:
    eintraege = {version: base64.b64decode(schluessel)}
    if alt:
        eintraege[version - 1] = base64.b64decode(alt)
    return Tresor(eintraege, version)


async def _alles_in_der_datenbank(session_fabrik) -> str:
    """Der Inhalt aller Tabellen als Text, als Eigentümer gelesen."""
    teile = []
    async with session_fabrik() as session:
        for name in Base.metadata.tables:
            for zeile in await session.execute(text(f"SELECT * FROM {name}")):
                teile.append(
                    " ".join(
                        bytes(w).decode("latin-1") if isinstance(w, bytes | memoryview) else str(w)
                        for w in zeile
                    )
                )
    return "\n".join(teile)


# ---------------------------------------------------------------- Verschlüsselung


def test_hin_und_rueckweg():
    tresor = _tresor()
    geheimtext, nonce, version = tresor.verschluessle(7, "asana", TOKEN_A)
    assert TOKEN_A.encode() not in geheimtext
    assert (len(nonce), version) == (12, 1)
    assert tresor.entschluessle(7, "asana", geheimtext, nonce, version) == TOKEN_A
    # Jede Verschlüsselung nutzt eine frische Nonce.
    assert tresor.verschluessle(7, "asana", TOKEN_A)[:2] != (geheimtext, nonce)


def test_falscher_schluessel_andere_person_oder_anderer_dienst_schlagen_fehl():
    tresor = _tresor()
    geheimtext, nonce, version = tresor.verschluessle(7, "asana", TOKEN_A)
    for versuch in (
        lambda: _tresor(_schluessel()).entschluessle(7, "asana", geheimtext, nonce, version),
        # Der Geheimtext von Person 7 ist für Person 8 wertlos (eigener Schlüssel je Person).
        lambda: tresor.entschluessle(8, "asana", geheimtext, nonce, version),
        lambda: tresor.entschluessle(7, "mail", geheimtext, nonce, version),
        lambda: tresor.entschluessle(7, "asana", geheimtext[:-1] + b"x", nonce, version),
        lambda: tresor.entschluessle(7, "asana", geheimtext, nonce, 2),
    ):
        with pytest.raises(TresorFehler) as fehler:
            versuch()
        assert TOKEN_A not in str(fehler.value)


def test_hauptschluessel_muss_32_byte_base64_sein(settings):
    for falsch in ("zu-kurz", base64.b64encode(b"x" * 16).decode(), "kein base64 !!"):
        kaputt = settings.model_copy(update={"secrets_master_key": SecretStr(falsch)})
        with pytest.raises(TresorFehler) as fehler:
            Tresor.aus_settings(kaputt)
        assert falsch not in str(fehler.value)
    leer = settings.model_copy(update={"secrets_master_key": SecretStr("")})
    assert Tresor.aus_settings(leer).verfuegbar is False
    assert Tresor.aus_settings(settings).verfuegbar is True


async def test_klartext_steht_in_keiner_tabelle(settings, user, session_fabrik):
    tresor = Tresor.aus_settings(settings)
    await speichere_geheimnis(session_fabrik, tresor, user, "asana", TOKEN_A)
    assert await lade_geheimnis(session_fabrik, tresor, user, "asana") == TOKEN_A
    inhalt = await _alles_in_der_datenbank(session_fabrik)
    assert TOKEN_A not in inhalt and "persoenlicher-token" not in inhalt
    async with session_fabrik() as session:
        (eintrag,) = list(await session.scalars(select(UserSecret)))
    assert (eintrag.user_id, eintrag.dienst, eintrag.schluessel_version) == (user.id, "asana", 1)


async def test_schluesselrotation(settings, user, admin, session_fabrik):
    alt = _tresor()
    await speichere_geheimnis(session_fabrik, alt, user, "asana", TOKEN_A)
    await speichere_geheimnis(session_fabrik, alt, admin, "asana", TOKEN_B)
    neuer_schluessel = _schluessel()
    neu = _tresor(neuer_schluessel, version=2, alt=TEST_HAUPTSCHLUESSEL)

    assert await rotiere(session_fabrik, neu, [user.id, admin.id]) == 2
    assert await rotiere(session_fabrik, neu, [user.id, admin.id]) == 0

    # Danach reicht der neue Schlüssel allein; der alte öffnet nichts mehr.
    nur_neu = _tresor(neuer_schluessel, version=2)
    assert await lade_geheimnis(session_fabrik, nur_neu, user, "asana") == TOKEN_A
    assert await lade_geheimnis(session_fabrik, nur_neu, admin, "asana") == TOKEN_B
    with pytest.raises(TresorFehler):
        await lade_geheimnis(session_fabrik, alt, user, "asana")
    async with session_fabrik() as session:
        assert {e.schluessel_version for e in await session.scalars(select(UserSecret))} == {2}


# ---------------------------------------------------------------- Asana mit eigenem Token


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    konten = {TOKEN_A: "Lea Beispiel", TOKEN_B: "Theis Ackermann", ALTER_TOKEN: "Theis (alt)"}

    def ich(anfrage: httpx.Request) -> httpx.Response:
        token = anfrage.headers["Authorization"].removeprefix("Bearer ")
        if token not in konten:
            return httpx.Response(401, json={"errors": [{"message": "Not Authorized"}]})
        return httpx.Response(200, json={"data": {"gid": "1", "name": konten[token]}})

    fake.route("GET", "/users/me", ich)
    fake.route("GET", "/tags", [{"gid": "1", "name": "eilig"}])
    return fake


@pytest.fixture
def akontext(kontext, fake):
    return asana_kontext(kontext, fake, asana_token=SecretStr(""))


def _tokens(fake: FakeAsana) -> list[str]:
    return [a.headers["Authorization"].removeprefix("Bearer ") for a in fake.anfragen]


async def test_jede_person_arbeitet_mit_ihrem_eigenen_token(
    akontext, fake, user, admin, session_fabrik
):
    tresor = Tresor.aus_settings(akontext.settings)
    await speichere_geheimnis(session_fabrik, tresor, user, "asana", TOKEN_A)
    await speichere_geheimnis(session_fabrik, tresor, admin, "asana", TOKEN_B)
    tool = AsanaTagsAnzeigen(akontext)

    assert not (await fuehre_tool_aus(tool, {}, user, akontext)).fehler
    assert not (await fuehre_tool_aus(tool, {}, admin, akontext)).fehler
    assert _tokens(fake) == [TOKEN_A, TOKEN_B]


async def test_ohne_eigenen_zugang_gibt_es_keinen_gemeinsamen_rueckfall(
    kontext, fake, user, admin, session_fabrik
):
    # In der .env steht noch der alte gemeinsame Token, und der Admin ist verbunden.
    akontext = asana_kontext(kontext, fake, asana_token=SecretStr(ALTER_TOKEN))
    tresor = Tresor.aus_settings(akontext.settings)
    await speichere_geheimnis(session_fabrik, tresor, admin, "asana", TOKEN_B)

    ergebnis = await fuehre_tool_aus(AsanaTagsAnzeigen(akontext), {}, user, akontext)
    assert ergebnis.fehler
    assert NICHT_VERBUNDEN_TEXT in ergebnis.text
    assert NICHT_VERBUNDEN_TEXT == "Verbinde zuerst deinen Asana-Zugang mit /verbinden asana."
    assert fake.anfragen == []


async def test_zwei_personen_gleichzeitig_teilen_nie_eine_verbindung(
    akontext, fake, user, admin, session_fabrik
):
    tresor = Tresor.aus_settings(akontext.settings)
    await speichere_geheimnis(session_fabrik, tresor, user, "asana", TOKEN_A)
    await speichere_geheimnis(session_fabrik, tresor, admin, "asana", TOKEN_B)
    client = AsanaClient(akontext)
    drin = asyncio.Event()
    weiter = asyncio.Event()

    async def erste() -> None:
        aktueller_nutzer.set(user)
        async with client:
            await client.get("/tags")
            drin.set()
            await weiter.wait()
            await client.get("/tags")

    async def zweite() -> None:
        aktueller_nutzer.set(admin)
        await drin.wait()
        async with client:
            await client.get("/tags")
        weiter.set()

    await asyncio.gather(erste(), zweite())
    # Während die Verbindung von A offen ist, nutzt B trotzdem den eigenen Token.
    assert _tokens(fake) == [TOKEN_A, TOKEN_B, TOKEN_A]
    assert client._sitzungen == {}


async def test_token_wird_nach_dem_tool_aufruf_verworfen(akontext, fake, user, session_fabrik):
    tresor = Tresor.aus_settings(akontext.settings)
    await speichere_geheimnis(session_fabrik, tresor, user, "asana", TOKEN_A)
    tool = AsanaTagsAnzeigen(akontext)
    ergebnis = await fuehre_tool_aus(tool, {}, user, akontext)
    assert TOKEN_A not in ergebnis.text
    assert tool.asana._sitzungen == {}
    assert TOKEN_A not in str(vars(tool.asana))


# ---------------------------------------------------------------- Übernahme des Bestands


async def test_bestehender_token_wird_einmalig_dem_admin_zugeordnet(
    kontext, fake, settings, session_fabrik
):
    akontext = asana_kontext(kontext, fake, asana_token=SecretStr(ALTER_TOKEN))
    await uebernehme_bestand(session_fabrik, akontext.settings)
    tresor = Tresor.aus_settings(akontext.settings)
    admin = await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID)
    mitarbeiter = await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)

    assert await uebernehme_asana_token(akontext) is True
    # Zweiter Start: nichts passiert.
    assert await uebernehme_asana_token(akontext) is False

    assert await lade_geheimnis(session_fabrik, tresor, admin, "asana") == ALTER_TOKEN
    assert await verbundene_dienste(session_fabrik, mitarbeiter) == []
    assert ALTER_TOKEN not in await _alles_in_der_datenbank(session_fabrik)
    async with session_fabrik() as session:
        assert len(list(await session.scalars(select(UserSecret)))) == 1

    # Der Admin arbeitet ohne Handgriff weiter, der Mitarbeiter muss sich selbst verbinden.
    tool = AsanaTagsAnzeigen(akontext)
    assert not (await fuehre_tool_aus(tool, {}, admin, akontext)).fehler
    assert _tokens(fake) == [ALTER_TOKEN]
    assert NICHT_VERBUNDEN_TEXT in (await fuehre_tool_aus(tool, {}, mitarbeiter, akontext)).text


async def test_nach_trennen_kommt_der_alte_token_nicht_zurueck(kontext, fake, session_fabrik):
    akontext = asana_kontext(kontext, fake, asana_token=SecretStr(ALTER_TOKEN))
    await uebernehme_bestand(session_fabrik, akontext.settings)
    admin = await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID)
    await uebernehme_asana_token(akontext)
    assert await Zugaenge(akontext).trenne(admin, "asana") is True
    assert await uebernehme_asana_token(akontext) is False
    assert await verbundene_dienste(session_fabrik, admin) == []


async def test_eigener_zugang_des_admins_wird_nicht_ueberschrieben(
    kontext, fake, session_fabrik, admin
):
    akontext = asana_kontext(kontext, fake, asana_token=SecretStr(ALTER_TOKEN))
    tresor = Tresor.aus_settings(akontext.settings)
    await speichere_geheimnis(session_fabrik, tresor, admin, "asana", TOKEN_B)
    assert await uebernehme_asana_token(akontext) is False
    assert await lade_geheimnis(session_fabrik, tresor, admin, "asana") == TOKEN_B


async def test_ohne_hauptschluessel_laeuft_die_alte_funktion_fuer_den_admin_weiter(
    kontext, fake, user, admin, session_fabrik
):
    akontext = asana_kontext(
        kontext,
        fake,
        asana_token=SecretStr(ALTER_TOKEN),
        secrets_master_key=SecretStr(""),
    )
    assert await uebernehme_asana_token(akontext) is False
    async with session_fabrik() as session:
        assert list(await session.scalars(select(UserSecret))) == []

    tool = AsanaTagsAnzeigen(akontext)
    assert not (await fuehre_tool_aus(tool, {}, admin, akontext)).fehler
    assert _tokens(fake) == [ALTER_TOKEN]
    # Für alle anderen gibt es den alten Token nicht.
    ergebnis = await fuehre_tool_aus(tool, {}, user, akontext)
    assert ergebnis.fehler and ASANA_KEIN_SCHLUESSEL in ergebnis.text
    assert len(fake.anfragen) == 1


# ---------------------------------------------------------------- /verbinden, /trennen, /verbunden


def _update(text_: str, telegram_id: int = ERLAUBT_ID, message_id: int = 41, chat_typ="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name="Lea Beispiel"),
        effective_chat=SimpleNamespace(id=CHAT_ID, type=chat_typ),
        effective_message=SimpleNamespace(text=text_, message_id=message_id),
    )


@pytest.fixture
def an_modell() -> list:
    """Alles, was beim Handler (und damit beim Modell) ankäme."""
    return []


@pytest.fixture
def geloescht() -> list:
    return []


@pytest.fixture
def gesendet() -> list[str]:
    return []


@pytest.fixture
def baue_kanal(session_fabrik, freigaben, kosten, alarme, user, an_modell, geloescht, gesendet):
    def _baue(akontext, monkeypatch, loeschen_scheitert: bool = False) -> TelegramKanal:
        async def handler(nachricht, nutzer):
            an_modell.append(nachricht.text)
            return Antwort(text="Antwort der KI")

        kanal = TelegramKanal(
            akontext.settings,
            session_fabrik,
            handler=handler,
            freigaben=freigaben,
            kosten=kosten,
            alarme=alarme,
            befehle=Befehle(session_fabrik, Zugaenge(akontext)),
        )

        async def sende_antwort(chat_id: int, text_: str) -> None:
            gesendet.append(text_)

        async def delete_message(self, chat_id, message_id, **kwargs):
            if loeschen_scheitert:
                raise RuntimeError("Message can't be deleted")
            geloescht.append((chat_id, message_id))

        kanal.sende_antwort = sende_antwort
        monkeypatch.setattr(type(kanal.application.bot), "delete_message", delete_message)
        return kanal

    return _baue


async def test_verbinden_nimmt_den_token_ohne_dass_er_irgendwo_landet(
    baue_kanal,
    akontext,
    fake,
    user,
    session_fabrik,
    an_modell,
    geloescht,
    gesendet,
    monkeypatch,
    caplog,
):
    kanal = baue_kanal(akontext, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        await kanal._bei_befehl(_update("/verbinden asana"), None)
        assert "persönlichen Asana-Zugriffstoken" in gesendet[-1]
        assert "lösche die Nachricht sofort" in gesendet[-1]
        await kanal._bei_nachricht(_update(TOKEN_A, message_id=42), None)

    # deleteMessage für genau diese Nachricht
    assert geloescht == [(CHAT_ID, 42)]
    # nicht an das Modell
    assert an_modell == []
    # nicht in messages, nicht im Audit-Log, nirgends im Klartext
    async with session_fabrik() as session:
        assert list(await session.scalars(select(Message))) == []
        audit = list(await session.scalars(select(AuditLog)))
    assert [a.tool_name for a in audit] == ["/verbinden"]
    assert audit[0].parameter == {"dienst": "asana"}
    assert TOKEN_A not in await _alles_in_der_datenbank(session_fabrik)
    # nicht im Log
    assert TOKEN_A not in caplog.text and "persoenlicher-token" not in caplog.text
    # nicht in der Antwort; bestätigt wird mit dem Namen des Asana-Kontos
    assert gesendet[-1] == (
        "✅ Asana ist verbunden, Konto: Lea Beispiel.\n"
        "Deine Nachricht mit dem Token habe ich aus dem Chat gelöscht."
    )
    assert all(TOKEN_A not in text_ for text_ in gesendet)
    # verschlüsselt gespeichert und sofort benutzbar
    tresor = Tresor.aus_settings(akontext.settings)
    assert await lade_geheimnis(session_fabrik, tresor, user, "asana") == TOKEN_A

    # Die nächste Nachricht ist wieder eine ganz normale.
    await kanal._bei_nachricht(_update("Was ist heute fällig?", message_id=43), None)
    assert an_modell == ["Was ist heute fällig?"]
    assert geloescht == [(CHAT_ID, 42)]


async def test_ungueltiger_token_wird_nicht_gespeichert(
    baue_kanal, akontext, session_fabrik, an_modell, geloescht, gesendet, monkeypatch, caplog
):
    kanal = baue_kanal(akontext, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        await kanal._bei_befehl(_update("/verbinden asana"), None)
        await kanal._bei_nachricht(_update("falscher-token-xyz", message_id=42), None)
    assert geloescht == [(CHAT_ID, 42)]
    assert an_modell == []
    assert "Asana lehnt diesen Token ab" in gesendet[-1]
    assert "falscher-token-xyz" not in gesendet[-1] + caplog.text
    async with session_fabrik() as session:
        assert list(await session.scalars(select(UserSecret))) == []
        assert list(await session.scalars(select(Message))) == []


async def test_nicht_loeschbare_nachricht_wird_gemeldet(
    baue_kanal, akontext, gesendet, an_modell, monkeypatch
):
    kanal = baue_kanal(akontext, monkeypatch, loeschen_scheitert=True)
    await kanal._bei_befehl(_update("/verbinden asana"), None)
    await kanal._bei_nachricht(_update(TOKEN_A), None)
    assert "Bitte lösche sie selbst" in gesendet[-1]
    assert an_modell == []


async def test_verbinden_nur_im_privaten_chat_und_nur_mit_hauptschluessel(
    baue_kanal, kontext, akontext, fake, gesendet, an_modell, monkeypatch
):
    kanal = baue_kanal(akontext, monkeypatch)
    await kanal._bei_befehl(_update("/verbinden asana", chat_typ="group"), None)
    assert gesendet[-1] == NUR_PRIVAT_TEXT
    await kanal._bei_befehl(_update("/verbinden"), None)
    assert "Möglich: asana" in gesendet[-1]
    await kanal._bei_befehl(_update("/verbinden dropbox"), None)
    assert "Möglich: asana" in gesendet[-1]
    # In keinem dieser Fälle wartet der Bot auf ein Geheimnis.
    await kanal._bei_nachricht(_update("Hallo"), None)
    assert an_modell == ["Hallo"]

    ohne = asana_kontext(kontext, fake, secrets_master_key=SecretStr(""))
    kanal = baue_kanal(ohne, monkeypatch)
    await kanal._bei_befehl(_update("/verbinden asana"), None)
    assert gesendet[-1] == KEIN_SCHLUESSEL_TEXT
    assert "SECRETS_MASTER_KEY" in gesendet[-1]


async def test_ein_anderer_befehl_bricht_das_verbinden_ab(
    baue_kanal, akontext, gesendet, an_modell, geloescht, monkeypatch
):
    kanal = baue_kanal(akontext, monkeypatch)
    await kanal._bei_befehl(_update("/verbinden asana"), None)
    await kanal._bei_befehl(_update("/verbunden"), None)
    assert "noch keinen Dienst verbunden" in gesendet[-1]
    await kanal._bei_nachricht(_update("Hallo"), None)
    assert an_modell == ["Hallo"] and geloescht == []


async def test_trennen_und_verbunden(
    baue_kanal, akontext, user, admin, session_fabrik, gesendet, monkeypatch
):
    tresor = Tresor.aus_settings(akontext.settings)
    await speichere_geheimnis(session_fabrik, tresor, user, "asana", TOKEN_A)
    await speichere_geheimnis(session_fabrik, tresor, admin, "asana", TOKEN_B)
    kanal = baue_kanal(akontext, monkeypatch)

    await kanal._bei_befehl(_update("/verbunden"), None)
    assert gesendet[-1] == "Verbunden: asana. Die Zugangsdaten selbst zeige ich nie an."
    await kanal._bei_befehl(_update("/trennen asana"), None)
    assert gesendet[-1] == "Asana ist getrennt. Dein gespeicherter Token ist gelöscht."
    await kanal._bei_befehl(_update("/trennen asana"), None)
    assert gesendet[-1] == "Asana war nicht verbunden."
    # Nur der eigene Eintrag ist weg; der des Admins bleibt.
    assert await verbundene_dienste(session_fabrik, user) == []
    assert await verbundene_dienste(session_fabrik, admin) == ["asana"]
    assert all(TOKEN_A not in text_ and TOKEN_B not in text_ for text_ in gesendet)


async def test_befehle_von_unbekannten_werden_ignoriert(
    baue_kanal, akontext, gesendet, monkeypatch
):
    kanal = baue_kanal(akontext, monkeypatch)
    await kanal._bei_befehl(_update("/verbinden asana", telegram_id=999), None)
    assert gesendet == []


async def test_pruefung_des_tokens_meldet_fehler_ohne_den_token(akontext, user, fake):
    fake.route("GET", "/users/me", httpx.ConnectError("weg"))
    with pytest.raises(AsanaFehler) as fehler:
        await Zugaenge(akontext).verbinde(user, "asana", TOKEN_A)
    assert TOKEN_A not in str(fehler.value)
