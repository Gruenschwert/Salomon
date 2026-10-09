"""SMTP mit STARTTLS oder SSL, Ausweichen auf die andere Variante, Nur-Lesen-Modus."""

import json
import logging
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agent.loop import VERSANDTEST_ANGEBOT, Agent
from app.auth.approvals import Freigaben
from app.auth.tresor import Tresor
from app.auth.zugaenge import SPERR_HINWEIS, Zugaenge
from app.channels.base import EingehendeNachricht
from app.channels.befehle import Befehle, BefehlsAntwort
from app.config import Settings
from app.db.models import AuditLog, UserSecret, jetzt
from app.mail.konten import Postfach, labels, lade_postfach, speichere_postfach
from app.mail.verbindung import ABGELEHNT_TEXT, NICHT_ERREICHBAR_TEXT, ZERTIFIKAT_TEXT
from app.mail.werkzeug import NUR_LESEN_TEXT
from app.tools.base import ToolFehler
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block
from tests.mail_fake import baue_mail
from tests.mail_hilfen import ADRESSE_A, PASSWORT_A, Werkzeuge, frage_an
from tests.test_config import _setze
from tests.test_zugaenge import (  # noqa: F401
    CHAT_ID,
    _alles_in_der_datenbank,
    _update,
    an_modell,
    baue_kanal,
    geloescht,
    gesendet,
)

KNOEPFE = ("📥 Nur zum Lesen speichern", "❌ Abbrechen")


@pytest.fixture
def server(mail_server, mkontext):
    mkontext.settings.mail_max_anmeldungen_pro_minute = 1000
    mail_server.postfach(ADRESSE_A, PASSWORT_A)
    return mail_server


@pytest.fixture
def befehle(mkontext, session_fabrik) -> Befehle:
    return Befehle(session_fabrik, Zugaenge(mkontext))


async def _verbinde(befehle, nutzer, passwort=PASSWORT_A) -> BefehlsAntwort:
    """Der Dialog von /verbinden mail bis zur Antwort auf das Passwort."""
    await befehle.fuehre_aus("verbinden", nutzer, ["mail"], CHAT_ID, True)
    await befehle.nimm_eingabe(nutzer, CHAT_ID, ADRESSE_A)
    ergebnis = await befehle.nimm_geheimnis(nutzer, CHAT_ID, passwort)
    return ergebnis if isinstance(ergebnis, BefehlsAntwort) else BefehlsAntwort(ergebnis)


async def _postfach(mkontext, session_fabrik, nutzer, label="lea") -> Postfach | None:
    tresor = Tresor.aus_settings(mkontext.settings)
    return await lade_postfach(session_fabrik, tresor, nutzer, label)


# ---------------------------------------------------------------- Konfiguration


def test_standard_ist_port_587_mit_starttls(monkeypatch):
    _setze(monkeypatch)
    settings = Settings(_env_file=None)
    assert (settings.mail_smtp_host, settings.mail_smtp_port, settings.mail_smtp_sicherheit) == (
        "smtps.udag.de",
        587,
        "starttls",
    )
    _setze(monkeypatch, MAIL_SMTP_SICHERHEIT=" SSL ", MAIL_SMTP_PORT="465")
    assert Settings(_env_file=None).mail_smtp_sicherheit == "ssl"
    _setze(monkeypatch, MAIL_SMTP_SICHERHEIT="")
    assert Settings(_env_file=None).mail_smtp_sicherheit == "starttls"
    # Eine Einstellung ohne Verschlüsselung gibt es nicht.
    for unzulaessig in ("none", "plain", "aus", "tls-ohne-pruefung"):
        _setze(monkeypatch, MAIL_SMTP_SICHERHEIT=unzulaessig)
        with pytest.raises(ValueError, match="MAIL_SMTP_SICHERHEIT muss ssl oder starttls sein"):
            Settings(_env_file=None)


async def test_aeltere_eintraege_ohne_das_feld_gelten_als_ssl_und_duerfen_senden(
    mkontext, session_fabrik, user
):
    from app.auth.tresor import speichere_geheimnis

    alt = {
        "adresse": ADRESSE_A,
        "passwort": PASSWORT_A,
        "imap_host": "imaps.udag.de",
        "imap_port": 993,
        "smtp_host": "smtps.udag.de",
        "smtp_port": 465,
        "signatur": "",
    }
    tresor = Tresor.aus_settings(mkontext.settings)
    await speichere_geheimnis(session_fabrik, tresor, user, "mail", json.dumps(alt), "lea")
    postfach = await _postfach(mkontext, session_fabrik, user)
    assert (postfach.smtp_sicherheit, postfach.senden, postfach.smtp_port) == ("ssl", True, 465)


