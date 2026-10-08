"""Telegram-Adapter (Long Polling, Inline-Buttons)."""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import __version__
from app.agent.history import speichere_hinweis
from app.auth.approvals import Freigaben
from app.auth.users import finde_erlaubten_nutzer
from app.channels.base import (
    Antwort,
    Bild,
    DateiHinweis,
    EingehendeNachricht,
    FreigabeAnfrage,
    NachrichtenHandler,
)
from app.config import Settings
from app.db.models import TelegramDatei
from app.db.session import SessionFabrik
from app.medien import bild_medientyp as medientyp
from app.observability.alerts import Alarme
from app.observability.audit import EREIGNIS_UNBEKANNT, protokolliere
from app.observability.costs import Kosten, als_euro
from app.observability.geheimnisse import maskiere, sammle_geheimnisse

log = logging.getLogger(__name__)

TELEGRAM_MAX_ZEICHEN = 4096
# Teile bleiben deutlich unter dem Limit von Telegram, damit nie eine Nachricht scheitert.
TEIL_MAX_ZEICHEN = 3800
# Längere Texte gehen gekürzt in den Chat und vollständig als Datei.
TEXT_MAX_ZEICHEN = 20000
MAX_GRUND_ZEICHEN = 200
KLICK_PRAEFIX = "freigabe"
KLICK_JA = "ja"
KLICK_NEIN = "nein"
KLICK_LOESCHEN = "loeschen"
FEHLER_TEXT = "Es ist ein interner Fehler aufgetreten. Bitte versuche es später erneut."
NUR_ADMIN_TEXT = "Dieser Befehl ist Admins vorbehalten."
MAX_FOTOS = 5
# So lange wird nach dem letzten Foto eines Albums auf weitere gewartet.
ALBUM_WARTEZEIT_SEKUNDEN = 1.5
FOTO_UNLESBAR_TEXT = (
    "Dieses Bildformat kann ich nicht lesen. Bitte schicke das Foto als JPEG oder PNG."
)
ZU_VIELE_FOTOS_TEXT = f"Hinweis: Ich habe nur die ersten {MAX_FOTOS} Fotos berücksichtigt."
MAX_DOKUMENTE = 10
# Telegram gibt Bots nur Dateien bis 20 MB heraus.
TELEGRAM_MAX_DOWNLOAD_MB = 20
DATEI_ZU_GROSS_TEXT = (
    f"Die Datei „{{name}}“ ist größer als {TELEGRAM_MAX_DOWNLOAD_MB} MB. So große Dateien gibt "
    "Telegram nicht an Bots heraus. Bitte lade sie direkt in Asana hoch."
)
_ENDUNG = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


