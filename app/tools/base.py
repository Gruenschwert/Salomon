"""Tool-Schnittstelle."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import ClassVar, Protocol

import httpx

from app.config import Settings
from app.db.models import User
from app.db.session import SessionFabrik


class Tool(Protocol):
    name: str  # z. B. "shopify_lagerbestand"
    beschreibung: str  # Klartext für Claude: wann nutzen, was kommt zurück
    parameter_schema: dict  # JSON-Schema der Eingaben
    schreibend: bool  # True = braucht Freigabe
    erlaubte_rollen: set[str]  # {"admin","user"}
    ergebnis_im_verlauf: bool  # True = Ergebnis der Freigabe kommt in den Gesprächsverlauf

    async def ausfuehren(self, **params) -> dict: ...

    # nur bei schreibend=True: lesbare Vorschau für den Freigabe-Button
    def vorschau(self, **params) -> str: ...

    async def bereite_vor(self, **params) -> str: ...

    def ergebnis_text(self, ergebnis: dict) -> str | None: ...

    def zweite_bestaetigung(self, vorschau_text: str, **params) -> str | None: ...


class ToolFehler(Exception):
    """Erwartbarer Fehler; die Meldung geht an Claude und darf keine internen Details enthalten."""


@dataclass(frozen=True)
class ToolKontext:
    """Abhängigkeiten, die jedes Tool beim Erzeugen erhält."""

    settings: Settings
    session_fabrik: SessionFabrik
    # Nur für Tests: ersetzt die Netzwerkschicht von httpx.
    http_transport: httpx.AsyncBaseTransport | None = None


# Der Nutzer, in dessen Auftrag das Tool gerade läuft. Wird vom Ausführer gesetzt, damit
# `ausfuehren(**params)` ausschließlich die Parameter aus dem Schema entgegennimmt.
aktueller_nutzer: ContextVar[User] = ContextVar("aktueller_nutzer")
# Die Freigabe, in deren Auftrag ein schreibendes Tool gerade läuft (None außerhalb davon).
aktuelle_freigabe: ContextVar[int | None] = ContextVar("aktuelle_freigabe", default=None)
# True, wenn der Nutzer für diese Freigabe auch die zweite Rückfrage bestätigt hat.
zweifach_bestaetigt: ContextVar[bool] = ContextVar("zweifach_bestaetigt", default=False)


class BasisTool:
    """Basisklasse aller Tools. Die Registry findet jede Unterklasse in `app/tools/`."""

    name: ClassVar[str]
    beschreibung: ClassVar[str]
    parameter_schema: ClassVar[dict]
    schreibend: ClassVar[bool] = False
    # True: Das Ergebnis der Freigabe wird in den Gesprächsverlauf geschrieben.
    ergebnis_im_verlauf: ClassVar[bool] = False
    erlaubte_rollen: ClassVar[set[str]] = {"admin", "user"}

    def __init__(self, kontext: ToolKontext) -> None:
        self.kontext = kontext

    async def ausfuehren(self, **params) -> dict:
        raise NotImplementedError

    def vorschau(self, **params) -> str:
        raise NotImplementedError

    async def bereite_vor(self, **params) -> str:
        """Liefert die Vorschau für die Freigabe. Tools, die dafür erst den aktuellen Zustand
        lesen müssen, überschreiben diese Methode; es darf dabei nichts geändert werden."""
        return self.vorschau(**params)

    def ergebnis_text(self, ergebnis: dict) -> str | None:
        """Eigene Meldung an den Nutzer nach der Ausführung; None = Standardtext."""
        return None

    def zweite_bestaetigung(self, vorschau_text: str, **params) -> str | None:
        """Text einer zweiten Rückfrage nach dem ersten ✅; None = keine nötig."""
        return None
