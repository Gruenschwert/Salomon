"""Schreib-Tools für die eigenen Mails. Jedes läuft nur nach Freigabe mit Vorschau.

Löschen gibt es nicht. Absender ist immer das eigene Postfach. Die Vorschau nennt, woher die
Aktion kommt, und markiert Empfänger, die weder im Antwortweg einer gelesenen Mail stehen noch
von der Person selbst genannt wurden.
"""

from typing import ClassVar

from app.mail import imap
from app.mail import nachricht as bau
from app.mail.inhalt import Mail, zerlege
from app.mail.kennung import Kennung
from app.mail.konten import Postfach
from app.mail.lauf import aktueller_mail_lauf
from app.mail.limits import AKTION_GESENDET, pruefe_tageslimit
from app.mail.nachricht import Ausgang, Empfaenger
from app.mail.schutz import NEUER_EMPFAENGER, herkunft_zeilen
from app.mail.verbindung import imap_aufruf, smtp_sende
from app.mail.werkzeug import KONTO_SCHEMA, MailSchreibTool
from app.medien import groesse_text
from app.tools.base import ToolFehler, aktueller_nutzer
from app.tools.mail_lesen import KENNUNG_SCHEMA

ADRESSEN = {"type": "array", "items": {"type": "string"}}
AN_SCHEMA = {**ADRESSEN, "description": "E-Mail-Adressen der Empfänger."}
CC_SCHEMA = {**ADRESSEN, "description": "E-Mail-Adressen in Kopie (optional)."}
BCC_SCHEMA = {**ADRESSEN, "description": "E-Mail-Adressen in Blindkopie (optional)."}
TEXT_SCHEMA = {
    "type": "string",
    "description": "Der vollständige Text als reiner Text, ohne Markdown und ohne Signatur.",
}
NEU_ERKLAERUNG = "(weder im Antwortweg einer gelesenen Mail noch von dir genannt)"
ERST_ENTWURF = (
    " Vorher muss die Person den Entwurf im Chat gesehen und ausdrücklich zugestimmt haben."
)


def _neue_empfaenger(postfach: Postfach, empfaenger: Empfaenger, bekannte: set[str]) -> list[str]:
    """Empfänger, die weder im Antwortweg einer gelesenen Mail stehen noch von der Person
    selbst genannt wurden."""
    lauf = aktueller_mail_lauf.get()
    neue = []
    for adresse in (*empfaenger.an, *empfaenger.cc, *empfaenger.bcc):
        klein = adresse.lower()
        if klein == postfach.adresse.lower() or klein in bekannte:
            continue
        if lauf is not None and (klein in lauf.antwortweg or lauf.hat_selbst_genannt(klein)):
            continue
        if adresse not in neue:
            neue.append(adresse)
    return neue


def _vorschau(
    titel: str,
    postfach: Postfach,
    ausgang: Ausgang,
    bekannte: set[str],
    zusatz: tuple[str, ...] = (),
) -> str:
    """Vollständige Vorschau. Alles, was vom System stammt (Herkunft, Markierungen), steht
    oben; der Text der Mail steht als Letztes und kann diese Zeilen nicht nachahmen."""
    empfaenger = ausgang.empfaenger
    zeilen = [
        f"{titel} aus dem Postfach „{postfach.label}“",
        *herkunft_zeilen(aktueller_mail_lauf.get()),
    ]
    zeilen += [
        f"{NEUER_EMPFAENGER} {adresse} {NEU_ERKLAERUNG}"
        for adresse in _neue_empfaenger(postfach, empfaenger, bekannte)
    ]
    zeilen.append(f"Von: {postfach.adresse}")
    zeilen.append(f"An: {', '.join(empfaenger.an) or '(noch offen)'}")
    if empfaenger.cc:
        zeilen.append(f"Cc: {', '.join(empfaenger.cc)}")
    if empfaenger.bcc:
        zeilen.append(f"Bcc: {', '.join(empfaenger.bcc)}")
    zeilen.append(f"Betreff: {ausgang.betreff}")
    if ausgang.anhaenge:
        dateien = ", ".join(f"{a.name} ({groesse_text(a.groesse)})" for a in ausgang.anhaenge)
        zeilen.append(f"Anhänge: {dateien}")
    else:
        zeilen.append("Anhänge: keine")
    zeilen += zusatz
    zeilen += ["Text (ab hier bis zum Ende der Vorschau):", ausgang.text]
    return "\n".join(zeilen)


def _alle_adressen(empfaenger: Empfaenger) -> list[str]:
    eindeutig: dict[str, str] = {}
    for adresse in (*empfaenger.an, *empfaenger.cc, *empfaenger.bcc):
        eindeutig.setdefault(adresse.lower(), adresse)
    return list(eindeutig.values())


