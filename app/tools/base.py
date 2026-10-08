"""Tool-Schnittstelle."""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
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

    def ist_schreibend(self, params: dict) -> bool: ...

    async def bereite_vor(self, **params) -> str: ...

    def ergebnis_text(self, ergebnis: dict) -> str | None: ...

    def zweite_bestaetigung(self, vorschau_text: str, **params) -> str | None: ...


# Unter diesem Schlüssel kann ein Tool-Ergebnis eine `Ansicht` enthalten.
ANSICHT_SCHLUESSEL = "_ansicht"


@dataclass(frozen=True)
class Ansicht:
    """Bild oder PDF, das Claude zusätzlich zum Text des Ergebnisses zu sehen bekommt."""

    medientyp: str
    daten: bytes = field(repr=False)


class ToolFehler(Exception):
    """Erwartbarer Fehler; die Meldung geht an Claude und darf keine internen Details enthalten."""


DateiLader = Callable[[str], Awaitable[bytes]]


class DateiQuelle:
    """Zugriff auf Dateien, die ein Nutzer über den Kanal geschickt hat.

    Die Tool-Schicht kennt den Kanal nicht; er meldet sich beim Start über `verbinde` an.
    """

    def __init__(self) -> None:
        self._lader: DateiLader | None = None

    def verbinde(self, lader: DateiLader) -> None:
        self._lader = lader

    async def lade(self, kennung: str) -> bytes:
        if self._lader is None:
            raise ToolFehler("Dateien aus dem Chat sind hier nicht erreichbar.")
        return await self._lader(kennung)


@dataclass(frozen=True)
class ToolKontext:
    """Abhängigkeiten, die jedes Tool beim Erzeugen erhält."""

    settings: Settings
    session_fabrik: SessionFabrik
    dateien: DateiQuelle = field(default_factory=DateiQuelle)
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

    def ist_schreibend(self, params: dict) -> bool:
        """Ob dieser Aufruf eine Freigabe braucht. Nur wenige Tools hängen von den Parametern
        ab (ein allgemeiner API-Aufruf liest mit GET und schreibt mit POST)."""
        return self.schreibend

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
