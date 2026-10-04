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

    async def ausfuehren(self, **params) -> dict: ...

    # nur bei schreibend=True: lesbare Vorschau für den Freigabe-Button
    def vorschau(self, **params) -> str: ...


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


class BasisTool:
    """Basisklasse aller Tools. Die Registry findet jede Unterklasse in `app/tools/`."""

    name: ClassVar[str]
    beschreibung: ClassVar[str]
    parameter_schema: ClassVar[dict]
    schreibend: ClassVar[bool] = False
    erlaubte_rollen: ClassVar[set[str]] = {"admin", "user"}

    def __init__(self, kontext: ToolKontext) -> None:
        self.kontext = kontext

    async def ausfuehren(self, **params) -> dict:
        raise NotImplementedError

    def vorschau(self, **params) -> str:
        raise NotImplementedError