@dataclass
class _Album:
    """Fotos einer Telegram-Mediengruppe, die noch gesammelt werden."""

    chat_id: int
    absender_id: int
    absender_name: str
    # (message_id, größtes Foto der Nachricht)
    fotos: list[tuple[int, object]] = field(default_factory=list)
    # (message_id, Dokument)
    dokumente: list[tuple[int, object]] = field(default_factory=list)
    bildunterschrift: str = ""
    aufgabe: asyncio.Task | None = None


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
        self._geheimnisse = sammle_geheimnisse(settings)
        self._alben: dict[tuple[int, str], _Album] = {}
        self.album_wartezeit = ALBUM_WARTEZEIT_SEKUNDEN
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
        self.application.add_handler(MessageHandler(filters.PHOTO, self._bei_foto))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self._bei_foto))
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

    async def lade_datei(self, file_id: str) -> bytes:
        """Lädt eine Datei, die ein Nutzer geschickt hat, in den Arbeitsspeicher."""
        datei = await self.application.bot.get_file(file_id)
        return bytes(await datei.download_as_bytearray())

    async def _merke_datei(
        self, user_id: int, chat_id: int, file_id: str, name: str, typ: str, groesse: int | None
    ) -> DateiHinweis:
        """Hält fest, wo die Datei bei Telegram liegt. Der Inhalt wird nicht gespeichert."""
        async with self._session_fabrik() as session:
            eintrag = TelegramDatei(
                user_id=user_id,
                chat_id=chat_id,
                file_id=file_id,
                name=name[:300],
                medientyp=typ[:100],
                groesse=groesse,
            )
            session.add(eintrag)
            await session.commit()
        return DateiHinweis(f"datei:{eintrag.id}", eintrag.name, eintrag.medientyp, groesse)

    async def sende_text(self, chat_id: int, text: str) -> None:
        """Zentrale Sendefunktion: teilt lange Texte an Zeilenenden und sendet die Teile der
        Reihe nach. Erst ab TEXT_MAX_ZEICHEN wird gekürzt; der ganze Text kommt dann als Datei."""
        if len(text) > TEXT_MAX_ZEICHEN:
            anfang = teile_text(text)[0]
            await self.application.bot.send_message(chat_id=chat_id, text=anfang)
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f"Der Text ist {len(text)} Zeichen lang und damit zu lang für den Chat. "
                "Oben steht der Anfang, der ganze Text ist in der Datei.",
            )
            await self.sende_datei(chat_id, "text.txt", text)
            return
        for teil in teile_text(text):
            await self.application.bot.send_message(chat_id=chat_id, text=teil)

    async def sende_datei(self, chat_id: int, name: str, text: str) -> None:
        await self.application.bot.send_document(
            chat_id=chat_id, document=InputFile(text.encode("utf-8"), filename=name)
        )

    async def sende_antwort(self, chat_id: int, text: str) -> None:
        await self.sende_text(chat_id, text)

    async def sende_freigabe_anfrage(self, chat_id: int, anfrage: FreigabeAnfrage) -> None:
        """Vorschau in beliebiger Länge; die Buttons hängen an einer kurzen letzten Nachricht."""
        kennung = f"{KLICK_PRAEFIX}:{anfrage.approval_id}"
        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Freigeben", callback_data=f"{kennung}:{KLICK_JA}"),
                    InlineKeyboardButton("❌ Verwerfen", callback_data=f"{kennung}:{KLICK_NEIN}"),
                ]
            ]
        )
        if anfrage.kompakt_text:
            await self.sende_text(chat_id, f"Freigabe erforderlich:\n{anfrage.kompakt_text}")
            await self.sende_datei(
                chat_id, f"aenderungssatz_{anfrage.approval_id}.txt", anfrage.vorschau_text
            )
        else:
            await self.sende_text(chat_id, f"Freigabe erforderlich:\n{anfrage.vorschau_text}")
        wort = "1 Änderung" if anfrage.anzahl == 1 else f"{anfrage.anzahl} Änderungen"
        await self.application.bot.send_message(
            chat_id=chat_id, text=f"Freigabe für {wort}, gültig 15 Minuten", reply_markup=buttons
        )

    async def sende_loesch_rueckfrage(self, chat_id: int, approval_id: int, text: str) -> None:
        """Zweite Rückfrage vor dem Löschen; die Buttons hängen an der letzten Nachricht."""
        kennung = f"{KLICK_PRAEFIX}:{approval_id}"
        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑 Ja, löschen", callback_data=f"{kennung}:{KLICK_LOESCHEN}"
                    ),
                    InlineKeyboardButton("Abbrechen", callback_data=f"{kennung}:{KLICK_NEIN}"),
                ]
            ]
        )
        await self.sende_text(chat_id, text)
        await self.application.bot.send_message(
            chat_id=chat_id,
            text="Löschen bestätigen? Gültig bis 15 Minuten nach der Vorschau.",
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
        if user.rolle != "admin":
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
        await self._sende(nachricht.chat_id, antwort)

    async def _sende(self, chat_id: int, antwort: Antwort | None) -> None:
        if antwort is not None:
            # Nachrichten gehen als reiner Text raus; Markdown-Zeichen von Claude würden
            # sonst wörtlich erscheinen. Vorschauen bleiben unangetastet.
            await self.sende_antwort(chat_id, ohne_markdown(antwort.text))
            for anfrage in antwort.freigaben:
                await self._sende_freigabe_oder_verwirf(chat_id, anfrage)

    async def _sende_freigabe_oder_verwirf(self, chat_id: int, anfrage: FreigabeAnfrage) -> None:
        """Kommt die Freigabe nicht beim Nutzer an, wird sie sofort verworfen und er erfährt es.

        Ohne sichtbare Buttons darf kein Änderungssatz offen bleiben.
        """
        try:
            await self.sende_freigabe_anfrage(chat_id, anfrage)
            return
        except Exception as exc:
            grund = maskiere(f"{type(exc).__name__}: {exc}", self._geheimnisse)[:MAX_GRUND_ZEICHEN]
            log.exception(
                "Freigabe #%s konnte nicht gesendet werden (%s Änderungen, Vorschau %s Zeichen)",
                anfrage.approval_id,
                anfrage.anzahl,
                len(anfrage.vorschau_text),
            )
        user_id = await self._freigaben.verwerfen(anfrage.approval_id, grund)
        text = f"Die Freigabe konnte nicht gesendet werden: {grund}. Es wurde nichts geändert."
        if user_id is not None:
            await speichere_hinweis(self._session_fabrik, chat_id, user_id, text)
        await self.sende_antwort(chat_id, text)
        await self._alarme.melde(
            f"⚠️ Freigabe #{anfrage.approval_id} war nicht zustellbar und wurde verworfen."
        )

    async def _bei_foto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fotos und Dokumente; beides kann der Nutzer lesen lassen oder an Asana anhängen."""
        nachricht = update.effective_message
        dokument = getattr(nachricht, "document", None)
        if (
            update.effective_user is None
            or update.effective_chat is None
            or not (nachricht.photo or dokument)
        ):
            return
        # Fotos von Nutzern außerhalb der Whitelist werden wie Text ignoriert – und gar nicht
        # erst heruntergeladen.
        absender = update.effective_user
        if await self._erlaubter_nutzer(absender.id, absender.full_name) is None:
            return
        chat_id = update.effective_chat.id
        foto = groesstes_foto(nachricht.photo) if nachricht.photo else None
        if not nachricht.media_group_id:
            await self._verarbeite_fotos(
                chat_id,
                absender.id,
                absender.full_name,
                [foto] if foto else [],
                nachricht.caption or "",
                dokumente=[] if foto else [dokument],
            )
            return
        # Album: Telegram schickt jedes Foto als eigene Nachricht. Sie werden gesammelt und
        # kurz nach dem letzten als eine Anfrage übergeben.
        schluessel = (chat_id, nachricht.media_group_id)
        album = self._alben.setdefault(schluessel, _Album(chat_id, absender.id, absender.full_name))
        if foto:
            album.fotos.append((nachricht.message_id, foto))
        else:
            album.dokumente.append((nachricht.message_id, dokument))
        album.bildunterschrift = album.bildunterschrift or nachricht.caption or ""
        if album.aufgabe is not None:
            album.aufgabe.cancel()
        album.aufgabe = asyncio.create_task(self._album_abschliessen(schluessel))

    async def _album_abschliessen(self, schluessel: tuple[int, str]) -> None:
        await asyncio.sleep(self.album_wartezeit)
        album = self._alben.pop(schluessel)
        fotos = [foto for _, foto in sorted(album.fotos, key=lambda eintrag: eintrag[0])]
        dokumente = [d for _, d in sorted(album.dokumente, key=lambda eintrag: eintrag[0])]
        await self._verarbeite_fotos(
            album.chat_id,
            album.absender_id,
            album.absender_name,
            fotos,
            album.bildunterschrift,
            dokumente=dokumente,
        )

    async def _verarbeite_fotos(
        self,
        chat_id: int,
        absender_id: int,
        absender_name: str,
        fotos: list,
        text: str,
        dokumente: list | tuple = (),
    ) -> None:
        """Lädt die Fotos in den Arbeitsspeicher und übergibt sie als eine Anfrage.

        Die Bilder werden nirgends gespeichert; nach der Verarbeitung sind sie verworfen.
        Zu Fotos und Dokumenten wird nur ein Verweis festgehalten, damit sie sich nach einer
        Freigabe an Asana anhängen lassen. Dokumente werden hier gar nicht heruntergeladen.
        """
        max_mb = self._settings.photo_max_mb
        max_bytes = int(max_mb * 1024 * 1024)
        hinweise = []
        if len(fotos) > MAX_FOTOS:
            fotos = fotos[:MAX_FOTOS]
            hinweise.append(ZU_VIELE_FOTOS_TEXT)
        try:
            user = await finde_erlaubten_nutzer(self._session_fabrik, absender_id)
            bilder: list[Bild] = []
            dateien: list[DateiHinweis] = []
            zu_gross = unlesbar = 0
            for foto in fotos:
                if (foto.file_size or 0) > max_bytes:
                    zu_gross += 1
                    continue
                daten = bytes(await (await foto.get_file()).download_as_bytearray())
                if len(daten) > max_bytes:
                    zu_gross += 1
                elif (typ := medientyp(daten)) is None:
                    unlesbar += 1
                else:
                    bilder.append(Bild(typ, daten))
                    if user is not None and getattr(foto, "file_id", None):
                        dateien.append(
                            await self._merke_datei(
                                user.id,
                                chat_id,
                                foto.file_id,
                                f"foto_{len(bilder)}.{_ENDUNG[typ]}",
                                typ,
                                len(daten),
                            )
                        )
            for dokument in list(dokumente)[:MAX_DOKUMENTE]:
                name = dokument.file_name or "datei"
                if (dokument.file_size or 0) > TELEGRAM_MAX_DOWNLOAD_MB * 1024 * 1024:
                    hinweise.append(DATEI_ZU_GROSS_TEXT.format(name=name))
                elif user is not None:
                    dateien.append(
                        await self._merke_datei(
                            user.id,
                            chat_id,
                            dokument.file_id,
                            name,
                            dokument.mime_type or "",
                            dokument.file_size,
                        )
                    )
            if zu_gross:
                if len(fotos) == 1:
                    hinweise.append(
                        f"Das Foto ist größer als {max_mb:g} MB. Bitte schicke ein kleineres Foto."
                    )
                else:
                    hinweise.append(
                        f"{zu_gross} der Fotos sind größer als {max_mb:g} MB und wurden nicht "
                        "berücksichtigt. Bitte schicke kleinere Fotos."
                    )
            if unlesbar:
                hinweise.append(FOTO_UNLESBAR_TEXT)
            antwort = None
            if bilder or dateien:
                antwort = await self.verarbeite(
                    EingehendeNachricht(
                        chat_id=chat_id,
                        absender_id=absender_id,
                        absender_name=absender_name,
                        text=text,
                        bilder=tuple(bilder),
                        dateien=tuple(dateien),
                    )
                )
        except Exception as exc:
            log.exception("Unbehandelter Fehler bei der Verarbeitung eines Fotos")
            await self.sende_antwort(chat_id, FEHLER_TEXT)
            await self._melde_fehler("Foto", exc)
            return
        for hinweis in hinweise:
            await self.sende_antwort(chat_id, hinweis)
        await self._sende(chat_id, antwort)

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
                int(approval_id),
                update.effective_user.id,
                genehmigt=wahl in (KLICK_JA, KLICK_LOESCHEN),
                bestaetigt=wahl == KLICK_LOESCHEN,
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
        # Ein großer Änderungssatz läuft länger, als Telegram auf die Quittung des Klicks
        # wartet. Das Ergebnis muss den Nutzer trotzdem erreichen.
        try:
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError as exc:
            log.warning("Klick konnte nicht quittiert werden: %s", type(exc).__name__)
        if entscheidung.rueckfrage:
            if update.effective_chat is not None:
                try:
                    await self.sende_loesch_rueckfrage(
                        update.effective_chat.id, int(approval_id), entscheidung.text
                    )
                except Exception as exc:
                    # Ohne sichtbare Rückfrage darf die Freigabe nicht offen bleiben.
                    grund = maskiere(f"{type(exc).__name__}: {exc}", self._geheimnisse)
                    log.exception("Lösch-Rückfrage #%s konnte nicht gesendet werden", approval_id)
                    await self._freigaben.verwerfen(int(approval_id), grund[:MAX_GRUND_ZEICHEN])
                    await self.sende_antwort(
                        update.effective_chat.id,
                        "Die Rückfrage zum Löschen konnte nicht gesendet werden: "
                        f"{grund[:MAX_GRUND_ZEICHEN]}. Es wurde nichts geändert.",
                    )
            return
        if update.effective_chat is not None:
            if entscheidung.im_verlauf and entscheidung.user_id is not None:
                await speichere_hinweis(
                    self._session_fabrik,
                    update.effective_chat.id,
                    entscheidung.user_id,
                    entscheidung.text,
                )
            await self.sende_antwort(update.effective_chat.id, entscheidung.text)

    async def _bei_fehler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unbehandelter Fehler im Telegram-Adapter", exc_info=context.error)
        await self._melde_fehler("Telegram", context.error)


def groesstes_foto(fotos):
    """Telegram liefert jedes Foto in mehreren Größen; verwendet wird die größte Auflösung."""
    return max(fotos, key=lambda foto: foto.width * foto.height)


_FETT = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_KURSIV = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_UEBERSCHRIFT = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_STERN_LISTE = re.compile(r"^([ \t]*)\*[ \t]+", re.MULTILINE)
_CODE = re.compile(r"`{1,3}([^`\n]+?)`{1,3}")


def ohne_markdown(text: str) -> str:
    """Entfernt Markdown-Auszeichnung, die Telegram als reinen Text wörtlich zeigen würde."""
    text = _UEBERSCHRIFT.sub("", text)
    text = _STERN_LISTE.sub(r"\1- ", text)
    text = _FETT.sub(r"\2", text)
    text = _KURSIV.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    return text


def teile_text(text: str, maximum: int = TEIL_MAX_ZEICHEN) -> list[str]:
    """Teilt einen Text in Nachrichten von höchstens `maximum` Zeichen.

    Getrennt wird nur an Zeilenumbrüchen. Eine einzelne Zeile, die allein zu lang ist, wird an
    Leerzeichen getrennt, damit kein Wort und keine URL zerrissen wird; erst ein einzelnes
    Wort über `maximum` Zeichen wird hart geteilt.
    """
    teile: list[str] = []
    zeilen: list[str] = []
    laenge = 0

    def abschliessen() -> None:
        nonlocal zeilen, laenge
        teil = "\n".join(zeilen).strip("\n")
        if teil.strip():
            teile.append(teil)
        zeilen, laenge = [], 0

    for zeile in text.split("\n"):
        for stueck in _teile_zeile(zeile, maximum):
            if zeilen and laenge + 1 + len(stueck) > maximum:
                abschliessen()
            zeilen.append(stueck)
            laenge += len(stueck) + (1 if len(zeilen) > 1 else 0)
    abschliessen()
    return teile


def _teile_zeile(zeile: str, maximum: int) -> list[str]:
    """Eine Zeile, die allein in keine Nachricht passt, an Leerzeichen in Stücke teilen."""
    if len(zeile) <= maximum:
        return [zeile]
    stuecke: list[str] = []
    aktuell = ""
    for wort in zeile.split(" "):
        while len(wort) > maximum:
            if aktuell:
                stuecke.append(aktuell)
                aktuell = ""
            stuecke.append(wort[:maximum])
            wort = wort[maximum:]
        if aktuell and len(aktuell) + 1 + len(wort) > maximum:
            stuecke.append(aktuell)
            aktuell = wort
        else:
            aktuell = f"{aktuell} {wort}" if aktuell else wort
    if aktuell:
        stuecke.append(aktuell)
    return stuecke


def _als_dauer(sekunden: float) -> str:
    minuten = int(sekunden // 60)
    tage, minuten = divmod(minuten, 24 * 60)
    stunden, minuten = divmod(minuten, 60)
    return f"{tage} T {stunden} h {minuten} min"
