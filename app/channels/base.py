"""Schnittstelle eines Kanals: Nachricht rein / Antwort raus / Freigabe-Anfrage."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.db.models import User


@dataclass(frozen=True)
class EingehendeNachricht:
    chat_id: int
    absender_id: int
    absender_name: str
    text: str


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
