"""Mail, Schritt 2: /verbinden mail, /trennen mail, Login-Test, Umgang mit dem Passwort."""

import imaplib
import logging
import ssl
from dataclasses import replace

import pytest
from sqlalchemy import select, text

from app.auth.tresor import Tresor
from app.auth.zugaenge import Zugaenge
from app.channels.befehle import MAIL_KEIN_RECHT_TEXT, NUR_PRIVAT_TEXT, Befehle
from app.db.models import AuditLog, Message, UserSecret
from app.db.session import db_sitzung
from app.mail import verbindung
from app.mail.konten import Postfach, label_aus, labels, lade_postfach, loese_konto
from app.mail.verbindung import (
    ABGELEHNT_TEXT,
    NICHT_ERREICHBAR_TEXT,
    ZU_VIELE_ANMELDUNGEN_TEXT,
    Anmeldebremse,
    EchtesNetz,
    MailFehler,
    pruefe_imap,
    pruefe_smtp,
    tls_kontext,
)
from app.tools.base import ToolFehler
from tests.conftest import ADMIN_ID
from tests.test_zugaenge import (  # noqa: F401
    CHAT_ID,
    _alles_in_der_datenbank,
    _update,
    an_modell,
    baue_kanal,
    geloescht,
    gesendet,
)

ADRESSE_A = "lea@gruenschwert.example"
PASSWORT_A = "Sehr-Geheimes-Passwort-von-Lea-123"
ADRESSE_B = "theis@gruenschwert.example"
PASSWORT_B = "Anderes-Passwort-von-Theis-456"


@pytest.fixture
def server(mail_server):
    mail_server.postfach(ADRESSE_A, PASSWORT_A)
    mail_server.postfach(ADRESSE_B, PASSWORT_B)
    return mail_server


async def _verbinde(kanal, adresse=ADRESSE_A, passwort=PASSWORT_A, telegram_id=None, befehl=None):
    extra = {} if telegram_id is None else {"telegram_id": telegram_id}
    await kanal._bei_befehl(_update(befehl or "/verbinden mail", **extra), None)
    await kanal._bei_nachricht(_update(adresse, message_id=41, **extra), None)
    await kanal._bei_nachricht(_update(passwort, message_id=42, **extra), None)


