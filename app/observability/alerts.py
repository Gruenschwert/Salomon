"""Fehler-/Kosten-Alarme per Telegram an die Admins."""

import logging
from collections.abc import Awaitable, Callable

from app.auth.users import aktive_admins
from app.db.session import SessionFabrik

log = logging.getLogger(__name__)

Sender = Callable[[int, str], Awaitable[None]]


class Alarme:
    def __init__(self, session_fabrik: SessionFabrik) -> None:
        self._session_fabrik = session_fabrik
        self._sende: Sender | None = None

    def verbinde(self, sende: Sender) -> None:
        """Legt fest, worüber Alarme verschickt werden (Chat-ID = Telegram-ID des Admins)."""
        self._sende = sende

    async def an_person(self, telegram_id: int, text: str) -> None:
        """Schickt einen Hinweis an genau eine Person. Schlägt nie fehl."""
        if self._sende is None:
            return
        try:
            await self._sende(telegram_id, text)
        except Exception as exc:
            log.error("Hinweis an %s nicht zustellbar: %s", telegram_id, type(exc).__name__)

    async def melde(self, text: str) -> None:
        """Schickt den Text an alle Admins. Schlägt nie fehl; Probleme landen nur im Log."""
        log.warning("Alarm: %s", text)
        if self._sende is None:
            return
        try:
            admins = await aktive_admins(self._session_fabrik)
        except Exception:
            log.exception("Admins für Alarm konnten nicht geladen werden")
            return
        for admin in admins:
            try:
                await self._sende(admin.telegram_id, text)
            except Exception as exc:
                log.error(
                    "Alarm an Admin %s nicht zustellbar: %s", admin.telegram_id, type(exc).__name__
                )
