"""Verbindungen zu IMAP und SMTP: Anmeldung, Zeitlimit, Wiederholungen, Ratenbegrenzung.

IMAP läuft über `imap-tools` (synchron, deshalb in einem eigenen Thread), SMTP über
`aiosmtplib`. Zertifikate werden immer geprüft. Fehler werden zu einer `MailFehler`-Meldung,
die nur den Grund nennt, nie Passwort, Adresse oder Antworttext des Servers.
"""

import asyncio
import imaplib
import logging
import ssl
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

import aiosmtplib
from imap_tools import BaseMailBox, MailBox
from imap_tools.errors import ImapToolsError, MailboxLoginError

from app.mail.konten import Postfach
from app.mail.lauf import aktueller_mail_lauf
from app.tools.base import ToolFehler, ToolKontext

log = logging.getLogger(__name__)

TIMEOUT_SEKUNDEN = 20.0
MAX_WIEDERHOLUNGEN = 2
WARTEZEITEN_SEKUNDEN = (1.0, 3.0)
FENSTER_SEKUNDEN = 60.0

ABGELEHNT_TEXT = "Anmeldung abgelehnt"
NICHT_ERREICHBAR_TEXT = "Server nicht erreichbar"
ZERTIFIKAT_TEXT = "Server nicht erreichbar: Das Zertifikat des Servers ist nicht gültig"
ZU_VIELE_ANMELDUNGEN_TEXT = (
    "Zu viele Anmeldungen an diesem Postfach in kurzer Zeit. Bitte versuche es in einer Minute "
    "noch einmal."
)
SERVER_FEHLER_TEXT = "Der Mailserver hat die Anfrage abgelehnt"
SMTP_EMPFAENGER_TEXT = "Der Mailserver hat mindestens einen Empfänger abgelehnt"
SMTP_ABGELEHNT_TEXT = "Der Mailserver hat die Mail nicht angenommen"


class MailFehler(ToolFehler):
    """Erwartbarer Fehler beim Zugriff auf ein Postfach. Die Meldung nennt nur den Grund."""


class Netz(Protocol):
    def imap(self, host: str, port: int) -> BaseMailBox: ...

    def smtp(self, host: str, port: int) -> aiosmtplib.SMTP: ...


def tls_kontext() -> ssl.SSLContext:
    """Prüft Zertifikat und Hostnamen. Ohne eigenen Kontext würde imaplib beides auslassen."""
    return ssl.create_default_context()


class EchtesNetz:
    """Verbindungen im Betrieb: immer TLS von Beginn an, immer mit Zertifikatsprüfung."""

    def imap(self, host: str, port: int) -> BaseMailBox:
        return MailBox(host, port, timeout=TIMEOUT_SEKUNDEN, ssl_context=tls_kontext())

    def smtp(self, host: str, port: int) -> aiosmtplib.SMTP:
        return aiosmtplib.SMTP(
            hostname=host,
            port=port,
            use_tls=True,
            validate_certs=True,
            tls_context=tls_kontext(),
            timeout=TIMEOUT_SEKUNDEN,
        )


def netz_von(kontext: ToolKontext) -> Netz:
    return kontext.mail_netz or EchtesNetz()


class Anmeldebremse:
    """Begrenzt die Anmeldungen je Postfach und Minute, damit der Server nicht sperrt."""

    def __init__(self) -> None:
        self._zeiten: dict[str, deque[float]] = {}

    def pruefe(self, postfach: Postfach, maximum: int, jetzt: float | None = None) -> None:
        jetzt = time.monotonic() if jetzt is None else jetzt
        zeiten = self._zeiten.setdefault(postfach.adresse.lower(), deque())
        while zeiten and jetzt - zeiten[0] >= FENSTER_SEKUNDEN:
            zeiten.popleft()
        if len(zeiten) >= maximum:
            raise MailFehler(ZU_VIELE_ANMELDUNGEN_TEXT)
        zeiten.append(jetzt)

    def leere(self) -> None:
        self._zeiten.clear()


bremse = Anmeldebremse()


async def _warte(sekunden: float) -> None:
    await asyncio.sleep(sekunden)