# ---------------------------------------------------------------- Ausweichen


async def test_bei_zeitueberschreitung_auf_ssl_wird_starttls_probiert_und_gespeichert(
    befehle, server, mail_netz, mkontext, session_fabrik, user
):
    """Der Fall auf dem Hetzner-Server: 465 mit SSL läuft ins Zeitlimit, 587 geht."""
    mkontext.settings.mail_smtp_port = 465
    mkontext.settings.mail_smtp_sicherheit = "ssl"
    server.gesperrte_smtp_ports = {25, 465}
    antwort = await _verbinde(befehle, user)
    assert antwort.aktion is None
    assert antwort.text == (
        f"✅ Das Postfach {ADRESSE_A} ist verbunden.\n"
        "Eingang (IMAP): ok\n"
        "Ausgang (SMTP): ok (Port 587 mit STARTTLS)\n"
        "Hinweis: Port 465 mit SSL/TLS war nicht erreichbar; ich nutze deshalb die andere "
        "Variante.\n"
        "Bei mir heißt es „lea“. Möchtest du einen anderen Namen? Dann antworte mit „name "
        "<neuer name>“. Sonst schreib einfach weiter."
    )
    # Erst die eingestellte Variante (mit einer Wiederholung), dann die andere.
    assert mail_netz.smtp_ziele == [
        ("smtps.udag.de", 465, "ssl"),
        ("smtps.udag.de", 465, "ssl"),
        ("smtps.udag.de", 587, "starttls"),
    ]
    # Gespeichert ist die Variante, die funktioniert hat.
    postfach = await _postfach(mkontext, session_fabrik, user)
    assert (postfach.smtp_port, postfach.smtp_sicherheit, postfach.senden) == (
        587,
        "starttls",
        True,
    )
    assert server.smtp_anmeldungen == 1 and server.gesendet == []


async def test_umgekehrt_weicht_starttls_auf_ssl_aus(
    befehle, server, mail_netz, mkontext, session_fabrik, user
):
    server.gesperrte_smtp_ports = {587}
    antwort = await _verbinde(befehle, user)
    assert "Ausgang (SMTP): ok (Port 465 mit SSL/TLS)" in antwort.text
    assert "Hinweis: Port 587 mit STARTTLS war nicht erreichbar" in antwort.text
    assert [z[1:] for z in mail_netz.smtp_ziele] == [(587, "starttls")] * 2 + [(465, "ssl")]
    postfach = await _postfach(mkontext, session_fabrik, user)
    assert (postfach.smtp_port, postfach.smtp_sicherheit) == (465, "ssl")


async def test_kein_ausweichen_bei_abgelehnter_anmeldung(
    befehle, server, mail_netz, mkontext, session_fabrik, user
):
    server.smtp_lehnt_ab = True
    antwort = await _verbinde(befehle, user)
    # Genau ein Versuch, keine zweite Variante, keine Wiederholung.
    assert mail_netz.smtp_ziele == [("smtps.udag.de", 587, "starttls")]
    assert server.smtp_anmeldungen == 1
    assert antwort.text == (
        "Der Eingang funktioniert, der Ausgang nicht:\n"
        "Eingang (IMAP): ok\n"
        f"Ausgang (SMTP): {ABGELEHNT_TEXT}\n"
        "Ich kann das Postfach nur zum Lesen speichern: Suchen, Lesen und Entwürfe gehen, "
        "Senden nicht. Gespeichert ist bisher nichts."
    )
    assert antwort.knoepfe == KNOEPFE and antwort.aktion
    assert SPERR_HINWEIS not in antwort.text
    assert await labels(session_fabrik, user) == []


async def test_falsches_passwort_wird_nirgends_zweimal_probiert(
    befehle, server, mail_netz, session_fabrik, user
):
    antwort = await _verbinde(befehle, user, passwort="falsches-passwort-xyz")
    assert antwort.text == (
        "Das hat nicht geklappt:\n"
        f"Eingang (IMAP): {ABGELEHNT_TEXT}\n"
        "Ausgang (SMTP): nicht geprüft\n"
        "Es wurde nichts gespeichert. Mit /verbinden mail kannst du es noch einmal versuchen."
    )
    assert antwort.aktion is None and antwort.knoepfe is None
    assert (server.imap_anmeldungen, server.smtp_anmeldungen) == (1, 0)
    assert mail_netz.smtp_ziele == []


