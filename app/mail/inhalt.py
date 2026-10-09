"""Zerlegt eine Mail mit Pythons `email`-Paket (`policy=email.policy.default`).

Mails aus der Praxis sind oft fehlerhaft: falsche Zeichensätze, kaputte Kopfzeilen, HTML ohne
Textteil. Nichts hier bricht deshalb mit einer Ausnahme ab; im Zweifel gibt es eine lesbare
Ersatzdarstellung.
"""

import email
import email.policy
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

ERSATZ_BETREFF = "(ohne Betreff)"
UNLESBAR = "(nicht lesbar)"

_UNSICHTBAR = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(px|pt|em|%)?\s*(;|$)"
    r"|opacity\s*:\s*0(\.0+)?\s*(;|$)",
    re.IGNORECASE,
)
UNSICHTBAR_MARKE = "[im Original unsichtbarer Text:]"
# Inhalt dieser Elemente wird verworfen (Skripte, Stile, Kopfbereich).
_VERWERFEN = {"script", "style", "head", "title", "noscript", "template", "svg", "object"}
_LEER = {"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "embed", "source"}
_BLOCK = {
    "p",
    "div",
    "tr",
    "table",
    "ul",
    "ol",
    "blockquote",
    "section",
    "article",
    "header",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
}


class _HtmlZuText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.teile: list[str] = []
        self._verwerfen = 0
        self._links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._verwerfen:
            if tag not in _LEER:
                self._verwerfen += 1
            return
        if tag in _VERWERFEN:
            self._verwerfen = 1
            return
        werte = dict(attrs)
        if tag == "br":
            self.teile.append("\n")
        elif tag == "li":
            self.teile.append("\n- ")
        elif tag in _BLOCK or tag == "hr":
            self.teile.append("\n")
        elif tag in ("td", "th"):
            self.teile.append(" ")
        elif tag == "a":
            self._links.append((werte.get("href") or "").strip())
        # Bilder werden ganz verworfen, damit auch Zählpixel.
        if tag not in _LEER and _UNSICHTBAR.search(werte.get("style") or ""):
            self.teile.append(f" {UNSICHTBAR_MARKE} ")

    def handle_endtag(self, tag: str) -> None:
        if self._verwerfen:
            self._verwerfen -= 1
            return
        if tag == "a" and self._links:
            ziel = self._links.pop()
            if ziel.lower().startswith(("http://", "https://", "mailto:")):
                self.teile.append(f" ({ziel})")
        elif tag in _BLOCK:
            self.teile.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._verwerfen:
            self.teile.append(data)


def _glaette(text: str) -> str:
    zeilen = [re.sub(r"[ \t\xa0​‌‍﻿]+", " ", z).strip() for z in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(zeilen)).strip()


def html_zu_text(html: str) -> str:
    """Lesbarer Text aus HTML. Skripte, Stile und Bilder (damit auch Zählpixel) fallen weg;
    Text, der im Original unsichtbar gemacht wurde, bleibt sichtbar und wird markiert."""
    parser = _HtmlZuText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return _glaette(re.sub(r"<[^>]{0,2000}>", " ", html))
    return _glaette("".join(parser.teile))


@dataclass(frozen=True)
class Anhang:
    name: str
    medientyp: str
    groesse: int
    daten: bytes = field(repr=False)
    eingebettet: bool = False


@dataclass(frozen=True)
class Mail:
    betreff: str
    von: str
    an: str
    cc: str
    antwort_an: str
    datum: str
    message_id: str
    in_reply_to: str
    references: tuple[str, ...]
    von_adressen: tuple[str, ...]
    an_adressen: tuple[str, ...]
    cc_adressen: tuple[str, ...]
    antwort_adressen: tuple[str, ...]
    zeitpunkt: datetime | None = None
    text: str = ""
    anhaenge: tuple[Anhang, ...] = ()
    nachricht: EmailMessage | None = field(default=None, repr=False, compare=False)

    @property
    def antwortweg(self) -> set[str]:
        """Adressen, an die eine Antwort auf diese Mail üblicherweise geht."""
        return {
            adresse.lower()
            for adresse in (
                *self.von_adressen,
                *self.antwort_adressen,
                *self.an_adressen,
                *self.cc_adressen,
            )
        }


def _entschluessle_kopf(roh: str) -> str:
    """Ersatzweg für Kopfzeilen, die das Paket nicht lesen kann."""
    teile = []
    try:
        for wert, zeichensatz in decode_header(roh):
            if isinstance(wert, bytes):
                try:
                    teile.append(wert.decode(zeichensatz or "utf-8", "replace"))
                except LookupError:
                    teile.append(wert.decode("latin-1", "replace"))
            else:
                teile.append(wert)
    except Exception:
        return roh
    return "".join(teile)


def _kopf(nachricht: EmailMessage, name: str) -> str:
    try:
        wert = nachricht.get(name)
        text = "" if wert is None else str(wert)
    except Exception:
        try:
            roh = next((w for n, w in nachricht.raw_items() if n.lower() == name.lower()), "")
            text = _entschluessle_kopf(str(roh))
        except Exception:
            text = UNLESBAR
    if "=?" in text:
        text = _entschluessle_kopf(text)
    return " ".join(text.replace("�", "?").split())


def _adressen(nachricht: EmailMessage, name: str) -> tuple[str, ...]:
    try:
        gefunden = [
            adresse.addr_spec
            for kopf in nachricht.get_all(name, [])
            for adresse in getattr(kopf, "addresses", ())
        ]
    except Exception:
        gefunden = []
    if not gefunden:
        try:
            roh = [str(w) for n, w in nachricht.raw_items() if n.lower() == name.lower()]
            gefunden = [adresse for _, adresse in getaddresses(roh)]
        except Exception:
            gefunden = []
    return tuple(dict.fromkeys(a.strip() for a in gefunden if "@" in a))


def _ids(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"<[^<>\s]+>", text)))


