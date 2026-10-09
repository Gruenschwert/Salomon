"""Lokaler Test-Mailserver: IMAP und SMTP im selben Prozess, nur im Arbeitsspeicher.

Die Tests sprechen über echte Sockets mit `imaplib`/`imap-tools` und `aiosmtplib`, aber nie
mit einem Server im Internet. Verschlüsselt wird hier nicht; im Betrieb erzwingt
`EchtesNetz` TLS mit Zertifikatsprüfung.
"""

import base64
import email
import email.policy
import re
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.message import EmailMessage

import aiosmtplib
from imap_tools import MailBoxUnencrypted
from imap_tools.imap_utf7 import utf7_decode, utf7_encode

MONATE = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class FakeMail:
    uid: int
    roh: bytes
    flags: set[str] = field(default_factory=set)
    eingang: datetime = field(default_factory=lambda: datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    @property
    def nachricht(self) -> EmailMessage:
        return email.message_from_bytes(self.roh, policy=email.policy.default)


@dataclass
class FakeOrdner:
    name: str
    merkmale: tuple[str, ...] = ()
    uidvalidity: int = 1
    uidnext: int = 1
    mails: list[FakeMail] = field(default_factory=list)

    def lege_ab(self, roh: bytes, flags: set[str] | None = None, eingang=None) -> FakeMail:
        mail = FakeMail(self.uidnext, roh, set(flags or ()))
        if eingang is not None:
            mail.eingang = eingang
        self.uidnext += 1
        self.mails.append(mail)
        return mail


class FakePostfach:
    def __init__(self, adresse: str, passwort: str, uidvalidity: int = 1) -> None:
        self.adresse = adresse
        self.passwort = passwort
        self.ordner: dict[str, FakeOrdner] = {}
        for name, merkmale in (
            ("INBOX", ()),
            ("Entwürfe", ("\\Drafts",)),
            ("Gesendet", ("\\Sent",)),
            ("Papierkorb", ("\\Trash",)),
            ("Archiv", ()),
        ):
            self.ordner[name] = FakeOrdner(name, merkmale, uidvalidity)

    def lege_ab(self, ordner: str, nachricht, flags: set[str] | None = None, eingang=None):
        roh = nachricht if isinstance(nachricht, bytes) else nachricht.as_bytes()
        return self.ordner[ordner].lege_ab(roh, flags, eingang)


class FakeMailServer:
    def __init__(self) -> None:
        self.postfaecher: dict[str, FakePostfach] = {}
        # Jede IMAP-Befehlszeile; bei LOGIN ohne das Passwort
        self.imap_befehle: list[str] = []
        self.imap_anmeldungen = 0
        self.smtp_anmeldungen = 0
        # (Absender des Umschlags, Empfänger des Umschlags, rohe Nachricht)
        self.gesendet: list[tuple[str, list[str], bytes]] = []
        # So viele kommende IMAP- bzw. SMTP-Verbindungen bricht der Server sofort ab.
        self.imap_ausfaelle = 0
        self.smtp_ausfaelle = 0
        self.imap_verbindungen = 0
        self.faehigkeiten = "IMAP4rev1 UIDPLUS MOVE SPECIAL-USE AUTH=PLAIN"
        self.abgelehnte_empfaenger: set[str] = set()
        # True: Der Server legt gesendete Mails selbst im Ordner „Gesendet“ ab.
        self.legt_gesendete_selbst_ab = False
        # True: Der SMTP-Server lehnt jede Anmeldung ab, auch mit richtigem Passwort.
        self.smtp_lehnt_ab = False
        # SMTP-Ports, die wie bei einer Sperre des Hosters nie antworten (Zeitüberschreitung)
        self.gesperrte_smtp_ports: set[int] = set()
        # SMTP-Ports, auf denen das Zertifikat nicht zum Hostnamen passt
        self.smtp_ports_mit_falschem_zertifikat: set[int] = set()
        # Nimmt Verbindungen an und antwortet nie
        self._schweiger = socket.socket()
        self._schweiger.bind(("127.0.0.1", 0))
        self._schweiger.listen(50)
        self.schweiger_port = self._schweiger.getsockname()[1]
        self._imap = _starte(_ImapHandler, self)
        self._smtp = _starte(_SmtpHandler, self)
        self.imap_port = self._imap.server_address[1]
        self.smtp_port = self._smtp.server_address[1]

    def postfach(self, adresse: str, passwort: str, uidvalidity: int = 1) -> FakePostfach:
        self.postfaecher[adresse] = FakePostfach(adresse, passwort, uidvalidity)
        return self.postfaecher[adresse]

    def stoppe(self) -> None:
        for server in (self._imap, self._smtp):
            server.shutdown()
            server.server_close()
        self._schweiger.close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _starte(handler, zustand: FakeMailServer) -> _Server:
    server = _Server(("127.0.0.1", 0), handler)
    server.zustand = zustand
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}).start()
    return server