async def test_verbinden_mail_ohne_dass_das_passwort_irgendwo_landet(
    baue_kanal,
    mkontext,
    mail_netz,
    server,
    user,
    session_fabrik,
    an_modell,
    geloescht,
    gesendet,
    monkeypatch,
    caplog,
):
    kanal = baue_kanal(mkontext, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        await kanal._bei_befehl(_update("/verbinden mail"), None)
        # Beim ersten Postfach: Hinweis in zwei Sätzen, dass Inhalte an den KI-Dienst gehen.
        assert "gehen die Inhalte dieser Mails zur Verarbeitung an den KI-Dienst" in gesendet[-1]
        assert "E-Mail-Adresse" in gesendet[-1]
        await kanal._bei_nachricht(_update(ADRESSE_A, message_id=41), None)
        assert "Passwort" in gesendet[-1] and "lösche die Nachricht sofort" in gesendet[-1]
        await kanal._bei_nachricht(_update(PASSWORT_A, message_id=42), None)

    # deleteMessage für genau die Nachricht mit dem Passwort
    assert geloescht == [(CHAT_ID, 42)]
    # weder Adresse noch Passwort gingen an das Modell
    assert an_modell == []
    assert gesendet[-1] == (
        f"✅ Das Postfach {ADRESSE_A} ist verbunden.\n"
        "Eingang (IMAP): ok\n"
        "Ausgang (SMTP): ok (Port 587 mit STARTTLS)\n"
        "Bei mir heißt es „lea“. Möchtest du einen anderen Namen? Dann antworte mit „name "
        "<neuer name>“. Sonst schreib einfach weiter.\n"
        "Deine Nachricht mit dem Passwort habe ich aus dem Chat gelöscht."
    )
    # nicht in messages, im Audit-Log nur Dienst und Label, nirgends im Klartext
    async with session_fabrik() as session:
        assert list(await session.scalars(select(Message))) == []
        audit = list(await session.scalars(select(AuditLog)))
        (eintrag,) = list(await session.scalars(select(UserSecret)))
    assert [(a.tool_name, a.parameter) for a in audit] == [
        ("/verbinden", {"dienst": "mail", "konto": "lea"})
    ]
    assert (eintrag.dienst, eintrag.label, eintrag.user_id) == ("mail", "lea", user.id)
    alles = await _alles_in_der_datenbank(session_fabrik)
    assert PASSWORT_A not in alles and ADRESSE_A not in alles
    assert PASSWORT_A not in caplog.text
    assert all(PASSWORT_A not in text_ for text_ in gesendet)
    # Login-Test an beiden Servern von united-domains, gesendet wurde nichts
    assert mail_netz.imap_ziele == [("imaps.udag.de", 993)]
    assert mail_netz.smtp_ziele == [("smtps.udag.de", 587, "starttls")]
    assert (server.imap_anmeldungen, server.smtp_anmeldungen) == (1, 1)
    assert server.gesendet == []
    assert "LOGIN <verborgen>" in server.imap_befehle
    # verschlüsselt gespeichert und entschlüsselbar nur für die Person selbst
    tresor = Tresor.aus_settings(mkontext.settings)
    postfach = await lade_postfach(session_fabrik, tresor, user, "lea")
    assert postfach == Postfach(
        "lea",
        ADRESSE_A,
        PASSWORT_A,
        "imaps.udag.de",
        993,
        "smtps.udag.de",
        587,
        smtp_sicherheit="starttls",
    )
    assert postfach.senden is True
    assert PASSWORT_A not in repr(postfach)

    # Anderer Name gewünscht
    await kanal._bei_nachricht(_update("name Shop", message_id=43), None)
    assert gesendet[-1] == "Das Postfach heißt jetzt „shop“."
    assert await labels(session_fabrik, user) == ["shop"]
    assert (await lade_postfach(session_fabrik, tresor, user, "shop")).passwort == PASSWORT_A
    # Danach ist die nächste Nachricht wieder eine ganz normale.
    await kanal._bei_nachricht(_update("Was ist heute fällig?", message_id=44), None)
    assert an_modell == ["Was ist heute fällig?"]
    assert geloescht == [(CHAT_ID, 42)]


async def test_ohne_namenswunsch_geht_die_naechste_nachricht_normal_weiter(
    baue_kanal, mkontext, server, user, session_fabrik, an_modell, gesendet, monkeypatch
):
    kanal = baue_kanal(mkontext, monkeypatch)
    await _verbinde(kanal)
    await kanal._bei_nachricht(_update("Zeig mir meine neuen Mails", message_id=43), None)
    assert an_modell == ["Zeig mir meine neuen Mails"]
    assert await labels(session_fabrik, user) == ["lea"]

    await _verbinde(kanal, ADRESSE_B, PASSWORT_B)
    # Beim zweiten Postfach kommt der Hinweis zum KI-Dienst nicht noch einmal.
    assert not [t for t in gesendet[-4:] if "KI-Dienst Anthropic" in t]
    await kanal._bei_nachricht(_update("ok", message_id=43), None)
    assert gesendet[-1] == "Alles klar, das Postfach heißt „theis“."
    await kanal._bei_nachricht(_update("name lea", message_id=44), None)
    assert an_modell[-1] == "name lea"


async def test_falsches_passwort_wird_nicht_gespeichert(
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
    kanal = baue_kanal(mkontext, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        await _verbinde(kanal, passwort="falsches-passwort-xyz")
    assert geloescht == [(CHAT_ID, 42)] and an_modell == []
    assert gesendet[-1].startswith(
        f"Das hat nicht geklappt:\nEingang (IMAP): {ABGELEHNT_TEXT}\n"
        "Ausgang (SMTP): nicht geprüft\nEs wurde nichts gespeichert."
    )
    assert "falsches-passwort-xyz" not in gesendet[-1] + caplog.text
    assert ADRESSE_A not in caplog.text
    async with session_fabrik() as session:
        assert list(await session.scalars(select(UserSecret))) == []
        (audit,) = list(await session.scalars(select(AuditLog)))
    assert (audit.parameter, audit.fehler) == ({"dienst": "mail"}, "abgelehnt")
    # Eine abgelehnte Anmeldung wird nicht wiederholt.
    assert server.imap_anmeldungen == 1 and server.smtp_anmeldungen == 0
    # Danach wartet der Bot nicht mehr auf ein Passwort.
    await kanal._bei_nachricht(_update("Hallo", message_id=43), None)
    assert an_modell == ["Hallo"]


async def test_server_nicht_erreichbar_nach_zwei_wiederholungen(
    baue_kanal, mkontext, server, session_fabrik, gesendet, monkeypatch
):
    server.imap_ausfaelle = 10
    kanal = baue_kanal(mkontext, monkeypatch)
    await _verbinde(kanal)
    assert f"Eingang (IMAP): {NICHT_ERREICHBAR_TEXT}\nAusgang (SMTP): ok" in gesendet[-1]
    assert "Es wurde nichts gespeichert." in gesendet[-1]
    assert server.imap_verbindungen == 3
    async with session_fabrik() as session:
        assert list(await session.scalars(select(UserSecret))) == []


async def test_kurzer_ausfall_wird_durch_wiederholung_ueberbrueckt(
    mkontext, server, user, session_fabrik
):
    server.imap_ausfaelle = 2
    server.smtp_ausfaelle = 1
    postfach = await Zugaenge(mkontext).verbinde_mail(user, ADRESSE_A, PASSWORT_A)
    assert postfach.label == "lea"
    assert await labels(session_fabrik, user) == ["lea"]


async def test_smtp_lehnt_ab_dann_wird_nichts_gespeichert(mkontext, server, user, session_fabrik):
    postfach = Postfach("x", ADRESSE_A, "falsch", "h", 993, "h", 465)
    with pytest.raises(MailFehler, match=ABGELEHNT_TEXT):
        await pruefe_smtp(mkontext, postfach)
    with pytest.raises(MailFehler, match=ABGELEHNT_TEXT):
        await pruefe_imap(mkontext, postfach)
    assert server.gesendet == []


async def test_passwort_mit_umlauten_und_sonderzeichen(mkontext, server, user):
    for adresse, passwort in (
        ("a@x.example", 'Pässwört "mit" \\ Zeichen'),
        ("b@x.example", 'a"b\\c d'),
    ):
        server.postfach(adresse, passwort)
        postfach = await Zugaenge(mkontext).verbinde_mail(user, adresse, passwort)
        assert postfach.passwort == passwort
    assert any(befehl.startswith("AUTHENTICATE PLAIN") for befehl in server.imap_befehle)


async def test_mehrere_postfaecher_verbunden_und_trennen(
    baue_kanal, mkontext, server, user, session_fabrik, gesendet, monkeypatch
):
    kanal = baue_kanal(mkontext, monkeypatch)
    await _verbinde(kanal)
    await _verbinde(kanal, ADRESSE_B, PASSWORT_B)
    await kanal._bei_befehl(_update("/verbunden"), None)
    assert gesendet[-1] == (
        f"Verbunden: Postfach lea ({ADRESSE_A}), Postfach theis ({ADRESSE_B}). "
        "Die Zugangsdaten selbst zeige ich nie an."
    )
    await kanal._bei_befehl(_update("/trennen mail"), None)
    assert gesendet[-1] == "Welches Postfach? /trennen mail <name>. Deine: lea, theis."
    await kanal._bei_befehl(_update("/trennen mail fremd"), None)
    assert gesendet[-1] == "Ein Postfach „fremd“ hast du nicht verbunden. Deine: lea, theis."
    await kanal._bei_befehl(_update("/trennen mail theis"), None)
    assert gesendet[-1].startswith("Das Postfach „theis“ ist getrennt.")
    assert await labels(session_fabrik, user) == ["lea"]
    await kanal._bei_befehl(_update("/trennen mail"), None)
    assert await labels(session_fabrik, user) == []
    await kanal._bei_befehl(_update("/trennen mail"), None)
    assert gesendet[-1] == "Du hast kein Postfach verbunden. Los geht es mit /verbinden mail."
    assert all(PASSWORT_A not in t and PASSWORT_B not in t for t in gesendet)


async def test_dieselbe_adresse_erneut_verbinden_ersetzt_den_eintrag(
    mkontext, server, user, session_fabrik
):
    zugaenge = Zugaenge(mkontext)
    await zugaenge.verbinde_mail(user, ADRESSE_A, PASSWORT_A)
    await zugaenge.benenne_mail_um(user, "lea", "privat")
    await zugaenge.setze_signatur(user, "privat", "Viele Grüße\nLea")
    server.postfaecher[ADRESSE_A].passwort = "Neues-Passwort-789"
    postfach = await zugaenge.verbinde_mail(user, ADRESSE_A, "Neues-Passwort-789")
    assert (postfach.label, postfach.signatur) == ("privat", "Viele Grüße\nLea")
    assert await labels(session_fabrik, user) == ["privat"]
    # Ein zweites Postfach mit gleichem Namensteil bekommt ein eigenes Label.
    server.postfach("privat@anders.example", "pw-anders-1")
    zweites = await zugaenge.verbinde_mail(user, "privat@anders.example", "pw-anders-1")
    assert zweites.label == "privat-2"


async def test_eigene_server_je_postfach(
    baue_kanal, mkontext, mail_netz, server, user, session_fabrik, gesendet, monkeypatch
):
    kanal = baue_kanal(mkontext, monkeypatch)
    await _verbinde(kanal, befehl="/verbinden mail imap.anders.example:1993 smtp.anders.example")
    assert mail_netz.imap_ziele == [("imap.anders.example", 1993)]
    assert mail_netz.smtp_ziele == [("smtp.anders.example", 587, "starttls")]
    tresor = Tresor.aus_settings(mkontext.settings)
    postfach = await lade_postfach(session_fabrik, tresor, user, "lea")
    assert (postfach.imap_host, postfach.imap_port) == ("imap.anders.example", 1993)
    await kanal._bei_befehl(_update("/verbinden mail nur-einer"), None)
    assert gesendet[-1].startswith("So geht es: /verbinden mail.")
    await kanal._bei_befehl(_update("/verbinden mail böse;host smtp.x.example"), None)
    assert "name oder name:port" in gesendet[-1]


async def test_verbinden_mail_nur_privat_nur_mit_recht_und_abbrechbar(
    baue_kanal, mkontext, server, user, session_fabrik, an_modell, geloescht, gesendet, monkeypatch
):
    kanal = baue_kanal(mkontext, monkeypatch)
    await kanal._bei_befehl(_update("/verbinden mail", chat_typ="group"), None)
    assert gesendet[-1] == NUR_PRIVAT_TEXT
    # Ein anderer Befehl bricht ab; danach wird nichts mehr als Adresse oder Passwort gelesen.
    await kanal._bei_befehl(_update("/verbinden mail"), None)
    await kanal._bei_nachricht(_update(ADRESSE_A), None)
    await kanal._bei_befehl(_update("/hilfe"), None)
    await kanal._bei_nachricht(_update("Hallo"), None)
    assert an_modell == ["Hallo"] and geloescht == []
    # Keine gültige Adresse: Abbruch, nichts gespeichert.
    await kanal._bei_befehl(_update("/verbinden mail"), None)
    await kanal._bei_nachricht(_update("das ist keine adresse"), None)
    assert "nicht nach einer E-Mail-Adresse" in gesendet[-1]
    assert await labels(session_fabrik, user) == []
    # Ohne das Recht mail.eigene
    befehle = Befehle(session_fabrik, Zugaenge(mkontext))
    ohne_recht = replace(user, rechte=frozenset())
    antwort = await befehle.fuehre_aus("verbinden", ohne_recht, ["mail"], CHAT_ID, True)
    assert antwort.text == MAIL_KEIN_RECHT_TEXT
    assert befehle.dialog(CHAT_ID, user.telegram_id) is None


async def test_niemand_erreicht_ein_fremdes_postfach(
    baue_kanal,
    mkontext,
    server,
    user,
    admin,
    session_fabrik,
    laufzeit_fabrik,
    gesendet,
    monkeypatch,
):
    """Person A verbindet ein Postfach. Person B, hier sogar Admin, sieht davon nichts."""
    kanal = baue_kanal(mkontext, monkeypatch)
    await _verbinde(kanal)
    assert admin.ist_admin
    assert await labels(laufzeit_fabrik, user) == ["lea"]
    assert await labels(laufzeit_fabrik, admin) == []
    tresor = Tresor.aus_settings(mkontext.settings)
    assert await lade_postfach(laufzeit_fabrik, tresor, admin, "lea") is None
    # konto wird nur innerhalb der eigenen Einträge aufgelöst
    assert (await loese_konto(mkontext, user, "lea")).adresse == ADRESSE_A
    assert (await loese_konto(mkontext, user, None)).adresse == ADRESSE_A
    with pytest.raises(ToolFehler, match="noch kein Postfach verbunden"):
        await loese_konto(mkontext, admin, "lea")
    # Direktabfrage mit der Laufzeitrolle im Kontext von B: null Zeilen, auch per SQL
    async with db_sitzung(laufzeit_fabrik, admin) as session:
        assert list(await session.scalars(select(UserSecret))) == []
        zeilen = await session.execute(text("SELECT * FROM user_secrets WHERE dienst = 'mail'"))
        assert zeilen.all() == []
        geloescht_ = await session.execute(text("DELETE FROM user_secrets"))
        assert geloescht_.rowcount == 0
    async with laufzeit_fabrik() as session:
        assert list(await session.scalars(select(UserSecret))) == []
    # B kann A nichts unterschieben
    with pytest.raises(Exception):  # noqa: B017
        async with db_sitzung(laufzeit_fabrik, admin) as session:
            session.add(
                UserSecret(user_id=user.id, dienst="mail", label="x", ciphertext=b"c", nonce=b"n")
            )
            await session.commit()
    # Befehle von B zeigen und trennen nichts von A
    await kanal._bei_befehl(_update("/verbunden", telegram_id=ADMIN_ID), None)
    assert "noch keinen Dienst verbunden" in gesendet[-1]
    await kanal._bei_befehl(_update("/trennen mail lea", telegram_id=ADMIN_ID), None)
    assert gesendet[-1] == "Du hast kein Postfach verbunden. Los geht es mit /verbinden mail."
    assert await labels(session_fabrik, user) == ["lea"]
    # Mit dem Schlüssel von B lässt sich der Geheimtext von A nicht öffnen.
    async with session_fabrik() as session:
        (eintrag,) = list(await session.scalars(select(UserSecret)))
    assert PASSWORT_A.encode() not in eintrag.ciphertext
    with pytest.raises(Exception):  # noqa: B017
        tresor.entschluessle(
            admin.id, "mail", eintrag.ciphertext, eintrag.nonce, eintrag.schluessel_version, "lea"
        )

    # Hat B ein eigenes Postfach, bleibt der Wert „lea“ trotzdem unerreichbar.
    await _verbinde(kanal, ADRESSE_B, PASSWORT_B, telegram_id=ADMIN_ID)
    with pytest.raises(ToolFehler) as fehler:
        await loese_konto(mkontext, admin, "lea")
    assert str(fehler.value) == "Dieses Postfach gibt es bei dir nicht. Deine Postfächer: theis."
    assert (await loese_konto(mkontext, admin, None)).adresse == ADRESSE_B


def test_label_vorschlag_und_anmeldebremse():
    assert label_aus("Theis.Ackermann@x.de") == "theis.ackermann"
    assert label_aus("ünïcode+tag@x.de") == "ncodetag"
    assert label_aus("standard@x.de") == "postfach"
    bremse = Anmeldebremse()
    postfach = Postfach("a", "A@x.de", "p", "h", 1, "h", 2)
    for sekunde in range(6):
        bremse.pruefe(postfach, 6, jetzt=float(sekunde))
    with pytest.raises(MailFehler, match="Zu viele Anmeldungen"):
        bremse.pruefe(replace(postfach, adresse="a@x.de"), 6, jetzt=30.0)
    # Ein anderes Postfach ist nicht betroffen, und nach einer Minute geht es weiter.
    bremse.pruefe(replace(postfach, adresse="b@x.de"), 6, jetzt=30.0)
    bremse.pruefe(postfach, 6, jetzt=61.0)


async def test_ratenbegrenzung_greift_vor_dem_server(mkontext, server, monkeypatch):
    monkeypatch.setattr(mkontext.settings, "mail_max_anmeldungen_pro_minute", 2)
    postfach = Postfach("lea", ADRESSE_A, PASSWORT_A, "h", 993, "h", 465)
    await pruefe_imap(mkontext, postfach)
    await pruefe_smtp(mkontext, postfach)
    with pytest.raises(MailFehler) as fehler:
        await pruefe_imap(mkontext, postfach)
    assert str(fehler.value) == ZU_VIELE_ANMELDUNGEN_TEXT
    assert (server.imap_anmeldungen, server.smtp_anmeldungen) == (1, 1)


def test_im_betrieb_immer_tls_mit_zertifikatspruefung(monkeypatch):
    kontext = tls_kontext()
    assert kontext.verify_mode == ssl.CERT_REQUIRED and kontext.check_hostname is True
    aufrufe = []

    class Attrappe:
        def __init__(self, host, port, **optionen):
            aufrufe.append((host, port, optionen))

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Attrappe)
    EchtesNetz().imap("imaps.udag.de", 993)
    ((host, port, optionen),) = aufrufe
    assert (host, port, optionen["timeout"]) == ("imaps.udag.de", 993, 20.0)
    assert optionen["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert optionen["ssl_context"].check_hostname is True
    # SMTP: TLS von Beginn an (ssl) oder erzwungenes STARTTLS; in beiden Fällen wird das
    # Zertifikat samt Hostname geprüft, und abschalten lässt sich das nirgends.
    for port, sicherheit, von_beginn, starttls in (
        (465, "ssl", True, False),
        (587, "starttls", False, True),
    ):
        smtp = EchtesNetz().smtp("smtps.udag.de", port, sicherheit)
        assert (smtp.hostname, smtp.port, smtp.use_tls, smtp._start_tls_on_connect) == (
            "smtps.udag.de",
            port,
            von_beginn,
            starttls,
        )
        assert smtp.validate_certs is True and smtp.timeout == 20.0
        assert smtp.tls_context.verify_mode == ssl.CERT_REQUIRED
        assert smtp.tls_context.check_hostname is True
    assert verbindung.MAX_WIEDERHOLUNGEN == 2
