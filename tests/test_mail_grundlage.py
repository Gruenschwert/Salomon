"""Mail, Schritt 1: Migration (`label`, `inhalt_verschluesselt`) und Konfiguration."""

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.tresor import (
    Tresor,
    TresorFehler,
    labels_von,
    lade_geheimnis,
    loesche_geheimnis,
    speichere_geheimnis,
    verbundene_dienste,
)
from app.config import Settings
from app.db.models import UserSecret
from app.db.session import db_sitzung
from tests.conftest import migriere
from tests.test_config import _setze

MAIL_STANDARD = {
    "mail_imap_host": "imaps.udag.de",
    "mail_imap_port": 993,
    "mail_smtp_host": "smtps.udag.de",
    "mail_smtp_port": 465,
    "mail_max_zeichen": 12000,
    "mail_anhang_max_mb": 5,
    "mail_max_senden_pro_tag": 20,
    "mail_max_empfaenger": 10,
    "mail_kontext_ttl_stunden": 24,
    "mail_max_anmeldungen_pro_minute": 6,
}


def test_mail_variablen_haben_standardwerte(monkeypatch):
    _setze(monkeypatch)
    settings = Settings(_env_file=None)
    assert {name: getattr(settings, name) for name in MAIL_STANDARD} == MAIL_STANDARD


def test_leere_mail_variablen_bedeuten_standard(monkeypatch):
    _setze(monkeypatch, **{name.upper(): "" for name in MAIL_STANDARD})
    settings = Settings(_env_file=None)
    assert {name: getattr(settings, name) for name in MAIL_STANDARD} == MAIL_STANDARD


def test_env_example_nennt_alle_mail_variablen_mit_standardwert():
    zeilen = (Path(__file__).parent.parent / ".env.example").read_text().splitlines()
    for name, wert in MAIL_STANDARD.items():
        assert f"{name.upper()}={wert}" in zeilen
    # Zugangsdaten zu Postfächern stehen nie in der .env.
    assert not [z for z in zeilen if "PASSWOR" in z.upper() and z.upper().startswith("MAIL")]


