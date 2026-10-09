"""IMAP-Zugriffe auf ein Postfach (synchron; `verbindung.imap_aufruf` führt sie im Thread aus).

Gelesen wird nur mit `BODY.PEEK` und in einem nur lesend geöffneten Ordner, damit keine Mail
ungewollt als gelesen markiert wird. Ganze Ordner werden nie geladen: Gesucht wird auf dem
Server, geholt werden zunächst nur Kopfzeilen.
"""

import re
from dataclasses import dataclass
from datetime import date

from imap_tools import AND, OR, BaseMailBox, Header, MailMessage
from imap_tools.utils import encode_folder

from app.mail.kennung import Kennung
from app.mail.verbindung import MailFehler

EINGANG = "INBOX"
ENTWUERFE = "entwuerfe"
GESENDET = "gesendet"
PAPIERKORB = "papierkorb"
SPAM = "spam"
# Art -> (Special-Use-Merkmal nach RFC 6154, gängige Namen als Rückfall)
BESONDERE_ORDNER: dict[str, tuple[str, tuple[str, ...]]] = {
    ENTWUERFE: ("\\Drafts", ("Drafts", "Entwürfe", "Entwurf", "Draft", "Entwuerfe")),
    GESENDET: (
        "\\Sent",
        (
            "Sent",
            "Gesendet",
            "Sent Items",
            "Sent Messages",
            "Gesendete Elemente",
            "Gesendete Objekte",
        ),
    ),
    PAPIERKORB: (
        "\\Trash",
        ("Trash", "Papierkorb", "Deleted Items", "Deleted Messages", "Gelöschte Elemente"),
    ),
    SPAM: ("\\Junk", ("Junk", "Spam", "Junk-E-Mail")),
}
ORDNER_UNBEKANNT_TEXT = (
    "Diesen Ordner gibt es in dem Postfach nicht. mail_ordner_anzeigen zeigt die vorhandenen."
)
KENNUNG_VERALTET_TEXT = (
    "Diese Kennung gilt nicht mehr, weil der Ordner auf dem Server neu aufgebaut wurde. "
    "Suche die Mail mit mail_suchen neu."
)
MAIL_FEHLT_TEXT = "Diese Mail gibt es in dem Ordner nicht mehr (verschoben oder gelöscht)."
MAX_TREFFER = 25
# So viele der neuesten Treffer werden für den Filter „mit Anhang“ höchstens angesehen.
MAX_KANDIDATEN = 200


class RohMail(MailMessage):
    """Wie `MailMessage`, hält aber zusätzlich die unveränderten Bytes der Mail."""

    def __init__(self, fetch_data: list) -> None:
        super().__init__(fetch_data)
        self.roh: bytes = self._get_message_data_parts(fetch_data)[0]


@dataclass(frozen=True)
class OrdnerInfo:
    name: str
    mails: int
    ungelesen: int
    art: str = ""


@dataclass(frozen=True)
class Gefunden:
    """Eine Mail vom Server: Ort, Merkmale und rohe Bytes (Kopfzeilen oder ganz)."""

    kennung: Kennung
    roh: bytes
    gelesen: bool
    groesse: int = 0
    anhang: bool | None = None


def _art(merkmale: tuple[str, ...], name: str, trenner: str) -> str:
    kurz = name.rsplit(trenner, 1)[-1] if trenner else name
    for art, (merkmal, _) in BESONDERE_ORDNER.items():
        if merkmal.lower() in (m.lower() for m in merkmale):
            return art
    for art, (_, namen) in BESONDERE_ORDNER.items():
        if kurz.lower() in (n.lower() for n in namen):
            return art
    return ""


def _waehlbar(box: BaseMailBox) -> list[tuple[str, str]]:
    """(Name, Art) aller Ordner, die sich öffnen lassen."""
    return [
        (ordner.name, _art(ordner.flags, ordner.name, ordner.delim))
        for ordner in box.folder.list()
        if "\\noselect" not in (m.lower() for m in ordner.flags)
    ]


