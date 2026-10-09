"""Baut ausgehende Mails: neue Mail, Antwort mit korrekten Kopfzeilen, Weiterleitung.

Absender ist immer das eigene Postfach. Empfänger einer Antwort bestimmt der Server aus der
Vorlage (Reply-To bzw. From), nicht das Modell.
"""

import email.policy
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from app.mail.inhalt import Anhang, Mail
from app.mail.konten import Postfach, ist_adresse
from app.tools.base import ToolFehler

MAX_BETREFF_ZEICHEN = 300
MAX_TEXT_ZEICHEN = 50000
ANTWORT_PRAEFIXE = ("re:", "aw:", "antw:")
WEITER_PRAEFIXE = ("fwd:", "fw:", "wg:")
SIGNATUR_TRENNER = "-- "


@dataclass(frozen=True)
class Empfaenger:
    an: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()

    @property
    def alle(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(a.lower() for a in (*self.an, *self.cc, *self.bcc)))


@dataclass(frozen=True)
class Ausgang:
    """Eine fertig gebaute ausgehende Mail samt dem, was die Vorschau zeigt."""

    nachricht: EmailMessage = field(repr=False)
    empfaenger: Empfaenger
    betreff: str
    text: str
    anhaenge: tuple[Anhang, ...] = ()

    @property
    def message_id(self) -> str:
        return str(self.nachricht["Message-ID"])

    def roh(self, mit_bcc: bool = False) -> bytes:
        """Bytes für die Ablage im Postfach. Die Bcc-Zeile steht nur in der eigenen Kopie."""
        if not mit_bcc or not self.empfaenger.bcc:
            return self.nachricht.as_bytes()
        kopie = email.message_from_bytes(self.nachricht.as_bytes(), policy=email.policy.SMTP)
        kopie["Bcc"] = ", ".join(self.empfaenger.bcc)
        return kopie.as_bytes()


def lies_adressen(wert: object, feld: str) -> tuple[str, ...]:
    """Adressen aus einem Tool-Parameter; jede wird geprüft, Doppelte entfallen."""
    if wert in (None, "", []):
        return ()
    eintraege = [wert] if isinstance(wert, str) else wert
    if not isinstance(eintraege, list | tuple):
        raise ToolFehler(f"{feld} ist eine Liste von E-Mail-Adressen.")
    adressen: list[str] = []
    for eintrag in eintraege:
        adresse = (
            parseaddr(str(eintrag))[1].strip() if "<" in str(eintrag) else str(eintrag).strip()
        )
        if not ist_adresse(adresse):
            raise ToolFehler(f"In {feld} steht keine gültige E-Mail-Adresse. Frag die Person.")
        if adresse.lower() not in (a.lower() for a in adressen):
            adressen.append(adresse)
    return tuple(adressen)


def pruefe_empfaenger(empfaenger: Empfaenger, maximum: int, *, pflicht: bool = True) -> None:
    if pflicht and not empfaenger.an:
        raise ToolFehler("Es fehlt mindestens ein Empfänger im Feld an.")
    if len(empfaenger.alle) > maximum:
        raise ToolFehler(
            f"Eine Mail darf höchstens {maximum} Empfänger haben (An, Cc und Bcc zusammen); "
            f"hier sind es {len(empfaenger.alle)}. Für Massenmails ist Klaviyo da."
        )


def _einzeilig(text: object, maximum: int) -> str:
    return " ".join(str(text or "").split())[:maximum]


def _pruefe_text(text: object) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_TEXT_ZEICHEN:
        raise ToolFehler(f"Der Text einer Mail darf höchstens {MAX_TEXT_ZEICHEN} Zeichen haben.")
    return text


def mit_signatur(text: str, postfach: Postfach) -> str:
    if not postfach.signatur:
        return text
    return f"{text}\n\n{SIGNATUR_TRENNER}\n{postfach.signatur}"


def antwort_betreff(betreff: str) -> str:
    betreff = _einzeilig(betreff, MAX_BETREFF_ZEICHEN)
    return betreff if betreff.lower().startswith(ANTWORT_PRAEFIXE) else f"Re: {betreff}"


def weiter_betreff(betreff: str) -> str:
    betreff = _einzeilig(betreff, MAX_BETREFF_ZEICHEN)
    return betreff if betreff.lower().startswith(WEITER_PRAEFIXE) else f"Fwd: {betreff}"


