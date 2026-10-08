"""Rundnachrichten an alle Personen mit einer Rolle, z. B. das Buchhaltungs-Update am
Monatsanfang."""

import logging
from collections.abc import Awaitable, Callable

from app.auth.users import aktive_mit_rolle
from app.db.session import SessionFabrik

log = logging.getLogger(__name__)

Sender = Callable[[int, str], Awaitable[None]]


async def an_rolle_senden(
    session_fabrik: SessionFabrik, sende: Sender, rolle: str, text: str
) -> int:
    """Schickt den Text an alle aktiven, nicht gesperrten Personen mit dieser Rolle.

    Liefert, wie viele ihn bekommen haben. Ein Fehler bei einer Person hält die anderen nicht
    auf. Der Text selbst wird nicht geloggt.
    """
    zugestellt = 0
    for person in await aktive_mit_rolle(session_fabrik, rolle):
        try:
            await sende(person.telegram_id, text)
            zugestellt += 1
        except Exception as exc:
            log.warning(
                "Rundnachricht an Rolle %s für %s nicht zustellbar: %s",
                rolle,
                person.telegram_id,
                type(exc).__name__,
            )
    return zugestellt