def _imap_anmelden(netz: Netz, postfach: Postfach) -> BaseMailBox:
    box = netz.imap(postfach.imap_host, postfach.imap_port)
    try:
        # imaplib kodiert LOGIN als ASCII; alles andere geht über AUTHENTICATE PLAIN.
        if postfach.adresse.isascii() and postfach.passwort.isascii():
            box.login(postfach.adresse, postfach.passwort, initial_folder=None)
        else:
            box.login_utf8(postfach.adresse, postfach.passwort, initial_folder=None)
    except BaseException:
        _imap_schliessen(box)
        raise
    return box


def _imap_schliessen(box: BaseMailBox) -> None:
    try:
        box.logout()
    except Exception:
        try:
            box.client.shutdown()
        except Exception:
            pass


def _ist_verbindungsfehler(exc: BaseException) -> bool:
    return isinstance(exc, OSError | EOFError | imaplib.IMAP4.abort)


async def oeffne_imap(kontext: ToolKontext, postfach: Postfach) -> BaseMailBox:
    """Meldet sich am Postfach an. Bei Verbindungsfehlern höchstens zwei Wiederholungen mit
    Wartezeit; eine abgelehnte Anmeldung wird nie wiederholt."""
    netz = netz_von(kontext)
    for versuch in range(MAX_WIEDERHOLUNGEN + 1):
        bremse.pruefe(postfach, kontext.settings.mail_max_anmeldungen_pro_minute)
        try:
            return await asyncio.to_thread(_imap_anmelden, netz, postfach)
        except MailboxLoginError:
            raise MailFehler(ABGELEHNT_TEXT) from None
        except imaplib.IMAP4.error as exc:
            if not _ist_verbindungsfehler(exc):
                # Auch AUTHENTICATE meldet eine abgelehnte Anmeldung auf diesem Weg.
                raise MailFehler(ABGELEHNT_TEXT) from None
            grund = type(exc).__name__
        except ssl.SSLCertVerificationError:
            raise MailFehler(ZERTIFIKAT_TEXT) from None
        except (OSError, EOFError) as exc:
            grund = type(exc).__name__
        if versuch == MAX_WIEDERHOLUNGEN:
            log.warning("IMAP nicht erreichbar (%s)", grund)
            raise MailFehler(NICHT_ERREICHBAR_TEXT)
        await _warte(WARTEZEITEN_SEKUNDEN[versuch])
    raise MailFehler(NICHT_ERREICHBAR_TEXT)


async def schliesse_imap(box: object) -> None:
    await asyncio.to_thread(_imap_schliessen, box)


async def schliesse_lauf() -> None:
    """Schließt alle IMAP-Verbindungen des laufenden Laufs."""
    lauf = aktueller_mail_lauf.get()
    if lauf is None:
        return
    for box in list(lauf.verbindungen.values()):
        await schliesse_imap(box)
    lauf.verbindungen.clear()


async def imap_aufruf[T](
    kontext: ToolKontext,
    postfach: Postfach,
    funktion: Callable[[BaseMailBox], T],
    *,
    wiederholen: bool = True,
) -> T:
    """Führt `funktion` mit einer angemeldeten Verbindung aus.

    Innerhalb eines Laufs wird die Verbindung je Postfach wiederverwendet und am Ende vom
    Agenten geschlossen; außerhalb davon (Ausführung nach einer Freigabe) gilt sie nur für
    diesen einen Aufruf. Bricht die Verbindung ab, wird bei lesenden Aufrufen neu verbunden;
    schreibende (`wiederholen=False`) laufen nie zweimal.
    """
    lauf = aktueller_mail_lauf.get()
    for versuch in range(MAX_WIEDERHOLUNGEN + 1):
        box = lauf.verbindungen.get(postfach.label) if lauf is not None else None
        if box is None:
            box = await oeffne_imap(kontext, postfach)
            if lauf is not None:
                lauf.verbindungen[postfach.label] = box
        try:
            return await asyncio.to_thread(funktion, box)
        except MailFehler:
            raise
        except ImapToolsError as exc:
            # Der Server hat geantwortet, aber abgelehnt (z. B. Ordner gibt es nicht).
            log.warning("IMAP-Befehl abgelehnt (%s)", type(exc).__name__)
            raise MailFehler(SERVER_FEHLER_TEXT) from None
        except (OSError, EOFError, imaplib.IMAP4.error) as exc:
            abgebrochen = _ist_verbindungsfehler(exc)
            if lauf is not None:
                lauf.verbindungen.pop(postfach.label, None)
            await schliesse_imap(box)
            box = None
            if not abgebrochen:
                log.warning("IMAP-Befehl abgelehnt (%s)", type(exc).__name__)
                raise MailFehler(SERVER_FEHLER_TEXT) from None
            if not wiederholen or versuch == MAX_WIEDERHOLUNGEN:
                log.warning("IMAP-Verbindung abgebrochen (%s)", type(exc).__name__)
                raise MailFehler(NICHT_ERREICHBAR_TEXT) from None
            await _warte(WARTEZEITEN_SEKUNDEN[versuch])
        finally:
            if lauf is None and box is not None:
                await schliesse_imap(box)
    raise MailFehler(NICHT_ERREICHBAR_TEXT)