def _zeit(nachricht: EmailMessage, zone: str) -> tuple[str, datetime | None]:
    roh = _kopf(nachricht, "Date")
    try:
        zeitpunkt = parsedate_to_datetime(roh)
        if zeitpunkt.tzinfo is None:
            return f"{zeitpunkt:%d.%m.%Y %H:%M}", zeitpunkt
        ort = zeitpunkt.astimezone(ZoneInfo(zone))
        return f"{ort:%d.%m.%Y %H:%M}", zeitpunkt
    except Exception:
        return roh or "(ohne Datum)", None


def _inhalt(teil: EmailMessage) -> str:
    """Text eines Teils; unbekannte oder falsche Zeichensätze führen nie zu einer Ausnahme."""
    try:
        inhalt = teil.get_content()
        if isinstance(inhalt, str):
            return inhalt
    except Exception:
        pass
    try:
        roh = teil.get_payload(decode=True) or b""
    except Exception:
        return UNLESBAR
    for zeichensatz in (teil.get_content_charset(), "utf-8"):
        try:
            return roh.decode(zeichensatz) if zeichensatz else roh.decode("utf-8")
        except (LookupError, UnicodeDecodeError):
            continue
    return roh.decode("latin-1", "replace")


def _text(nachricht: EmailMessage) -> str:
    for art in ("plain", "html"):
        try:
            teil = nachricht.get_body(preferencelist=(art,))
        except Exception:
            teil = None
        if teil is None:
            continue
        inhalt = _inhalt(teil)
        text = html_zu_text(inhalt) if art == "html" else _glaette(inhalt)
        if text:
            return text
    if not nachricht.is_multipart() and nachricht.get_content_maintype() == "text":
        return _glaette(_inhalt(nachricht))
    return ""


def _anhaenge(nachricht: EmailMessage) -> tuple[Anhang, ...]:
    try:
        teile = list(nachricht.iter_attachments())
    except Exception:
        teile = []
    anhaenge = []
    for nummer, teil in enumerate(teile, 1):
        try:
            if teil.get_content_maintype() == "message":
                inhalt = teil.get_payload()
                eingebettete = inhalt[0] if isinstance(inhalt, list) and inhalt else teil
                daten = eingebettete.as_bytes()
            else:
                daten = teil.get_payload(decode=True) or b""
            name = teil.get_filename() or f"anhang_{nummer}"
            anhaenge.append(
                Anhang(
                    name=" ".join(str(name).split())[:200],
                    medientyp=teil.get_content_type(),
                    groesse=len(daten),
                    daten=daten,
                    eingebettet=teil.get_content_disposition() == "inline",
                )
            )
        except Exception:
            anhaenge.append(Anhang(f"anhang_{nummer}", "application/octet-stream", 0, b""))
    return tuple(anhaenge)


def zerlege(roh: bytes, *, nur_kopf: bool = False, zone: str = "Europe/Berlin") -> Mail:
    """Macht aus den rohen Bytes eine Mail. `nur_kopf`: Es liegen nur die Kopfzeilen vor."""
    try:
        nachricht = email.message_from_bytes(roh, policy=email.policy.default)
    except Exception:
        nachricht = EmailMessage()
    datum, zeitpunkt = _zeit(nachricht, zone)
    return Mail(
        betreff=_kopf(nachricht, "Subject") or ERSATZ_BETREFF,
        von=_kopf(nachricht, "From") or "(ohne Absender)",
        an=_kopf(nachricht, "To"),
        cc=_kopf(nachricht, "Cc"),
        antwort_an=_kopf(nachricht, "Reply-To"),
        datum=datum,
        zeitpunkt=zeitpunkt,
        message_id=next(iter(_ids(_kopf(nachricht, "Message-ID"))), ""),
        in_reply_to=next(iter(_ids(_kopf(nachricht, "In-Reply-To"))), ""),
        references=_ids(_kopf(nachricht, "References")),
        von_adressen=_adressen(nachricht, "From"),
        an_adressen=_adressen(nachricht, "To"),
        cc_adressen=_adressen(nachricht, "Cc"),
        antwort_adressen=_adressen(nachricht, "Reply-To"),
        text="" if nur_kopf else _text(nachricht),
        anhaenge=() if nur_kopf else _anhaenge(nachricht),
        nachricht=nachricht,
    )