def _lies_empfaenger(an: object, cc: object, bcc: object) -> Empfaenger:
    return Empfaenger(
        bau.lies_adressen(an, "an"), bau.lies_adressen(cc, "cc"), bau.lies_adressen(bcc, "bcc")
    )


class _MitVorlage(MailSchreibTool):
    """Hilfen für Tools, die sich auf eine vorhandene Mail beziehen."""

    async def hole_vorlage(self, postfach: Postfach, kennung: object) -> tuple[Kennung, Mail]:
        ort = self.lies_kennung(postfach, kennung)
        gefunden = await imap_aufruf(self.kontext, postfach, lambda box: imap.hole_mail(box, ort))
        vorlage = zerlege(gefunden.roh, zone=self.kontext.settings.tz)
        if (lauf := aktueller_mail_lauf.get()) is not None:
            # Die Vorlage ist Inhalt von außen und steht als Zitat in der neuen Mail.
            lauf.merke_gelesen(vorlage.von, vorlage.antwortweg)
        return ort, vorlage


class _Sendend(_MitVorlage):
    """Gemeinsamer Ablauf aller Tools, die über SMTP versenden."""

    aktion = AKTION_GESENDET

    async def baue_ausgang(
        self, postfach: Postfach, **params
    ) -> tuple[Ausgang, set[str], tuple[str, ...]]:
        """Liefert die fertige Mail, die schon bekannten Empfänger und Zusatzzeilen."""
        raise NotImplementedError

    async def _pruefe(self, postfach: Postfach, **params):
        await pruefe_tageslimit(self.kontext, aktueller_nutzer.get())
        ausgang, bekannte, zusatz = await self.baue_ausgang(postfach, **params)
        bau.pruefe_empfaenger(ausgang.empfaenger, self.kontext.settings.mail_max_empfaenger)
        return ausgang, bekannte, zusatz

    async def vorschau_fuer(self, postfach: Postfach, **params) -> str:
        ausgang, bekannte, zusatz = await self._pruefe(postfach, **params)
        return _vorschau(self.titel, postfach, ausgang, bekannte, zusatz)

    async def arbeite(self, postfach: Postfach, **params) -> dict:
        # Limits und Empfänger werden unmittelbar vor dem Senden noch einmal geprüft.
        ausgang, _, _ = await self._pruefe(postfach, **params)
        adressen = _alle_adressen(ausgang.empfaenger)
        await smtp_sende(self.kontext, postfach, ausgang.nachricht, adressen)
        ergebnis = {
            "gesendet": True,
            "empfaenger": len(adressen),
            "message_id": ausgang.message_id,
        }
        try:
            # Kopie in „Gesendet“, sofern der Server sie nicht schon selbst abgelegt hat.
            ergebnis["kopie_in"] = await imap_aufruf(
                self.kontext,
                postfach,
                lambda box: imap.lege_gesendete_ab(
                    box, ausgang.roh(mit_bcc=True), ausgang.message_id
                ),
                wiederholen=False,
            )
        except ToolFehler:
            ergebnis["kopie_in"] = None
        return ergebnis

    def audit_zusatz(self, daten: dict) -> dict:
        return {"empfaenger": daten.get("empfaenger"), "message_id": daten.get("message_id")}

    def ergebnis_text(self, ergebnis: dict) -> str:
        anzahl = ergebnis.get("empfaenger", 0)
        wort = "einen Empfänger" if anzahl == 1 else f"{anzahl} Empfänger"
        text = f"✅ Mail gesendet aus dem Postfach „{ergebnis.get('konto')}“ an {wort}."
        if ergebnis.get("kopie_in"):
            return f"{text} Eine Kopie liegt im Ordner „{ergebnis['kopie_in']}“."
        return f"{text} Die Kopie im Ordner für gesendete Mails konnte ich nicht ablegen."


class MailSenden(_Sendend):
    name = "mail_senden"
    titel = "Mail senden"
    beschreibung = (
        "Versendet eine neue Mail aus dem eigenen Postfach (für Antworten gibt es "
        "mail_antworten). Läuft erst nach Freigabe mit vollständiger Vorschau." + ERST_ENTWURF
    )
    parameter_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "konto": KONTO_SCHEMA,
            "an": AN_SCHEMA,
            "cc": CC_SCHEMA,
            "bcc": BCC_SCHEMA,
            "betreff": {"type": "string"},
            "text": TEXT_SCHEMA,
        },
        "required": ["an", "betreff", "text"],
    }

    async def baue_ausgang(
        self, postfach: Postfach, an=None, cc=None, bcc=None, betreff="", text="", **_: object
    ):
        ausgang = bau.neue_mail(postfach, _lies_empfaenger(an, cc, bcc), betreff, text)
        return ausgang, set(), ()