async def _smtp_verbinden(kontext: ToolKontext, postfach: Postfach) -> aiosmtplib.SMTP:
    netz = netz_von(kontext)
    for versuch in range(MAX_WIEDERHOLUNGEN + 1):
        bremse.pruefe(postfach, kontext.settings.mail_max_anmeldungen_pro_minute)
        smtp = netz.smtp(postfach.smtp_host, postfach.smtp_port)
        try:
            await smtp.connect()
            await smtp.login(postfach.adresse, postfach.passwort)
            return smtp
        except aiosmtplib.SMTPAuthenticationError:
            smtp.close()
            raise MailFehler(ABGELEHNT_TEXT) from None
        except ssl.SSLCertVerificationError:
            smtp.close()
            raise MailFehler(ZERTIFIKAT_TEXT) from None
        except (aiosmtplib.SMTPException, OSError) as exc:
            smtp.close()
            ursache = exc.__cause__ or exc.__context__
            if isinstance(ursache, ssl.SSLCertVerificationError):
                raise MailFehler(ZERTIFIKAT_TEXT) from None
            if isinstance(exc, aiosmtplib.SMTPNotSupported):
                # Der Server bietet keine Anmeldung an.
                raise MailFehler(ABGELEHNT_TEXT) from None
            grund = type(exc).__name__
        if versuch == MAX_WIEDERHOLUNGEN:
            log.warning("SMTP nicht erreichbar (%s)", grund)
            raise MailFehler(NICHT_ERREICHBAR_TEXT)
        await _warte(WARTEZEITEN_SEKUNDEN[versuch])
    raise MailFehler(NICHT_ERREICHBAR_TEXT)


async def _smtp_beenden(smtp: aiosmtplib.SMTP) -> None:
    try:
        await smtp.quit()
    except Exception:
        smtp.close()


async def pruefe_smtp(kontext: ToolKontext, postfach: Postfach) -> None:
    """Nur Anmeldung; es wird nichts gesendet."""
    await _smtp_beenden(await _smtp_verbinden(kontext, postfach))


async def pruefe_imap(kontext: ToolKontext, postfach: Postfach) -> None:
    await schliesse_imap(await oeffne_imap(kontext, postfach))


async def smtp_sende(
    kontext: ToolKontext, postfach: Postfach, nachricht: object, empfaenger: list[str]
) -> None:
    """Versendet eine fertige Nachricht. Absender des Umschlags ist immer das Postfach selbst.
    Das Senden wird nie wiederholt; wiederholt wird nur der Verbindungsaufbau davor."""
    smtp = await _smtp_verbinden(kontext, postfach)
    try:
        await smtp.send_message(nachricht, sender=postfach.adresse, recipients=empfaenger)
    except (aiosmtplib.SMTPRecipientsRefused, aiosmtplib.SMTPRecipientRefused):
        raise MailFehler(SMTP_EMPFAENGER_TEXT) from None
    except aiosmtplib.SMTPResponseException as exc:
        log.warning("SMTP hat die Mail abgelehnt (Code %s)", exc.code)
        raise MailFehler(SMTP_ABGELEHNT_TEXT) from None
    except (aiosmtplib.SMTPException, OSError) as exc:
        log.warning("SMTP-Verbindung beim Senden abgebrochen (%s)", type(exc).__name__)
        raise MailFehler(
            "Die Verbindung zum Mailserver ist beim Senden abgebrochen. Ob die Mail "
            "angekommen ist, ist unklar. Bitte im Ordner Gesendet und beim Empfänger prüfen; "
            "es wurde nichts wiederholt."
        ) from None
    finally:
        await _smtp_beenden(smtp)
