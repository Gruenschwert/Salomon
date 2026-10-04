"""Telegram-Adapter (Long Polling, Inline-Buttons)."""

import logging
import time
from collections.abc import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import __version__
from app.auth.approvals import Freigaben
from app.auth.users import finde_erlaubten_nutzer
from app.channels.base import Antwort, EingehendeNachricht, FreigabeAnfrage, NachrichtenHandler
from app.config import Settings
from app.db.models import ROLLE_ADMIN
from app.db.session import SessionFabrik
from app.observability.alerts import Alarme
from app.observability.audit import EREIGNIS_UNBEKANNT, protokolliere
from app.observability.costs import Kosten, als_euro

log = logging.getLogger(__name__)

TELEGRAM_MAX_ZEICHEN = 4096
KLICK_PRAEFIX = "freigabe"
KLICK_JA = "ja"
KLICK_NEIN = "nein"
FEHLER_TEXT = "Es ist ein interner Fehler aufgetreten. Bitte versuche es später erneut."
NUR_ADMIN_TEXT = "Dieser Befehl ist Admins vorbehalten."


class TelegramKanal:
    def __init__(
        self,
        settings: Settings,
        session_fabrik: SessionFabrik,
        handler: NachrichtenHandler,
        freigaben: Freigaben,
        kosten: Kosten,
        alarme: Alarme,
        beim_start: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._session_fabrik = session_fabrik
        self._handler = handler
        self._freigaben = freigaben
        self._kosten = kosten
        self._alarme = alarme
        self._beim_start = beim_start
        self._gestartet = time.monotonic()
        self.application = (
            Application.builder()
            .token(settings.telegram_bot_token.get_secret_value())
            .post_init(self._nach_init)
            .build()
        )
        self.application.add_handler(CommandHandler("status", self._bei_status))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._bei_nachricht)
        )
        self.application.add_handler(
            CallbackQueryHandler(self._bei_klick, pattern=rf"^{KLICK_PRAEFIX}:\d+:\w+$")
        )
        self.application.add_error_handler(self._bei_fehler)

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
        user = await self._erlaubter_nutzer(nachricht.absender_id, nachricht.absender_name)
        if user is None:
            return None
        return await self._handler(nachricht, user)

    async def verarbeite_status(self, telegram_id: int) -> str | None:
        """Antwort auf /status: nur für Admins, Unbekannte werden ignoriert (None)."""
        user = await self._erlaubter_nutzer(telegram_id)
        if user is None:
            return None
        if user.rolle != ROLLE_ADMIN:
            return NUR_ADMIN_TEXT
        limit = self._settings.daily_cost_limit_eur
        return "\n".join(
            [
                f"gs-assistant {__version__}",
                f"Uptime: {_als_dauer(time.monotonic() - self._gestartet)}",
                f"Kosten heute: {als_euro(await self._kosten.heute_eur())} von {als_euro(limit)}",
                f"Offene Freigaben: {await self._freigaben.anzahl_offen()}",
            ]
        )

    async def _erlaubter_nutzer(self, telegram_id: int, name: str = ""):
        user = await finde_erlaubten_nutzer(self._session_fabrik, telegram_id, name)
        if user is None:
            await protokolliere(
                self._session_fabrik,
                user_id=None,
                tool_name=EREIGNIS_UNBEKANNT,
                parameter={"telegram_id": telegram_id},
            )
        return user

    async def _melde_fehler(self, wo: str, exc: BaseException | None) -> None:
        # Nur der Typ der Ausnahme: Meldungstexte können interne Details enthalten.
        await self._alarme.melde(f"⚠️ Unbehandelter Fehler ({wo}): {type(exc).__name__}")

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
        except Exception as exc:
            log.exception("Unbehandelter Fehler bei der Verarbeitung einer Nachricht")
            await self.sende_antwort(nachricht.chat_id, FEHLER_TEXT)
            await self._melde_fehler("Nachricht", exc)
            return
        if antwort is not None:
            await self.sende_antwort(nachricht.chat_id, antwort.text)
            for anfrage in antwort.freigaben:
                await self.sende_freigabe_anfrage(nachricht.chat_id, anfrage)

    async def _bei_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        text = await self.verarbeite_status(update.effective_user.id)
        if text is not None:
            await self.sende_antwort(update.effective_chat.id, text)

    async def _bei_klick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None or update.effective_user is None:
            return
        _, approval_id, wahl = query.data.split(":")
        try:
            entscheidung = await self._freigaben.entscheiden(
                int(approval_id), update.effective_user.id, genehmigt=wahl == KLICK_JA
            )
        except Exception as exc:
            log.exception("Unbehandelter Fehler bei der Verarbeitung einer Freigabe")
            await query.answer(FEHLER_TEXT, show_alert=True)
            await self._melde_fehler("Freigabe", exc)
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

    async def _bei_fehler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unbehandelter Fehler im Telegram-Adapter", exc_info=context.error)
        await self._melde_fehler("Telegram", context.error)


def _als_dauer(sekunden: float) -> str:
    minuten = int(sekunden // 60)
    tage, minuten = divmod(minuten, 24 * 60)
    stunden, minuten = divmod(minuten, 60)
    return f"{tage} T {stunden} h {minuten} min"