def antwort_empfaenger(vorlage: Mail, postfach: Postfach, an_alle: bool) -> Empfaenger:
    """An wen eine Antwort geht: Reply-To, sonst From; bei „allen“ dazu An und Cc der
    Vorlage, ohne das eigene Postfach."""
    eigene = postfach.adresse.lower()
    an = [a for a in (vorlage.antwort_adressen or vorlage.von_adressen) if ist_adresse(a)]
    cc: list[str] = []
    if an_alle:
        bekannt = {a.lower() for a in an} | {eigene}
        for adresse in (*vorlage.an_adressen, *vorlage.cc_adressen):
            if ist_adresse(adresse) and adresse.lower() not in bekannt:
                bekannt.add(adresse.lower())
                cc.append(adresse)
    if not an:
        raise ToolFehler("Die Mail hat keinen lesbaren Absender, an den sich antworten ließe.")
    return Empfaenger(tuple(an), tuple(cc))


def zitat(vorlage: Mail) -> str:
    zeilen = "\n".join(f"> {zeile}".rstrip() for zeile in vorlage.text.splitlines())
    return f"Am {vorlage.datum} schrieb {vorlage.von}:\n{zeilen}"


def weiterleitung(vorlage: Mail) -> str:
    kopf = [
        "---------- Weitergeleitete Nachricht ----------",
        f"Von: {vorlage.von}",
        f"Datum: {vorlage.datum}",
        f"Betreff: {vorlage.betreff}",
        f"An: {vorlage.an}",
    ]
    if vorlage.cc:
        kopf.append(f"Cc: {vorlage.cc}")
    return "\n".join(kopf) + f"\n\n{vorlage.text}"


def baue(
    postfach: Postfach,
    empfaenger: Empfaenger,
    betreff: object,
    text: str,
    *,
    antwort_auf: Mail | None = None,
    anhaenge: tuple[Anhang, ...] = (),
) -> Ausgang:
    """Die fertige Mail. Absender ist immer die Adresse des eigenen Postfachs."""
    betreff = _einzeilig(betreff, MAX_BETREFF_ZEICHEN)
    if not betreff:
        raise ToolFehler("Der Betreff fehlt.")
    nachricht = EmailMessage(policy=email.policy.SMTP)
    nachricht["From"] = postfach.adresse
    if empfaenger.an:
        nachricht["To"] = ", ".join(empfaenger.an)
    if empfaenger.cc:
        nachricht["Cc"] = ", ".join(empfaenger.cc)
    nachricht["Subject"] = betreff
    nachricht["Date"] = formatdate(localtime=True)
    nachricht["Message-ID"] = make_msgid(domain=postfach.adresse.rsplit("@", 1)[-1])
    if antwort_auf is not None and antwort_auf.message_id:
        nachricht["In-Reply-To"] = antwort_auf.message_id
        kette = [*antwort_auf.references, antwort_auf.message_id]
        nachricht["References"] = " ".join(dict.fromkeys(kette))
    nachricht.set_content(text, charset="utf-8")
    for anhang in anhaenge:
        haupt, _, unter = (anhang.medientyp or "application/octet-stream").partition("/")
        nachricht.add_attachment(
            anhang.daten,
            maintype=haupt or "application",
            subtype=unter or "octet-stream",
            filename=anhang.name,
        )
    return Ausgang(nachricht, empfaenger, betreff, text, anhaenge)


def neue_mail(postfach: Postfach, empfaenger: Empfaenger, betreff: object, text: object) -> Ausgang:
    return baue(postfach, empfaenger, betreff, mit_signatur(_pruefe_text(text), postfach))


def antwort(
    postfach: Postfach,
    vorlage: Mail,
    text: object,
    *,
    an_alle: bool = False,
    empfaenger: Empfaenger | None = None,
) -> Ausgang:
    koerper = f"{mit_signatur(_pruefe_text(text), postfach)}\n\n{zitat(vorlage)}"
    ziel = empfaenger or antwort_empfaenger(vorlage, postfach, an_alle)
    return baue(postfach, ziel, antwort_betreff(vorlage.betreff), koerper, antwort_auf=vorlage)


def weitergeleitet(
    postfach: Postfach, vorlage: Mail, empfaenger: Empfaenger, text: object
) -> Ausgang:
    einleitung = mit_signatur(_pruefe_text(text), postfach)
    koerper = f"{einleitung}\n\n{weiterleitung(vorlage)}".strip()
    echte = tuple(a for a in vorlage.anhaenge if a.daten)
    return baue(postfach, empfaenger, weiter_betreff(vorlage.betreff), koerper, anhaenge=echte)
