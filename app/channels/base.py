"""Schnittstelle eines Kanals: Nachricht rein / Antwort raus / Freigabe-Anfrage."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.db.models import User


@dataclass(frozen=True)
class Bild:
    """Ein Foto im Arbeitsspeicher. Bilder werden nie dauerhaft gespeichert."""

    medientyp: str
    daten: bytes = field(repr=False)


@dataclass(frozen=True)
class DateiHinweis:
    """Verweis auf eine Datei aus dem Chat, die sich später an Asana anhängen lässt."""

    verweis: str
    name: str
    medientyp: str
    groesse: int | None


@dataclass(frozen=True)
class EingehendeNachricht:
    chat_id: int
    absender_id: int
    absender_name: str
    # Bei Fotos: die Bildunterschrift
    text: str
    bilder: tuple[Bild, ...] = ()
    dateien: tuple[DateiHinweis, ...] = ()


@dataclass(frozen=True)
class FreigabeAnfrage:
    approval_id: int
    vorschau_text: str


@dataclass(frozen=True)
class Antwort:
    text: str
    freigaben: tuple[FreigabeAnfrage, ...] = ()


NachrichtenHandler = Callable[[EingehendeNachricht, User], Awaitable[Antwort]]


class Kanal(Protocol):
    async def sende_antwort(self, chat_id: int, text: str) -> None: ...

    async def sende_freigabe_anfrage(self, chat_id: int, anfrage: FreigabeAnfrage) -> None: ...