class TestNetz:
    """Leitet jede Verbindung auf den lokalen Test-Server um und merkt sich das Ziel."""

    __test__ = False

    def __init__(self, server: FakeMailServer) -> None:
        self.server = server
        self.imap_ziele: list[tuple[str, int]] = []
        # (Host, Port, Verschlüsselung), so wie der Bot sie im Betrieb ansprechen würde
        self.smtp_ziele: list[tuple[str, int, str]] = []

    def imap(self, host: str, port: int):
        self.imap_ziele.append((host, port))
        return MailBoxUnencrypted("127.0.0.1", self.server.imap_port, timeout=5)

    def smtp(self, host: str, port: int, sicherheit: str):
        self.smtp_ziele.append((host, port, sicherheit))
        if port in self.server.smtp_ports_mit_falschem_zertifikat:
            return _FalschesZertifikat()
        gesperrt = port in self.server.gesperrte_smtp_ports
        return aiosmtplib.SMTP(
            hostname="127.0.0.1",
            port=self.server.schweiger_port if gesperrt else self.server.smtp_port,
            use_tls=False,
            start_tls=False,
            timeout=0.3 if gesperrt else 5,
        )


class _FalschesZertifikat:
    """Verhält sich wie ein Server, dessen Zertifikat nicht zum Hostnamen passt."""

    async def connect(self):
        raise ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch"
        )

    def close(self) -> None:
        return None


def baue_mail(
    von: str,
    an: str,
    betreff: str,
    text: str = "",
    *,
    html: str | None = None,
    anhaenge: tuple[tuple[str, str, bytes], ...] = (),
    kopf: dict[str, str] | None = None,
    datum: str = "Thu, 01 Oct 2026 09:00:00 +0200",
) -> EmailMessage:
    nachricht = EmailMessage()
    nachricht["From"] = von
    nachricht["To"] = an
    nachricht["Subject"] = betreff
    nachricht["Date"] = datum
    for name, wert in (kopf or {}).items():
        nachricht[name] = wert
    if "Message-ID" not in nachricht:
        nachricht["Message-ID"] = f"<{abs(hash((von, betreff, text)))}@test.example>"
    nachricht.set_content(text)
    if html is not None:
        nachricht.add_alternative(html, subtype="html")
    for name, typ, daten in anhaenge:
        haupt, _, unter = typ.partition("/")
        nachricht.add_attachment(daten, maintype=haupt, subtype=unter, filename=name)
    return nachricht


# ---------------------------------------------------------------- IMAP


def _zerlege(text: str) -> list:
    """Zerlegt IMAP-Argumente in Atome, Zeichenketten und geschachtelte Listen."""
    stapel: list[list] = [[]]
    i = 0
    while i < len(text):
        zeichen = text[i]
        if zeichen == " ":
            i += 1
        elif zeichen == "(":
            stapel.append([])
            i += 1
        elif zeichen == ")":
            fertig = stapel.pop()
            stapel[-1].append(fertig)
            i += 1
        elif zeichen == '"':
            i += 1
            wert = ""
            while text[i] != '"':
                if text[i] == "\\":
                    i += 1
                wert += text[i]
                i += 1
            i += 1
            stapel[-1].append(_Text(wert))
        else:
            ende = i
            while ende < len(text) and text[ende] not in ' ()"':
                ende += 1
            stapel[-1].append(text[i:ende])
            i = ende
    return stapel[0]


class _Text(str):
    """Eine Zeichenkette in Anführungszeichen (kein Schlüsselwort)."""