async def test_ungueltiges_zertifikat_wird_gemeldet_und_nie_umgangen(
    befehle, server, mail_netz, session_fabrik, user
):
    server.smtp_ports_mit_falschem_zertifikat = {587}
    antwort = await _verbinde(befehle, user)
    assert f"Eingang (IMAP): ok\nAusgang (SMTP): {ZERTIFIKAT_TEXT}\n" in antwort.text
    # Kein zweiter Versuch über einen anderen Port und keine Anmeldung ohne gültiges Zertifikat.
    assert mail_netz.smtp_ziele == [("smtps.udag.de", 587, "starttls")]
    assert server.smtp_anmeldungen == 0
    assert antwort.knoepfe == KNOEPFE
    assert await labels(session_fabrik, user) == []


# ---------------------------------------------------------------- Nur lesen


async def test_beide_ports_gesperrt_bietet_nur_lesen_per_button_an(
    befehle, server, mail_netz, mkontext, session_fabrik, user, admin, caplog
):
    server.gesperrte_smtp_ports = {587, 465}
    with caplog.at_level(logging.DEBUG):
        antwort = await _verbinde(befehle, user)
    assert antwort.text == (
        "Der Eingang funktioniert, der Ausgang nicht:\n"
        "Eingang (IMAP): ok\n"
        f"Ausgang (SMTP): {NICHT_ERREICHBAR_TEXT} (versucht: Port 587 mit STARTTLS, Port 465 "
        "mit SSL/TLS)\n"
        f"{SPERR_HINWEIS}\n"
        "Ich kann das Postfach nur zum Lesen speichern: Suchen, Lesen und Entwürfe gehen, "
        "Senden nicht. Gespeichert ist bisher nichts."
    )
    assert "Hetzner Cloud sperrt ausgehend 25 und 465" in antwort.text
    assert antwort.knoepfe == KNOEPFE
    # Bis zum Klick ist nichts gespeichert, und nur die Person selbst kann entscheiden.
    assert await labels(session_fabrik, user) == []
    assert await befehle.bestaetige(antwort.aktion, admin, True) is None
    assert await labels(session_fabrik, user) == []

    with caplog.at_level(logging.DEBUG):
        bestaetigt = await befehle.bestaetige(antwort.aktion, user, True)
    assert bestaetigt == (
        f"✅ Das Postfach {ADRESSE_A} ist nur zum Lesen verbunden: Suchen, Lesen und Entwürfe "
        "gehen, Senden nicht. Bei mir heißt es „lea“ (anderer Name: „name <neuer name>“). Mit "
        "/testen mail lea prüfe ich den Versand erneut."
    )
    postfach = await _postfach(mkontext, session_fabrik, user)
    assert postfach.senden is False and postfach.passwort == PASSWORT_A
    verbunden = await befehle.fuehre_aus("verbunden", user, [], CHAT_ID, True)
    assert verbunden.text == (
        f"Verbunden: Postfach lea ({ADRESSE_A}, nur lesen). "
        "Die Zugangsdaten selbst zeige ich nie an."
    )
    async with session_fabrik() as session:
        audit = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert (audit[-1].tool_name, audit[-1].parameter) == (
        "/verbinden",
        {"dienst": "mail", "konto": "lea", "senden": False},
    )
    # Das Passwort steht in keiner Meldung, keinem Log und nirgends im Klartext.
    alles = await _alles_in_der_datenbank(session_fabrik) + caplog.text
    for text in (alles, antwort.text, bestaetigt, verbunden.text):
        assert PASSWORT_A not in text


async def test_abbrechen_speichert_nichts(befehle, server, session_fabrik, user):
    server.smtp_lehnt_ab = True
    antwort = await _verbinde(befehle, user)
    assert await befehle.bestaetige(antwort.aktion, user, False) == (
        "Abgebrochen. Es wurde nichts gespeichert."
    )
    async with session_fabrik() as session:
        assert list(await session.scalars(select(UserSecret))) == []
    # Der Button gilt nur einmal.
    assert (
        await befehle.bestaetige(antwort.aktion, user, True) == "Diese Aktion gibt es nicht mehr."
    )
    assert await labels(session_fabrik, user) == []


