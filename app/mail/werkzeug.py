"""Gemeinsame Basis der Mail-Tools: eigenes Postfach auflösen, `konto`-`enum`, Audit."""

import copy
import logging
import re
from typing import ClassVar

from app.auth.rechte import MAIL_EIGENE
from app.auth.tresor import Tresor
from app.mail import kennung as kennungen
from app.mail.kennung import Kennung
from app.mail.konten import Postfach, loese_konto, tresor_fuer
from app.mail.lauf import aktueller_mail_lauf
from app.tools.base import BasisTool, ToolFehler, Umgebung, aktueller_nutzer

log = logging.getLogger(__name__)

KONTO_BESCHREIBUNG = (
    "Label des eigenen Postfachs. Nur nötig, wenn die Person mehrere Postfächer verbunden hat."
)
KONTO_SCHEMA = {"type": "string", "description": KONTO_BESCHREIBUNG}
INTERNER_FEHLER_TEXT = "Beim Zugriff auf das Postfach ist ein interner Fehler aufgetreten."
_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,29}$")


class MailTool(BasisTool):
    """Ein Tool, das im eigenen Postfach der anfragenden Person arbeitet.

    Welches Postfach gemeint ist, sagt der Parameter `konto`. Seine erlaubten Werte baut
    `schema_fuer` je Anfrage aus den eigenen Postfächern; aufgelöst wird er auf dem Server
    ausschließlich innerhalb der eigenen Einträge.
    """

    erforderliche_rechte: ClassVar[frozenset[str]] = frozenset({MAIL_EIGENE})
    # Wort für das Audit-Log: gelesen, durchsucht, Entwurf, gesendet …
    aktion: ClassVar[str] = ""

    @property
    def max_ergebnis_zeichen(self) -> int:
        return self.kontext.settings.mail_max_zeichen + 6000

    def verfuegbar_fuer(self, umgebung: Umgebung) -> bool:
        return bool(umgebung.mail_konten)

    def schema_fuer(self, umgebung: Umgebung) -> dict:
        schema = copy.deepcopy(self.parameter_schema)
        schema["properties"]["konto"] = {**KONTO_SCHEMA, "enum": list(umgebung.mail_konten)}
        if len(umgebung.mail_konten) > 1:
            schema["required"] = [*schema.get("required", []), "konto"]
        return schema

    async def postfach(self, konto: object) -> Postfach:
        return await loese_konto(self.kontext, aktueller_nutzer.get(), konto)

    def tresor(self) -> Tresor:
        return tresor_fuer(self.kontext.settings)

    def kennung_text(self, postfach: Postfach, kennung: Kennung) -> str:
        return kennungen.erzeuge(self.tresor(), aktueller_nutzer.get().nutzer_id, postfach, kennung)

    def lies_kennung(self, postfach: Postfach, text: object) -> Kennung:
        """Löst eine Kennung gegen das Postfach der anfragenden Person auf."""
        return kennungen.pruefe(self.tresor(), aktueller_nutzer.get().nutzer_id, postfach, text)

    async def ausfuehren(self, **params) -> dict:
        postfach = await self.postfach(params.pop("konto", None))
        try:
            ergebnis = await self.arbeite(postfach, **params)
        except ToolFehler:
            raise
        except Exception as exc:
            # Nur der Typ: Meldungen können Adressen oder Betreffs enthalten.
            log.error("Fehler im Tool %s: %s", self.name, type(exc).__name__)
            raise ToolFehler(INTERNER_FEHLER_TEXT) from None
        return {"konto": postfach.label, **ergebnis}

    async def arbeite(self, postfach: Postfach, **params) -> dict:
        raise NotImplementedError

    def anzahl(self, daten: dict) -> int:
        return 1

    def audit(self, params: dict, daten: dict | None) -> tuple[dict, str]:
        """Nur Metadaten: Aktion, Konto-Label, Anzahl. Kein Betreff, keine Adresse, kein Text."""
        konto = (daten or {}).get("konto") or params.get("konto")
        angaben: dict = {
            "aktion": self.aktion,
            "konto": konto if isinstance(konto, str) and _LABEL.match(konto) else None,
        }
        if daten is not None:
            angaben["anzahl"] = self.anzahl(daten)
            angaben.update(self.audit_zusatz(daten))
        return angaben, self.aktion if daten is not None else ""

    def audit_zusatz(self, daten: dict) -> dict:
        return {}


class MailSchreibTool(MailTool):
    """Ein Mail-Tool mit Wirkung: läuft nur nach Freigabe mit vollständiger Vorschau.

    Empfänger, Betreff und Text stehen nie im Klartext in der Datenbank (`vertraulich`).
    """

    schreibend: ClassVar[bool] = True
    vertraulich: ClassVar[bool] = True
    ergebnis_im_verlauf: ClassVar[bool] = True
    # Überschrift der Vorschau, z. B. „Mail senden“
    titel: ClassVar[str] = ""

    async def bereite_vor(self, **params) -> str:
        postfach = await self.postfach(params.pop("konto", None))
        if (lauf := aktueller_mail_lauf.get()) is not None:
            # Die Antwort dieser Runde enthält in aller Regel den Entwurf der Mail.
            lauf.vertraulich = True
        try:
            return await self.vorschau_fuer(postfach, **params)
        except ToolFehler:
            raise
        except Exception as exc:
            log.error("Fehler in der Vorschau von %s: %s", self.name, type(exc).__name__)
            raise ToolFehler(INTERNER_FEHLER_TEXT) from None

    async def vorschau_fuer(self, postfach: Postfach, **params) -> str:
        raise NotImplementedError

    def neutrale_vorschau(self, params: dict) -> str:
        konto = params.get("konto")
        if isinstance(konto, str) and _LABEL.match(konto):
            return f"{self.titel} (Postfach {konto})"
        return self.titel