async def test_migration_gibt_bestehenden_zugaengen_das_label_standard(pg_server):
    """Stand vor dem Update (Revision 0005) mit einem Asana-Zugang; danach heißt er
    `standard` und bleibt entschlüsselbar, ohne dass jemand etwas tun muss."""
    verwaltung = create_async_engine(pg_server, isolation_level="AUTOCOMMIT")
    async with verwaltung.connect() as verbindung:
        await verbindung.execute(sa.text("DROP DATABASE IF EXISTS mailmigration"))
        await verbindung.execute(sa.text("CREATE DATABASE mailmigration"))
    await verwaltung.dispose()
    url = make_url(pg_server).set(database="mailmigration").render_as_string(False)

    await asyncio.to_thread(migriere, url, "0005")
    tresor = Tresor({1: b"k" * 32})
    geheimtext, nonce, version = tresor.verschluessle(1, "asana", "alter-asana-token")
    engine = create_async_engine(url)
    async with engine.begin() as verbindung:
        await verbindung.execute(
            sa.text(
                "INSERT INTO users (id, telegram_id, anzeigename, aktiv, zeitzone, ton, "
                "erstellt_am) VALUES (1, 222, 'Theis', true, 'Europe/Berlin', 'du', now())"
            )
        )
        await verbindung.execute(
            sa.text(
                "INSERT INTO user_secrets (user_id, dienst, ciphertext, nonce, "
                "schluessel_version, erstellt_am, aktualisiert_am) "
                "VALUES (1, 'asana', :c, :n, :v, now(), now())"
            ),
            {"c": geheimtext, "n": nonce, "v": version},
        )
        await verbindung.execute(
            sa.text(
                "INSERT INTO messages (chat_id, user_id, rolle, inhalt, zeit) "
                "VALUES (5, 1, 'user', '\"Hallo\"', now())"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(migriere, url)
    await asyncio.to_thread(migriere, url)

    engine = create_async_engine(url)
    async with engine.begin() as verbindung:
        (zeile,) = (
            await verbindung.execute(
                sa.text("SELECT label, ciphertext, nonce, schluessel_version FROM user_secrets")
            )
        ).all()
        assert zeile.label == "standard"
        klartext = tresor.entschluessle(
            1, "asana", zeile.ciphertext, zeile.nonce, zeile.schluessel_version
        )
        assert klartext == "alter-asana-token"
        nachricht = (
            await verbindung.execute(sa.text("SELECT inhalt, inhalt_verschluesselt FROM messages"))
        ).one()
        assert nachricht.inhalt == "Hallo" and nachricht.inhalt_verschluesselt is None
        # Die Datentrennung der beiden Tabellen steht weiter.
        richtlinien = (
            await verbindung.execute(
                sa.text(
                    "SELECT tablename FROM pg_policies WHERE policyname = 'nutzer_trennung' "
                    "AND tablename IN ('user_secrets', 'messages') ORDER BY 1"
                )
            )
        ).scalars()
        assert list(richtlinien) == ["messages", "user_secrets"]
    await engine.dispose()


async def test_mehrere_zugaenge_je_dienst_aber_jedes_label_nur_einmal(
    settings, session_fabrik, user, admin
):
    tresor = Tresor.aus_settings(settings)
    await speichere_geheimnis(session_fabrik, tresor, user, "mail", "eins", "theis")
    await speichere_geheimnis(session_fabrik, tresor, user, "mail", "zwei", "shop")
    await speichere_geheimnis(session_fabrik, tresor, user, "mail", "zwei-neu", "shop")
    await speichere_geheimnis(session_fabrik, tresor, user, "asana", "asana-token")
    assert await labels_von(session_fabrik, user, "mail") == ["shop", "theis"]
    assert await labels_von(session_fabrik, admin, "mail") == []
    assert await verbundene_dienste(session_fabrik, user) == ["asana", "mail"]
    assert await lade_geheimnis(session_fabrik, tresor, user, "mail", "shop") == "zwei-neu"
    assert await lade_geheimnis(session_fabrik, tresor, user, "asana") == "asana-token"
    assert await lade_geheimnis(session_fabrik, tresor, admin, "mail", "shop") is None
    with pytest.raises((IntegrityError, DBAPIError)):
        async with db_sitzung(session_fabrik, user) as session:
            doppelt = UserSecret(
                user_id=user.id, dienst="mail", label="shop", ciphertext=b"x", nonce=b"y"
            )
            session.add(doppelt)
            await session.commit()
    assert await loesche_geheimnis(session_fabrik, user, "mail", "theis") is True
    assert await labels_von(session_fabrik, user, "mail") == ["shop"]
    assert await lade_geheimnis(session_fabrik, tresor, user, "asana") == "asana-token"


def test_geheimtext_ist_an_das_label_gebunden(settings):
    tresor = Tresor.aus_settings(settings)
    geheimtext, nonce, version = tresor.verschluessle(7, "mail", "passwort-a", "theis")
    assert tresor.entschluessle(7, "mail", geheimtext, nonce, version, "theis") == "passwort-a"
    for user_id, label in ((7, "shop"), (8, "theis"), (7, "standard")):
        with pytest.raises(TresorFehler):
            tresor.entschluessle(user_id, "mail", geheimtext, nonce, version, label)


def test_versiegelter_text_gehoert_nur_der_person_und_dem_zweck(settings):
    tresor = Tresor.aus_settings(settings)
    versiegelt = tresor.versiegle(7, "verlauf", "Betreff: Rechnung 4711")
    assert b"Rechnung" not in versiegelt
    assert tresor.entsiegle(7, "verlauf", versiegelt) == "Betreff: Rechnung 4711"
    for user_id, zweck in ((8, "verlauf"), (7, "freigabe")):
        with pytest.raises(TresorFehler):
            tresor.entsiegle(user_id, zweck, versiegelt)
    with pytest.raises(TresorFehler):
        tresor.entsiegle(7, "verlauf", versiegelt[:10])
