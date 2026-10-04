"""Telegram-Adapter (Long Polling)."""

import logging
from collections.abc import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.auth.approvals import Freigaben
from app.auth.users import finde_erlaubten_nutzer
from app.channels.base import Antwort, EingehendeNachricht, FreigabeAnfrage, NachrichtenHandler
from app.config import Settings
from app.db.session import SessionFabrik
from app.observability.audit import EREIGNIS_UNBEKANNT, protokolliere

log = logging.getLogger(__name__)

TELEGRAM_MAX_ZEICHEN = 4096
KLICK_PRAEFIX = "freigabe"
KLICK_JA = "ja"
KLICK_NEIN = "nein"
FEHLER_TEXT = "Es ist ein interner Fehler aufgetreten. Bitte versuche es später erneut."


class TelegramKanal:
    def __init__(
        self,
        settings: Settings,
        session_fabrik: SessionFabrik,
        handler: NachrichtenHandler,
        freigaben: Freigaben,
        beim_start: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._session_fabrik = session_fabrik
        self._handler = handler
        self._freigaben = freigaben
        self._beim_start = beim_start
        self.application = (
            Application.builder()
            .token(settings.telegram_bot_token.get_secret_value())
            .post_init(self._nach_init)
            .build()
        )
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._bei_nachricht)
        )
        self.application.add_handler(
            CallbackQueryHandler(self._bei_klick, pattern=rf"^{KLICK_PRAEFIX}:\d+:\w+$")
        )

    def starte(self) -> None:
        """Blockiert und pollt, bis der Prozess beendet wird."""
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

    async def _nach_init(self, application: Application) -> None:
        if self._beim_start is not None:
            await self._beim_start()

    async def sende_antwort(self, chat_id: int, text: str) -> None:
        for anfang in range(0, len(text), TELEGRAM_MAX_ZEICHEN):
            await self.application.bot.send_message(
                chat_id=chat_id, text=text[anfang : anfang + TELEGRAM_MAX_ZEICHEN]
            )

    async def sende_freigabe_anfrage(self, chat_id: int, anfrage: FreigabeAnfrage) -> None:
        kennung = f"{KLICK_PRAEFIX}:{anfrage.approval_id}"
        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Freigeben", callback_data=f"{kennung}:{KLICK_JA}"),
                    InlineKeyboardButton("❌ Verwerfen", callback_data=f"{kennung}:{KLICK_NEIN}"),
                ]
            ]
        )
        await self.application.bot.send_message(
            chat_id=chat_id,
            text=f"Freigabe erforderlich (gültig 15 Minuten):\n{anfrage.vorschau_text}",
            reply_markup=buttons,
        )

    async def verarbeite(self, nachricht: EingehendeNachricht) -> Antwort | None:
        """Prüft die Whitelist und reicht die Nachricht an den Handler weiter.

        Unbekannte Nutzer bekommen keine Antwort (None) und werden im Audit-Log vermerkt.
        """
        user = await finde_erlaubten_nutzer(
            self._session_fabrik, nachricht.absender_id, nachricht.absender_name
        )
        if user is None:
            await protokolliere(
                self._session_fabrik,
                user_id=None,
                tool_name=EREIGNIS_UNBEKANNT,
                parameter={"telegram_id": nachricht.absender_id},
            )
            return None
        return await self._handler(nachricht, user)

    async def _bei_nachricht(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        nachricht = EingehendeNachricht(
            chat_id=update.effective_chat.id,
            absender_id=update.effective_user.id,
            absender_name=update.effective_user.full_name,
            text=update.effective_message.text or "",
        )
        try:
            antwort = await self.verarbeite(nachricht)
        except Exception:
            log.exception("Unbehandelter Fehler bei der Verarbeitung einer Nachricht")
            await self.sende_antwort(nachricht.chat_id, FEHLER_TEXT)
            return
        if antwort is not None:
            await self.sende_antwort(nachricht.chat_id, antwort.text)
            for anfrage in antwort.freigaben:
                await self.sende_freigabe_anfrage(nachricht.chat_id, anfrage)

    async def _bei_klick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None or update.effective_user is None:
            return
        _, approval_id, wahl = query.data.split(":")
        try:
            entscheidung = await self._freigaben.entscheiden(
                int(approval_id), update.effective_user.id, genehmigt=wahl == KLICK_JA
            )
        except Exception:
            log.exception("Unbehandelter Fehler bei der Verarbeitung einer Freigabe")
            await query.answer(FEHLER_TEXT, show_alert=True)
            return
        if entscheidung is None:
            return
        if not entscheidung.abgeschlossen:
            await query.answer(entscheidung.text, show_alert=True)
            return
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        if update.effective_chat is not None:
            await self.sende_antwort(update.effective_chat.id, entscheidung.text)