def _uid_menge(angabe: str, ordner: FakeOrdner) -> list[FakeMail]:
    hoechste = max((m.uid for m in ordner.mails), default=0)
    gewaehlt: set[int] = set()
    for teil in angabe.split(","):
        von, _, bis = teil.partition(":")
        start = hoechste if von == "*" else int(von)
        ende = start if not bis else (hoechste if bis == "*" else int(bis))
        gewaehlt.update(range(min(start, ende), max(start, ende) + 1))
    return [m for m in ordner.mails if m.uid in gewaehlt]


def _datum(wert: str) -> date:
    tag, monat, jahr = wert.split("-")
    return date(int(jahr), MONATE.index(monat) + 1, int(tag))


def _kopf_text(nachricht: EmailMessage, name: str) -> str:
    return " ".join(str(wert) for wert in nachricht.get_all(name, [])).casefold()


def _body_text(nachricht: EmailMessage) -> str:
    teile = []
    for teil in nachricht.walk():
        if teil.get_content_maintype() == "text":
            try:
                teile.append(str(teil.get_content()))
            except Exception:
                teile.append((teil.get_payload(decode=True) or b"").decode("latin-1"))
    return "\n".join(teile).casefold()


def _passt(mail: FakeMail, ordner: FakeOrdner, kriterien: list) -> bool:
    """Wertet Suchkriterien aus (alle müssen zutreffen)."""
    rest = list(kriterien)
    while rest:
        if not _eines(mail, ordner, rest):
            return False
    return True


def _eines(mail: FakeMail, ordner: FakeOrdner, rest: list) -> bool:
    schluessel = rest.pop(0)
    if isinstance(schluessel, list):
        return _passt(mail, ordner, schluessel)
    nachricht = mail.nachricht
    wort = schluessel.upper()
    if wort == "ALL":
        return True
    if wort in ("SEEN", "UNSEEN"):
        return ("\\Seen" in mail.flags) == (wort == "SEEN")
    if wort in ("FLAGGED", "DELETED", "DRAFT", "ANSWERED"):
        return f"\\{wort.capitalize()}" in mail.flags
    if wort == "NOT":
        return not _eines(mail, ordner, rest)
    if wort == "OR":
        erstes = _eines(mail, ordner, rest)
        zweites = _eines(mail, ordner, rest)
        return erstes or zweites
    if wort in ("FROM", "TO", "CC", "BCC", "SUBJECT"):
        return rest.pop(0).casefold() in _kopf_text(nachricht, wort)
    if wort == "HEADER":
        name, wert = rest.pop(0), rest.pop(0)
        return wert.casefold() in _kopf_text(nachricht, name)
    if wort == "BODY":
        return rest.pop(0).casefold() in _body_text(nachricht)
    if wort == "TEXT":
        gesucht = rest.pop(0).casefold()
        koepfe = " ".join(str(wert) for wert in nachricht.values()).casefold()
        return gesucht in koepfe or gesucht in _body_text(nachricht)
    if wort == "SINCE":
        return mail.eingang.date() >= _datum(rest.pop(0))
    if wort == "BEFORE":
        return mail.eingang.date() < _datum(rest.pop(0))
    if wort == "ON":
        return mail.eingang.date() == _datum(rest.pop(0))
    if wort == "UID":
        return mail in _uid_menge(rest.pop(0), ordner)
    raise ValueError(f"Suchkriterium nicht unterstützt: {schluessel}")


def _struktur(nachricht: EmailMessage) -> str:
    """Vereinfachtes BODYSTRUCTURE: genug, um Anhänge zu erkennen."""
    teile = []
    for teil in nachricht.walk():
        if teil.is_multipart():
            continue
        haupt, unter = teil.get_content_maintype(), teil.get_content_subtype()
        name = teil.get_filename()
        if teil.get_content_disposition() == "attachment":
            teile.append(
                f'("{haupt}" "{unter}" ("name" "{name}") NIL NIL "base64" 10 NIL '
                f'("attachment" ("filename" "{name}")) NIL)'
            )
        else:
            teile.append(f'("{haupt}" "{unter}" ("charset" "utf-8") NIL NIL "7bit" 10 1)')
    return teile[0] if len(teile) == 1 else f'({"".join(teile)} "mixed")'