class MailAntworten(_Sendend):
    name = "mail_antworten"
    titel = "Antwort senden"
    beschreibung = (
        "Antwortet auf eine Mail im eigenen Postfach: Empfänger aus Reply-To bzw. Absender der "
        "Vorlage, mit an_alle zusätzlich deren weitere Empfänger in Kopie; Betreff mit „Re:“, "
        "Zitat der Vorlage und die Kopfzeilen In-Reply-To und References setzt das System. "
        "Läuft erst nach Freigabe mit vollständiger Vorschau." + ERST_ENTWURF
    )
    parameter_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "konto": KONTO_SCHEMA,
            "kennung": KENNUNG_SCHEMA,
            "text": TEXT_SCHEMA,
            "an_alle": {
                "type": "boolean",
                "description": "Auch die weiteren Empfänger der Vorlage in Kopie setzen.",
            },
        },
        "required": ["kennung", "text"],
    }

    async def baue_ausgang(
        self, postfach: Postfach, kennung="", text="", an_alle=False, **_: object
    ):
        _, vorlage = await self.hole_vorlage(postfach, kennung)
        ausgang = bau.antwort(postfach, vorlage, text, an_alle=bool(an_alle))
        zusatz = (f"Antwort auf: Mail von {vorlage.von} vom {vorlage.datum}",)
        # Der Antwortweg genau dieser Mail gilt als bekannt.
        return ausgang, set(vorlage.antwortweg), zusatz


class MailWeiterleiten(_Sendend):
    name = "mail_weiterleiten"
    titel = "Mail weiterleiten"
    beschreibung = (
        "Leitet eine Mail aus dem eigenen Postfach samt ihren Anhängen an andere weiter. Läuft "
        "erst nach Freigabe mit vollständiger Vorschau. Nur wenn die Person das Weiterleiten "
        "und die Empfänger in ihrer eigenen Nachricht verlangt hat."
    )
    parameter_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "konto": KONTO_SCHEMA,
            "kennung": KENNUNG_SCHEMA,
            "an": AN_SCHEMA,
            "cc": CC_SCHEMA,
            "text": {
                "type": "string",
                "description": "Einleitender Text vor der weitergeleiteten Mail (optional).",
            },
        },
        "required": ["kennung", "an"],
    }

    async def baue_ausgang(
        self, postfach: Postfach, kennung="", an=None, cc=None, text="", **_: object
    ):
        _, vorlage = await self.hole_vorlage(postfach, kennung)
        ausgang = bau.weitergeleitet(postfach, vorlage, _lies_empfaenger(an, cc, None), text)
        zusatz = (f"Weitergeleitet wird: Mail von {vorlage.von} vom {vorlage.datum}",)
        # Beim Weiterleiten gilt kein Antwortweg als bekannt: Wer die Mail bekommt, muss die
        # Person selbst genannt haben.
        return ausgang, set(), zusatz


class MailEntwurfSpeichern(_MitVorlage):
    name = "mail_entwurf_speichern"
    titel = "Entwurf ablegen"
    aktion = "Entwurf"
    beschreibung = (
        "Legt einen Entwurf im Ordner „Entwürfe“ des eigenen Postfachs ab. Er wird nicht "
        "versendet; die Person kann ihn im Mailprogramm weiterbearbeiten. Mit antwort_auf wird "
        "es der Entwurf einer Antwort auf diese Mail. Braucht eine einfache Bestätigung."
    )
    parameter_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "konto": KONTO_SCHEMA,
            "an": AN_SCHEMA,
            "cc": CC_SCHEMA,
            "bcc": BCC_SCHEMA,
            "betreff": {"type": "string", "description": "Entfällt bei antwort_auf."},
            "text": TEXT_SCHEMA,
            "antwort_auf": {
                **KENNUNG_SCHEMA,
                "description": "Optional: Kennung der Mail, auf die der Entwurf antwortet.",
            },
        },
        "required": ["text"],
    }

    async def _baue(
        self,
        postfach: Postfach,
        an=None,
        cc=None,
        bcc=None,
        betreff="",
        text="",
        antwort_auf=None,
        **_: object,
    ) -> tuple[Ausgang, set[str], tuple[str, ...]]:
        empfaenger = _lies_empfaenger(an, cc, bcc)
        if antwort_auf:
            _, vorlage = await self.hole_vorlage(postfach, antwort_auf)
            eigene = empfaenger if empfaenger.alle else None
            ausgang = bau.antwort(postfach, vorlage, text, empfaenger=eigene)
            zusatz = (f"Antwort auf: Mail von {vorlage.von} vom {vorlage.datum}",)
            bekannte = set(vorlage.antwortweg)
        else:
            ausgang, bekannte, zusatz = (
                bau.neue_mail(postfach, empfaenger, betreff, text),
                set(),
                (),
            )
        bau.pruefe_empfaenger(
            ausgang.empfaenger, self.kontext.settings.mail_max_empfaenger, pflicht=False
        )
        return ausgang, bekannte, zusatz

    async def vorschau_fuer(self, postfach: Postfach, **params) -> str:
        ausgang, bekannte, zusatz = await self._baue(postfach, **params)
        zusatz = (*zusatz, "Der Entwurf wird nur abgelegt und nicht versendet.")
        return _vorschau(self.titel, postfach, ausgang, bekannte, zusatz)

    async def arbeite(self, postfach: Postfach, **params) -> dict:
        ausgang, _, _ = await self._baue(postfach, **params)
        ordner = await imap_aufruf(
            self.kontext,
            postfach,
            lambda box: imap.lege_ab(
                box, imap.ENTWUERFE, ausgang.roh(mit_bcc=True), ("\\Draft", "\\Seen")
            ),
            wiederholen=False,
        )
        return {"entwurf": True, "ordner": ordner}

    def ergebnis_text(self, ergebnis: dict) -> str:
        return (
            f"✅ Entwurf abgelegt im Ordner „{ergebnis.get('ordner')}“ des Postfachs "
            f"„{ergebnis.get('konto')}“. Versendet wurde nichts."
        )


