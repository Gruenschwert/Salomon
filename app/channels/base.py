"""Schnittstelle eines Kanals: Nachricht rein / Antwort raus."""

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
class Antwort:
    text: str


NachrichtenHandler = Callable[[EingehendeNachricht, User], Awaitable[Antwort]]


class Kanal(Protocol):
    async def sende_antwort(self, chat_id: int, text: str) -> None: ...
