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
from dataclasses import replace
from typing import Protocol

import aiosmtplib
from imap_tools import BaseMailBox, MailBox
from imap_tools.errors import ImapToolsError, MailboxLoginError

from app.mail.konten import SICHERHEIT_TEXT, STARTTLS, Postfach, andere_variante
from app.mail.lauf import aktueller_mail_lauf
from app.tools.base import ToolFehler, ToolKontext

log = logging.getLogger(__name__)

TIMEOUT_SEKUNDEN = 20.0
MAX_WIEDERHOLUNGEN = 2
WARTEZEITEN_SEKUNDEN = (1.0, 3.0)
FENSTER_SEKUNDEN = 60.0

ABGELEHNT_TEXT = "Anmeldung abgelehnt"
NICHT_ERREICHBAR_TEXT = "Server nicht erreichbar"
ZERTIFIKAT_TEXT = "Zertifikat ungültig"
KEIN_TLS_TEXT = "Server bietet keine Verschlüsselung (STARTTLS) an"
# Arten von Fehlern, damit Aufrufer entscheiden können, wie es weitergeht
ART_ABGELEHNT = "abgelehnt"
ART_NICHT_ERREICHBAR = "nicht_erreichbar"
ART_ZERTIFIKAT = "zertifikat"
ART_SONSTIGES = "sonstiges"
ZU_VIELE_ANMELDUNGEN_TEXT = (
    "Zu viele Anmeldungen an diesem Postfach in kurzer Zeit. Bitte versuche es in einer Minute "
    "noch einmal."
)
SERVER_FEHLER_TEXT = "Der Mailserver hat die Anfrage abgelehnt"
SMTP_EMPFAENGER_TEXT = "Der Mailserver hat mindestens einen Empfänger abgelehnt"
SMTP_ABGELEHNT_TEXT = "Der Mailserver hat die Mail nicht angenommen"


class MailFehler(ToolFehler):
    """Erwartbarer Fehler beim Zugriff auf ein Postfach. Die Meldung nennt nur den Grund."""

    def __init__(self, meldung: str, art: str = ART_SONSTIGES) -> None:
        super().__init__(meldung)
        self.art = art


def _abgelehnt() -> MailFehler:
    return MailFehler(ABGELEHNT_TEXT, ART_ABGELEHNT)


def _nicht_erreichbar() -> MailFehler:
    return MailFehler(NICHT_ERREICHBAR_TEXT, ART_NICHT_ERREICHBAR)


def _zertifikat() -> MailFehler:
    return MailFehler(ZERTIFIKAT_TEXT, ART_ZERTIFIKAT)


class Netz(Protocol):
    def imap(self, host: str, port: int) -> BaseMailBox: ...

    def smtp(self, host: str, port: int, sicherheit: str) -> aiosmtplib.SMTP: ...


def tls_kontext() -> ssl.SSLContext:
    """Prüft Zertifikat und Hostnamen. Ohne eigenen Kontext würde imaplib beides auslassen."""
    return ssl.create_default_context()