class MailVerschieben(_MitVorlage):
    name = "mail_verschieben"
    titel = "Mail verschieben oder markieren"
    aktion = "verschoben oder markiert"
    beschreibung = (
        "Verschiebt eine Mail im eigenen Postfach in einen anderen Ordner und/oder markiert "
        "sie als gelesen oder ungelesen. Löschen und der Papierkorb sind nicht möglich. Läuft "
        "erst nach Freigabe."
    )
    parameter_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "konto": KONTO_SCHEMA,
            "kennung": KENNUNG_SCHEMA,
            "ziel_ordner": {"type": "string", "description": "Name des Zielordners (optional)."},
            "markieren": {"type": "string", "enum": ["gelesen", "ungelesen"]},
        },
        "required": ["kennung"],
    }

    @staticmethod
    def _pruefe_wunsch(ziel_ordner: object, markieren: object) -> None:
        if not ziel_ordner and not markieren:
            raise ToolFehler("Gib ziel_ordner oder markieren an (oder beides).")
        if markieren not in (None, "", "gelesen", "ungelesen"):
            raise ToolFehler("markieren ist „gelesen“ oder „ungelesen“.")

    async def vorschau_fuer(
        self, postfach: Postfach, kennung="", ziel_ordner=None, markieren=None, **_: object
    ) -> str:
        self._pruefe_wunsch(ziel_ordner, markieren)
        ort, vorlage = await self.hole_vorlage(postfach, kennung)
        zeilen = [
            f"{self.titel} im Postfach „{postfach.label}“",
            *herkunft_zeilen(aktueller_mail_lauf.get()),
            f"Mail: „{vorlage.betreff}“ von {vorlage.von} vom {vorlage.datum}",
            f"Liegt in: {ort.ordner}",
        ]
        if ziel_ordner:
            ziel = await imap_aufruf(
                self.kontext, postfach, lambda box: imap.pruefe_ziel(box, str(ziel_ordner))
            )
            zeilen.append(f"Verschieben nach: {ziel}")
        if markieren:
            zeilen.append(f"Markieren als: {markieren}")
        return "\n".join(zeilen)

    async def arbeite(
        self, postfach: Postfach, kennung="", ziel_ordner=None, markieren=None, **_: object
    ) -> dict:
        self._pruefe_wunsch(ziel_ordner, markieren)
        ort = self.lies_kennung(postfach, kennung)

        def aendere(box) -> dict:
            ergebnis: dict = {}
            # Erst markieren, dann verschieben: Danach gilt die Kennung nicht mehr.
            if markieren:
                imap.markiere(box, ort, markieren == "gelesen")
                ergebnis["markiert"] = markieren
            if ziel_ordner:
                ergebnis["verschoben_nach"] = imap.verschiebe(box, ort, str(ziel_ordner))
                ergebnis["hinweis"] = (
                    "Im Zielordner hat die Mail eine neue Kennung; suche sie dort bei Bedarf neu."
                )
            return ergebnis

        return await imap_aufruf(self.kontext, postfach, aendere, wiederholen=False)

    def audit_zusatz(self, daten: dict) -> dict:
        return {"verschoben": "verschoben_nach" in daten, "markiert": daten.get("markiert")}

    def ergebnis_text(self, ergebnis: dict) -> str:
        teile = []
        if ergebnis.get("markiert"):
            teile.append(f"als {ergebnis['markiert']} markiert")
        if ergebnis.get("verschoben_nach"):
            teile.append(f"nach „{ergebnis['verschoben_nach']}“ verschoben")
        return f"✅ Mail im Postfach „{ergebnis.get('konto')}“ {' und '.join(teile)}."