def ordner_liste(box: BaseMailBox) -> list[OrdnerInfo]:
    liste = []
    for name, art in _waehlbar(box):
        status = box.folder.status(name, ["MESSAGES", "UNSEEN"])
        liste.append(OrdnerInfo(name, status.get("MESSAGES", 0), status.get("UNSEEN", 0), art))
    return liste


def finde_ordner(box: BaseMailBox, art: str) -> str | None:
    """Der Ordner für Entwürfe, Gesendet oder Papierkorb: zuerst über das Special-Use-
    Merkmal, sonst über gängige Namen."""
    ordner = _waehlbar(box)
    merkmal = BESONDERE_ORDNER[art][0].lower()
    for eintrag in box.folder.list():
        if merkmal in (m.lower() for m in eintrag.flags):
            return eintrag.name
    return next((name for name, gefunden in ordner if gefunden == art), None)


def loese_ordner(box: BaseMailBox, angabe: str | None) -> str:
    """Der genaue Name eines Ordners; die Angabe darf in der Schreibweise abweichen."""
    if not angabe or angabe.strip().upper() == EINGANG:
        return EINGANG
    gesucht = angabe.strip().lower()
    ordner = _waehlbar(box)
    for name, art in ordner:
        if name.lower() == gesucht or art == gesucht:
            return name
    # „Gesendet“ oder „Entwürfe“ meinen den jeweiligen besonderen Ordner, wie er auch heißt.
    for art, (_, namen) in BESONDERE_ORDNER.items():
        if gesucht in (n.lower() for n in namen):
            treffer = next((name for name, gefunden in ordner if gefunden == art), None)
            if treffer:
                return treffer
    raise MailFehler(ORDNER_UNBEKANNT_TEXT)


def oeffne(box: BaseMailBox, ordner: str, *, nur_lesen: bool = True) -> int:
    """Öffnet den Ordner und liefert seine UIDVALIDITY."""
    box.email_message_class = RohMail
    box.folder.set(ordner, readonly=nur_lesen)
    return box.folder.status(ordner, ["UIDVALIDITY"]).get("UIDVALIDITY", 0)


def oeffne_fuer(box: BaseMailBox, kennung: Kennung, *, nur_lesen: bool = True) -> None:
    if oeffne(box, kennung.ordner, nur_lesen=nur_lesen) != kennung.uidvalidity:
        raise MailFehler(KENNUNG_VERALTET_TEXT)


def kriterien(
    *,
    absender: str = "",
    empfaenger: str = "",
    betreff: str = "",
    text: str = "",
    seit: date | None = None,
    bis: date | None = None,
    nur_ungelesen: bool = False,
) -> tuple[str, str]:
    """Suchkriterien für den Server und der Zeichensatz, in dem sie stehen."""
    werte: dict[str, object] = {}
    if absender:
        werte["from_"] = absender
    if empfaenger:
        werte["to"] = empfaenger
    if betreff:
        werte["subject"] = betreff
    if text:
        werte["text"] = text
    if seit:
        werte["date_gte"] = seit
    if bis:
        werte["date_lt"] = bis
    if nur_ungelesen:
        werte["seen"] = False
    suche = str(AND(**werte)) if werte else "ALL"
    return suche, "US-ASCII" if suche.isascii() else "UTF-8"


def _hat_anhang(box: BaseMailBox, uids: list[str]) -> dict[str, bool]:
    """Liest die Struktur der Mails (ohne Inhalt) und erkennt daran Anhänge."""
    if not uids:
        return {}
    typ, daten = box.client.uid("FETCH", ",".join(uids), "(UID BODYSTRUCTURE)")
    ergebnis: dict[str, bool] = {}
    if typ != "OK":
        return ergebnis
    for eintrag in daten or ():
        zeile = eintrag[0] if isinstance(eintrag, tuple) else eintrag
        if not isinstance(zeile, bytes):
            continue
        treffer = re.search(rb"UID (\d+)", zeile)
        if treffer:
            klein = zeile.lower()
            ergebnis[treffer.group(1).decode()] = b'"attachment"' in klein or b'"filename' in klein
    return ergebnis