async def test_nur_lesen_verweigert_senden_antworten_und_weiterleiten_entwuerfe_gehen(
    befehle, server, mkontext, session_fabrik, user
):
    server.gesperrte_smtp_ports = {587, 465}
    antwort = await _verbinde(befehle, user)
    await befehle.bestaetige(antwort.aktion, user, True)
    server.gesperrte_smtp_ports = set()
    postfach = server.postfaecher[ADRESSE_A]
    postfach.lege_ab("INBOX", baue_mail("max@lieferant.example", ADRESSE_A, "Rechnung", "Text"))
    werkzeuge = Werkzeuge(mkontext)
    freigaben = Freigaben(mkontext, werkzeuge.registry)

    # Lesen geht.
    kennung = (await werkzeuge.daten("mail_suchen", user))["treffer"][0]["kennung"]
    assert (await werkzeuge.daten("mail_lesen", user, kennung=kennung))["betreff"] == "Rechnung"
    # Senden, Antworten und Weiterleiten werden schon beim Vorbereiten abgelehnt.
    erwartet = NUR_LESEN_TEXT.format(label="lea")
    for name, params in (
        ("mail_senden", {"an": ["x@y.example"], "betreff": "B", "text": "T"}),
        ("mail_antworten", {"kennung": kennung, "text": "T"}),
        ("mail_weiterleiten", {"kennung": kennung, "an": ["x@y.example"]}),
    ):
        with pytest.raises(ToolFehler) as fehler:
            await frage_an(freigaben, werkzeuge, user, name, **params)
        assert str(fehler.value) == erwartet
    assert "nur zum Lesen verbunden" in erwartet and "/testen mail lea" in erwartet
    assert server.gesendet == [] and server.smtp_anmeldungen == 0
    # Auch eine früher erteilte Freigabe sendet nicht mehr (Prüfung unmittelbar vor dem Senden).
    tresor = Tresor.aus_settings(mkontext.settings)
    eintrag = await _postfach(mkontext, session_fabrik, user)
    await speichere_postfach(session_fabrik, tresor, user, replace(eintrag, senden=True))
    anfrage = await frage_an(
        freigaben, werkzeuge, user, "mail_senden", an=["x@y.example"], betreff="B", text="T"
    )
    await speichere_postfach(session_fabrik, tresor, user, eintrag)
    ergebnis = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert (
        "Ausführung fehlgeschlagen" in ergebnis.text and "nur zum Lesen verbunden" in ergebnis.text
    )
    assert server.gesendet == []
    # Ein Entwurf im Entwürfe-Ordner bleibt möglich.
    anfrage = await frage_an(
        freigaben, werkzeuge, user, "mail_entwurf_speichern", betreff="Entwurf", text="Text"
    )
    ergebnis = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert ergebnis.text.startswith("✅ Entwurf abgelegt")
    assert len(postfach.ordner["Entwürfe"].mails) == 1


# ---------------------------------------------------------------- Erneut testen


async def test_testen_mail_schaltet_den_versand_frei_sobald_er_geht(
    befehle, server, mail_netz, mkontext, session_fabrik, user, admin
):
    server.gesperrte_smtp_ports = {587, 465}
    antwort = await _verbinde(befehle, user)
    await befehle.bestaetige(antwort.aktion, user, True)
    befehle.brich_ab(CHAT_ID, user.telegram_id)

    weiterhin = await befehle.fuehre_aus("testen", user, ["mail", "lea"], CHAT_ID, True)
    assert weiterhin.text == (
        "Der Versand aus „lea“ geht weiterhin nicht; es bleibt beim Lesen.\n"
        f"Ausgang (SMTP): {NICHT_ERREICHBAR_TEXT} (versucht: Port 587 mit STARTTLS, Port 465 "
        f"mit SSL/TLS)\n{SPERR_HINWEIS}"
    )
    assert (await _postfach(mkontext, session_fabrik, user)).senden is False

    server.gesperrte_smtp_ports = {465}
    geht = await befehle.fuehre_aus("testen", user, ["mail"], CHAT_ID, True)
    assert geht.text == (
        "✅ Der Versand aus „lea“ funktioniert (Port 587 mit STARTTLS). Das Postfach kann jetzt "
        "senden.\nAusgang (SMTP): ok (Port 587 mit STARTTLS)"
    )
    postfach = await _postfach(mkontext, session_fabrik, user)
    assert (postfach.senden, postfach.smtp_port, postfach.smtp_sicherheit) == (
        True,
        587,
        "starttls",
    )
    verbunden = await befehle.fuehre_aus("verbunden", user, [], CHAT_ID, True)
    assert "nur lesen" not in verbunden.text
    # Danach sendet das Postfach wirklich.
    werkzeuge = Werkzeuge(mkontext)
    freigaben = Freigaben(mkontext, werkzeuge.registry)
    anfrage = await frage_an(
        freigaben, werkzeuge, user, "mail_senden", an=["x@y.example"], betreff="B", text="T"
    )
    await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert len(server.gesendet) == 1
    # Eingaben, die nicht passen, und fremde Postfächer
    assert (await befehle.fuehre_aus("testen", user, [], CHAT_ID, True)).text == (
        "So geht es: /testen mail <name>"
    )
    fremd = await befehle.fuehre_aus("testen", admin, ["mail", "lea"], CHAT_ID, True)
    assert fremd.text == "Du hast kein Postfach verbunden. Los geht es mit /verbinden mail."