class _ImapHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.zustand: FakeMailServer = self.server.zustand
        self.postfach: FakePostfach | None = None
        self.ordner: FakeOrdner | None = None
        self.nur_lesen = False

    def sende(self, zeile: str | bytes) -> None:
        daten = zeile if isinstance(zeile, bytes) else zeile.encode("utf-8")
        self.wfile.write(daten + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        self.zustand.imap_verbindungen += 1
        if self.zustand.imap_ausfaelle > 0:
            self.zustand.imap_ausfaelle -= 1
            return
        self.sende("* OK IMAP4rev1 Testserver bereit")
        while True:
            roh = self.rfile.readline()
            if not roh:
                return
            zeile = roh.rstrip(b"\r\n").decode("utf-8", "replace")
            tag, _, rest = zeile.partition(" ")
            befehl, _, argumente = rest.partition(" ")
            befehl = befehl.upper()
            if befehl == "UID":
                unterbefehl, _, argumente = argumente.partition(" ")
                befehl = f"UID {unterbefehl.upper()}"
            self.zustand.imap_befehle.append(
                f"{befehl} <verborgen>" if befehl == "LOGIN" else f"{befehl} {argumente}".strip()
            )
            try:
                if self.fuehre_aus(tag, befehl, argumente) is False:
                    return
            except Exception as exc:
                self.sende(f"{tag} BAD {type(exc).__name__}: {exc}")

    def anmelden(self, tag: str, adresse: str, passwort: str) -> None:
        self.zustand.imap_anmeldungen += 1
        postfach = self.zustand.postfaecher.get(adresse)
        if postfach is None or postfach.passwort != passwort:
            self.sende(f"{tag} NO [AUTHENTICATIONFAILED] Authentication failed.")
            return
        self.postfach = postfach
        self.sende(f"{tag} OK Logged in")

    def hole_ordner(self, angabe: str) -> FakeOrdner:
        name = utf7_decode(_zerlege(angabe)[0].encode()) if angabe else ""
        return self.postfach.ordner[name]

    def fuehre_aus(self, tag: str, befehl: str, argumente: str) -> bool | None:
        if befehl == "CAPABILITY":
            self.sende(f"* CAPABILITY {self.zustand.faehigkeiten}")
            self.sende(f"{tag} OK fertig")
        elif befehl == "LOGIN":
            adresse, passwort = _zerlege(argumente)
            self.anmelden(tag, adresse, passwort)
        elif befehl == "AUTHENTICATE":
            self.sende("+ ")
            teile = base64.b64decode(self.rfile.readline().strip()).split(b"\0")
            self.anmelden(tag, teile[1].decode("utf-8"), teile[2].decode("utf-8"))
        elif befehl == "LOGOUT":
            self.sende("* BYE bis bald")
            self.sende(f"{tag} OK fertig")
            return False
        elif befehl == "NOOP":
            self.sende(f"{tag} OK fertig")
        elif self.postfach is None:
            self.sende(f"{tag} NO nicht angemeldet")
        elif befehl == "LIST":
            for ordner in self.postfach.ordner.values():
                merkmale = " ".join(("\\HasNoChildren", *ordner.merkmale))
                name = utf7_encode(ordner.name).decode()
                self.sende(f'* LIST ({merkmale}) "/" "{name}"')
            self.sende(f"{tag} OK fertig")
        elif befehl == "STATUS":
            ordner = self.hole_ordner(argumente)
            ungelesen = sum("\\Seen" not in m.flags for m in ordner.mails)
            name = utf7_encode(ordner.name).decode()
            self.sende(
                f'* STATUS "{name}" (MESSAGES {len(ordner.mails)} RECENT 0 '
                f"UIDNEXT {ordner.uidnext} UIDVALIDITY {ordner.uidvalidity} UNSEEN {ungelesen})"
            )
            self.sende(f"{tag} OK fertig")
        elif befehl in ("SELECT", "EXAMINE"):
            if utf7_decode(_zerlege(argumente)[0].encode()) not in self.postfach.ordner:
                self.sende(f"{tag} NO Mailbox doesn't exist")
                return None
            self.ordner = self.hole_ordner(argumente)
            self.nur_lesen = befehl == "EXAMINE"
            self.sende(f"* {len(self.ordner.mails)} EXISTS")
            self.sende("* 0 RECENT")
            self.sende(f"* OK [UIDVALIDITY {self.ordner.uidvalidity}] UIDs gültig")
            self.sende(f"{tag} OK [{'READ-ONLY' if self.nur_lesen else 'READ-WRITE'}] fertig")
        elif befehl == "UNSELECT":
            self.ordner = None
            self.sende(f"{tag} OK fertig")
        elif befehl == "APPEND":
            self.haenge_an(tag, argumente)
        elif self.ordner is None:
            self.sende(f"{tag} BAD kein Ordner gewählt")
        elif befehl == "UID SEARCH":
            kriterien = _zerlege(argumente)
            if kriterien and kriterien[0] == "CHARSET":
                kriterien = kriterien[2:]
            uids = [str(m.uid) for m in self.ordner.mails if _passt(m, self.ordner, kriterien)]
            self.sende(f"* SEARCH {' '.join(uids)}".rstrip())
            self.sende(f"{tag} OK fertig")
        elif befehl == "UID FETCH":
            self.hole(tag, argumente)
        elif befehl == "UID STORE":
            menge, art, flags = _zerlege(argumente)
            if self.nur_lesen:
                self.sende(f"{tag} NO Ordner ist nur lesend geöffnet")
                return None
            for mail in _uid_menge(menge, self.ordner):
                if art.upper().startswith("+"):
                    mail.flags |= set(flags)
                else:
                    mail.flags -= set(flags)
                nummer = self.ordner.mails.index(mail) + 1
                self.sende(f"* {nummer} FETCH (UID {mail.uid} FLAGS ({' '.join(mail.flags)}))")
            self.sende(f"{tag} OK fertig")
        elif befehl in ("UID COPY", "UID MOVE"):
            menge, ziel = _zerlege(argumente)
            ziel_ordner = self.postfach.ordner.get(utf7_decode(str(ziel).encode()))
            if ziel_ordner is None:
                self.sende(f"{tag} NO [TRYCREATE] Mailbox doesn't exist")
                return None
            for mail in _uid_menge(menge, self.ordner):
                ziel_ordner.lege_ab(mail.roh, mail.flags, mail.eingang)
                if befehl == "UID MOVE":
                    self.ordner.mails.remove(mail)
            self.sende(f"{tag} OK fertig")
        elif befehl in ("EXPUNGE", "UID EXPUNGE"):
            betroffen = (
                _uid_menge(argumente, self.ordner) if befehl == "UID EXPUNGE" else self.ordner.mails
            )
            for mail in [m for m in betroffen if "\\Deleted" in m.flags]:
                self.ordner.mails.remove(mail)
            self.sende(f"{tag} OK fertig")
        else:
            self.sende(f"{tag} BAD unbekannter Befehl")
        return None

    def haenge_an(self, tag: str, argumente: str) -> None:
        treffer = re.search(r"\{(\d+)\}$", argumente)
        teile = _zerlege(argumente[: treffer.start()])
        name = utf7_decode(str(teile[0]).encode())
        flags = next((set(t) for t in teile[1:] if isinstance(t, list)), set())
        self.sende("+ bereit")
        roh = self.rfile.read(int(treffer.group(1)))
        self.rfile.readline()
        if name not in self.postfach.ordner:
            self.sende(f"{tag} NO [TRYCREATE] Mailbox doesn't exist")
            return
        ordner = self.postfach.ordner[name]
        mail = ordner.lege_ab(roh, flags, datetime.now(UTC))
        self.sende(f"{tag} OK [APPENDUID {ordner.uidvalidity} {mail.uid}] abgelegt")

    def hole(self, tag: str, argumente: str) -> None:
        menge, _, teile = argumente.partition(" ")
        teile = teile.upper()
        for mail in _uid_menge(menge, self.ordner):
            nummer = self.ordner.mails.index(mail) + 1
            if "BODYSTRUCTURE" in teile:
                self.sende(
                    f"* {nummer} FETCH (UID {mail.uid} BODYSTRUCTURE {_struktur(mail.nachricht)})"
                )
                continue
            nur_kopf = "[HEADER]" in teile
            inhalt = mail.roh.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if nur_kopf else mail.roh
            if "BODY[" in teile and not self.nur_lesen:
                # Ohne PEEK markiert ein echter Server die Mail als gelesen.
                mail.flags.add("\\Seen")
            abschnitt = "BODY[HEADER]" if nur_kopf else "BODY[]"
            kopf = (
                f"* {nummer} FETCH (UID {mail.uid} FLAGS ({' '.join(sorted(mail.flags))}) "
                f"RFC822.SIZE {len(mail.roh)} {abschnitt} {{{len(inhalt)}}}"
            )
            self.wfile.write(kopf.encode() + b"\r\n" + inhalt + b")\r\n")
        self.wfile.flush()
        self.sende(f"{tag} OK fertig")


# ---------------------------------------------------------------- SMTP


class _SmtpHandler(socketserver.StreamRequestHandler):
    def sende(self, zeile: str) -> None:
        self.wfile.write(zeile.encode() + b"\r\n")
        self.wfile.flush()

    def anmelden(self, adresse: str, passwort: str) -> None:
        zustand: FakeMailServer = self.server.zustand
        zustand.smtp_anmeldungen += 1
        postfach = zustand.postfaecher.get(adresse)
        if postfach is None or postfach.passwort != passwort or zustand.smtp_lehnt_ab:
            self.sende("535 5.7.8 Authentication failed")
            return
        self.adresse = adresse
        self.sende("235 2.7.0 Authentication successful")

    def handle(self) -> None:
        zustand: FakeMailServer = self.server.zustand
        if zustand.smtp_ausfaelle > 0:
            zustand.smtp_ausfaelle -= 1
            return
        self.adresse = None
        absender, empfaenger = "", []
        self.sende("220 testserver ESMTP")
        while True:
            roh = self.rfile.readline()
            if not roh:
                return
            zeile = roh.rstrip(b"\r\n").decode("utf-8", "replace")
            befehl = zeile.upper()
            if befehl.startswith(("EHLO", "HELO")):
                self.sende("250-testserver")
                self.sende("250-AUTH PLAIN LOGIN")
                self.sende("250 SIZE 30000000")
            elif befehl.startswith("AUTH PLAIN"):
                daten = zeile[10:].strip()
                if not daten:
                    self.sende("334 ")
                    daten = self.rfile.readline().strip().decode()
                teile = base64.b64decode(daten).split(b"\0")
                self.anmelden(teile[1].decode("utf-8"), teile[2].decode("utf-8"))
            elif befehl.startswith("AUTH LOGIN"):
                self.sende("334 VXNlcm5hbWU6")
                adresse = base64.b64decode(self.rfile.readline().strip()).decode("utf-8")
                self.sende("334 UGFzc3dvcmQ6")
                passwort = base64.b64decode(self.rfile.readline().strip()).decode("utf-8")
                self.anmelden(adresse, passwort)
            elif befehl.startswith("MAIL FROM"):
                absender = re.search(r"<([^>]*)>", zeile).group(1)
                if self.adresse is None:
                    self.sende("530 5.7.0 Authentication required")
                elif absender != self.adresse:
                    self.sende("553 5.7.1 Sender address rejected")
                else:
                    empfaenger = []
                    self.sende("250 OK")
            elif befehl.startswith("RCPT TO"):
                adresse = re.search(r"<([^>]*)>", zeile).group(1)
                if adresse in zustand.abgelehnte_empfaenger:
                    self.sende("550 5.1.1 User unknown")
                else:
                    empfaenger.append(adresse)
                    self.sende("250 OK")
            elif befehl == "DATA":
                self.sende("354 Ende mit <CRLF>.<CRLF>")
                zeilen = []
                while (teil := self.rfile.readline()) != b".\r\n":
                    zeilen.append(teil[1:] if teil.startswith(b"..") else teil)
                zustand.gesendet.append((absender, list(empfaenger), b"".join(zeilen)))
                if zustand.legt_gesendete_selbst_ab:
                    zustand.postfaecher[absender].lege_ab("Gesendet", b"".join(zeilen), {"\\Seen"})
                self.sende("250 OK angenommen")
            elif befehl in ("RSET", "NOOP"):
                self.sende("250 OK")
            elif befehl == "QUIT":
                self.sende("221 bis bald")
                return
            else:
                self.sende("502 nicht unterstützt")