def _hole(box: BaseMailBox, uids: list[str], *, nur_kopf: bool) -> dict[str, RohMail]:
    if not uids:
        return {}
    mails = box.fetch(uid_list=uids, mark_seen=False, headers_only=nur_kopf, bulk=True)
    return {mail.uid: mail for mail in mails if mail.uid}


def suche(
    box: BaseMailBox,
    ordner: str,
    suchtext: str,
    zeichensatz: str,
    *,
    mit_anhang: bool = False,
    maximum: int = MAX_TREFFER,
) -> tuple[list[Gefunden], int, bool]:
    """Sucht auf dem Server und holt von den neuesten Treffern nur die Kopfzeilen.

    Liefert (Treffer, Gesamtzahl, unvollständig). `unvollständig` ist True, wenn für den
    Filter „mit Anhang“ nicht alle Treffer angesehen wurden.
    """
    uidvalidity = oeffne(box, ordner)
    alle = box.uids(suchtext, zeichensatz)
    neueste = sorted(alle, key=int, reverse=True)
    unvollstaendig = False
    if mit_anhang:
        unvollstaendig = len(neueste) > MAX_KANDIDATEN
        kandidaten = neueste[:MAX_KANDIDATEN]
        mit = _hat_anhang(box, kandidaten)
        neueste = [uid for uid in kandidaten if mit.get(uid)]
        anhaenge = dict.fromkeys(neueste, True)
        gesamt = len(neueste)
    else:
        gesamt = len(neueste)
        anhaenge = _hat_anhang(box, neueste[:maximum])
    gewaehlt = neueste[:maximum]
    geholt = _hole(box, gewaehlt, nur_kopf=True)
    treffer = [
        Gefunden(
            Kennung(ordner, uidvalidity, int(uid)),
            geholt[uid].roh,
            "\\Seen" in geholt[uid].flags,
            geholt[uid].size_rfc822,
            anhaenge.get(uid),
        )
        for uid in gewaehlt
        if uid in geholt
    ]
    return treffer, gesamt, unvollstaendig


def hole_mail(box: BaseMailBox, kennung: Kennung, *, nur_kopf: bool = False) -> Gefunden:
    """Holt genau eine Mail. Das Gelesen-Merkmal bleibt, wie es ist."""
    oeffne_fuer(box, kennung)
    geholt = _hole(box, [str(kennung.uid)], nur_kopf=nur_kopf)
    mail = geholt.get(str(kennung.uid))
    if mail is None or not mail.roh:
        raise MailFehler(MAIL_FEHLT_TEXT)
    return Gefunden(kennung, mail.roh, "\\Seen" in mail.flags, mail.size_rfc822)


def suche_gespraech(
    box: BaseMailBox, ordner: list[str], ids: list[str], maximum: int
) -> list[Gefunden]:
    """Mails, die über Message-ID, In-Reply-To oder References zu denselben IDs gehören."""
    if not ids:
        return []
    koepfe = [
        Header(name, wert) for wert in ids for name in ("Message-ID", "References", "In-Reply-To")
    ]
    suchtext = str(OR(header=koepfe))
    gefunden: list[Gefunden] = []
    for name in ordner:
        try:
            uidvalidity = oeffne(box, name)
        except Exception:
            continue
        uids = sorted(box.uids(suchtext, "US-ASCII" if suchtext.isascii() else "UTF-8"), key=int)
        geholt = _hole(box, uids[-maximum:], nur_kopf=True)
        gefunden += [
            Gefunden(
                Kennung(name, uidvalidity, int(uid)),
                mail.roh,
                "\\Seen" in mail.flags,
                mail.size_rfc822,
            )
            for uid, mail in geholt.items()
        ]
    return gefunden


def kodiere_ordner(name: str) -> bytes:
    return encode_folder(name)