class EchtesNetz:
    """Verbindungen im Betrieb: immer verschlüsselt, immer mit Prüfung von Zertifikat und
    Hostnamen. IMAP nutzt TLS von Beginn an, SMTP je nach Postfach TLS von Beginn an (`ssl`)
    oder STARTTLS. Mit STARTTLS wird ohne erfolgreiche Verschlüsselung nichts gesendet,
    auch keine Anmeldung."""

    def imap(self, host: str, port: int) -> BaseMailBox:
        return MailBox(host, port, timeout=TIMEOUT_SEKUNDEN, ssl_context=tls_kontext())

    def smtp(self, host: str, port: int, sicherheit: str) -> aiosmtplib.SMTP:
        starttls = sicherheit == STARTTLS
        return aiosmtplib.SMTP(
            hostname=host,
            port=port,
            use_tls=not starttls,
            # True erzwingt die Umstellung auf TLS; bietet der Server sie nicht an, bricht
            # die Verbindung ab, statt unverschlüsselt weiterzumachen.
            start_tls=starttls,
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
            raise _abgelehnt() from None
        except imaplib.IMAP4.error as exc:
            if not _ist_verbindungsfehler(exc):
                # Auch AUTHENTICATE meldet eine abgelehnte Anmeldung auf diesem Weg.
                raise _abgelehnt() from None
            grund = type(exc).__name__
        except ssl.SSLCertVerificationError:
            raise _zertifikat() from None
        except (OSError, EOFError) as exc:
            grund = type(exc).__name__
        if versuch == MAX_WIEDERHOLUNGEN:
            log.warning("IMAP nicht erreichbar (%s)", grund)
            raise _nicht_erreichbar()
        await _warte(WARTEZEITEN_SEKUNDEN[versuch])
    raise _nicht_erreichbar()


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
                raise _nicht_erreichbar() from None
            await _warte(WARTEZEITEN_SEKUNDEN[versuch])
        finally:
            if lauf is None and box is not None:
                await schliesse_imap(box)
    raise _nicht_erreichbar()


def _ist_zertifikatsfehler(exc: BaseException) -> bool:
    """Auch wenn aiosmtplib den Fehler der Zertifikatsprüfung in einen eigenen verpackt."""
    while exc is not None:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def _smtp_verbinden(
    kontext: ToolKontext, postfach: Postfach, wiederholungen: int = MAX_WIEDERHOLUNGEN
) -> aiosmtplib.SMTP:
    """Verbindet verschlüsselt (je nach Postfach TLS von Beginn an oder STARTTLS) und meldet
    sich an. Wiederholt wird nur bei Verbindungsfehlern, nie bei abgelehnter Anmeldung."""
    netz = netz_von(kontext)
    for versuch in range(wiederholungen + 1):
        bremse.pruefe(postfach, kontext.settings.mail_max_anmeldungen_pro_minute)
        smtp = netz.smtp(postfach.smtp_host, postfach.smtp_port, postfach.smtp_sicherheit)
        try:
            await smtp.connect()
        except Exception as exc:
            smtp.close()
            if _ist_zertifikatsfehler(exc):
                raise _zertifikat() from None
            if not isinstance(exc, aiosmtplib.SMTPException | OSError):
                raise
            if isinstance(exc, aiosmtplib.SMTPException) and not isinstance(
                exc,
                aiosmtplib.SMTPConnectError
                | aiosmtplib.SMTPTimeoutError
                | aiosmtplib.SMTPServerDisconnected
                | aiosmtplib.SMTPResponseException,
            ):
                # Der Server ist da, bietet aber kein STARTTLS: nie unverschlüsselt weiter.
                log.warning("SMTP ohne Verschlüsselung abgelehnt (%s)", type(exc).__name__)
                raise MailFehler(KEIN_TLS_TEXT, ART_NICHT_ERREICHBAR) from None
            grund = type(exc).__name__
        else:
            try:
                await smtp.login(postfach.adresse, postfach.passwort)
                return smtp
            except (aiosmtplib.SMTPAuthenticationError, aiosmtplib.SMTPNotSupported):
                # Abgelehnt, oder der Server bietet gar keine Anmeldung an.
                smtp.close()
                raise _abgelehnt() from None
            except (aiosmtplib.SMTPException, OSError) as exc:
                smtp.close()
                grund = type(exc).__name__
        if versuch == wiederholungen:
            log.warning("SMTP nicht erreichbar (%s)", grund)
            raise _nicht_erreichbar()
        await _warte(WARTEZEITEN_SEKUNDEN[min(versuch, len(WARTEZEITEN_SEKUNDEN) - 1)])
    raise _nicht_erreichbar()


async def _smtp_beenden(smtp: aiosmtplib.SMTP) -> None:
    try:
        await smtp.quit()
    except Exception:
        smtp.close()


async def pruefe_smtp(
    kontext: ToolKontext, postfach: Postfach, wiederholungen: int = MAX_WIEDERHOLUNGEN
) -> None:
    """Nur Anmeldung; es wird nichts gesendet."""
    await _smtp_beenden(await _smtp_verbinden(kontext, postfach, wiederholungen))


def variante_text(postfach: Postfach) -> str:
    return f"Port {postfach.smtp_port} mit {SICHERHEIT_TEXT[postfach.smtp_sicherheit]}"


async def teste_versand(
    kontext: ToolKontext, postfach: Postfach
) -> tuple[Postfach, MailFehler | None, list[str]]:
    """Login-Test am Ausgangsserver mit Ausweichen auf die andere übliche Variante.

    Zuerst die eingestellte Kombination aus Port und Verschlüsselung. Nur wenn der Server
    darüber nicht erreichbar ist (nicht bei abgelehnter Anmeldung und nicht bei ungültigem
    Zertifikat), folgt die andere: 587 mit STARTTLS bzw. 465 mit SSL/TLS. Liefert das
    Postfach mit der Variante, die funktioniert hat, sonst den Fehler, dazu die Liste der
    nicht erreichbaren Varianten.
    """
    port, sicherheit = andere_variante(postfach.smtp_port, postfach.smtp_sicherheit)
    varianten = [postfach, replace(postfach, smtp_port=port, smtp_sicherheit=sicherheit)]
    nicht_erreichbar: list[str] = []
    fehler: MailFehler | None = None
    for variante in varianten:
        try:
            # Eine Wiederholung genügt hier: Ein gesperrter Port läuft jedes Mal ins Zeitlimit.
            await pruefe_smtp(kontext, variante, wiederholungen=1)
            return variante, None, nicht_erreichbar
        except MailFehler as exc:
            fehler = exc
            if exc.art != ART_NICHT_ERREICHBAR:
                break
            nicht_erreichbar.append(variante_text(variante))
    return postfach, fehler, nicht_erreichbar


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