async def test_einmal_pro_woche_bietet_der_bot_den_erneuten_test_an(
    befehle, server, mkontext, settings, session_fabrik, kosten, user
):
    server.gesperrte_smtp_ports = {587, 465}
    antwort = await _verbinde(befehle, user)
    await befehle.bestaetige(antwort.aktion, user, True)
    werkzeuge = Werkzeuge(mkontext)
    freigaben = Freigaben(mkontext, werkzeuge.registry)

    async def frage() -> str:
        client = FakeAnthropic(
            claude_antwort(tool_use_block("mail_ordner_anzeigen", {})),
            claude_antwort(text_block("Dein Postfach hat fünf Ordner.")),
        )
        agent = Agent(
            settings,
            session_fabrik,
            client,
            werkzeuge.registry,
            freigaben,
            kosten,
            kontext=mkontext,
        )
        nachricht = EingehendeNachricht(CHAT_ID, ERLAUBT_ID, "Lea", "Welche Ordner habe ich?")
        return (await agent.beantworte(nachricht, user)).text

    angebot = VERSANDTEST_ANGEBOT.format(label="lea")
    assert "/testen mail lea" in angebot
    # Direkt nach dem Verbinden nicht: Die Person hat es gerade erst erfahren.
    assert await frage() == "Dein Postfach hat fünf Ordner."
    # Eine Woche später einmal, danach wieder eine Woche lang nicht.
    tresor = Tresor.aus_settings(settings)
    eintrag = await _postfach(mkontext, session_fabrik, user)
    vor_acht_tagen = (jetzt() - timedelta(days=8)).isoformat()
    await speichere_postfach(
        session_fabrik, tresor, user, replace(eintrag, versand_hinweis_am=vor_acht_tagen)
    )
    assert await frage() == "Dein Postfach hat fünf Ordner." + angebot
    assert await frage() == "Dein Postfach hat fünf Ordner."
    # Kann das Postfach senden, gibt es das Angebot nie.
    await speichere_postfach(
        session_fabrik,
        tresor,
        user,
        replace(eintrag, senden=True, versand_hinweis_am=vor_acht_tagen),
    )
    assert await frage() == "Dein Postfach hat fünf Ordner."


# ---------------------------------------------------------------- Im Chat


async def test_im_chat_haengen_die_beiden_buttons_an_der_meldung(
    baue_kanal,
    mkontext,
    server,
    session_fabrik,
    an_modell,
    geloescht,
    gesendet,
    monkeypatch,
    caplog,
):
    server.gesperrte_smtp_ports = {587, 465}
    kanal = baue_kanal(mkontext, monkeypatch)
    mit_buttons = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        mit_buttons.append((text, reply_markup))

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    with caplog.at_level(logging.DEBUG):
        await kanal._bei_befehl(_update("/verbinden mail"), None)
        await kanal._bei_nachricht(_update(ADRESSE_A, message_id=41), None)
        await kanal._bei_nachricht(_update(PASSWORT_A, message_id=42), None)
    # Die Nachricht mit dem Passwort ist gelöscht und ging nicht an das Modell.
    assert geloescht == [(CHAT_ID, 42)] and an_modell == []
    ((text, markup),) = mit_buttons
    assert text.startswith("Der Eingang funktioniert, der Ausgang nicht:\nEingang (IMAP): ok\n")
    assert text.endswith("Deine Nachricht mit dem Passwort habe ich aus dem Chat gelöscht.")
    knoepfe = markup.inline_keyboard[0]
    assert tuple(k.text for k in knoepfe) == KNOEPFE
    assert knoepfe[0].callback_data.endswith(":ja") and knoepfe[1].callback_data.endswith(":nein")
    assert PASSWORT_A not in text + caplog.text + "".join(gesendet)
    assert await labels(session_fabrik, (await _nutzer(session_fabrik))) == []


async def _nutzer(session_fabrik):
    from app.auth.users import finde_erlaubten_nutzer

    return await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)
