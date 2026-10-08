"""Slash-Befehle. Sie laufen direkt auf dem Server und nie über das Modell."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.auth.kontext import NutzerKontext
from app.auth.zugaenge import DIENSTE, Zugaenge
from app.db.session import SessionFabrik
from app.observability.audit import protokolliere
from app.tools.base import ToolFehler

# So lange wartet der Bot nach /verbinden auf die Nachricht mit dem Token.
GEHEIMNIS_FRIST_SEKUNDEN = 600
NUR_PRIVAT_TEXT = (
    "Zugangsdaten nehme ich nur im privaten Chat mit mir an, nie in einer Gruppe. Schreib mir "
    "dort /verbinden."
)


@dataclass(frozen=True)
class Befehl:
    name: str
    beschreibung: str
    funktion: Callable[..., Awaitable[str]]
    # Erforderliches Recht; None = für alle
    recht: str | None = None


class Befehle:
    """Alle Slash-Befehle mit ihren Abhängigkeiten. Der Kanal ruft `fuehre_aus` auf."""

    def __init__(self, session_fabrik: SessionFabrik, zugaenge: Zugaenge | None = None) -> None:
        self._session_fabrik = session_fabrik
        self._zugaenge = zugaenge
        # (Chat-ID, Telegram-ID) -> (Dienst, gültig bis). Die nächste Nachricht dieser Person
        # in diesem Chat ist dann ein Geheimnis und geht an niemanden sonst.
        self._erwartet: dict[tuple[int, int], tuple[str, float]] = {}
        self._tabelle = {
            befehl.name: befehl
            for befehl in (
                Befehl("verbinden", "eigenen Zugang zu einem Dienst verbinden", self._verbinden),
                Befehl("trennen", "eigenen Zugang zu einem Dienst entfernen", self._trennen),
                Befehl("verbunden", "zeigt, welche Dienste du verbunden hast", self._verbunden),
            )
        }

    def namen(self) -> list[str]:
        return list(self._tabelle)

    def erlaubte(self, nutzer: NutzerKontext) -> list[Befehl]:
        return [b for b in self._tabelle.values() if b.recht is None or nutzer.darf(b.recht)]

    async def fuehre_aus(
        self, name: str, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        befehl = self._tabelle.get(name)
        if befehl is None:
            return "Diesen Befehl kenne ich nicht. /hilfe zeigt, was geht."
        if befehl.recht is not None and not nutzer.darf(befehl.recht):
            await protokolliere(
                self._session_fabrik,
                user_id=nutzer.id,
                tool_name=f"/{name}",
                parameter={},
                fehler="kein Recht",
            )
            return "Dafür fehlt dir das Recht. Die Rolle dafür kann ein Admin vergeben."
        try:
            return await befehl.funktion(nutzer, argumente, chat_id, privat)
        except ToolFehler as exc:
            return str(exc)

    # ---------------------------------------------------------------- Zugänge

    def _dienst(self, argumente: list[str]) -> str:
        dienst = argumente[0].lower() if argumente else ""
        if dienst not in DIENSTE:
            raise ToolFehler(f"Bitte den Dienst angeben. Möglich: {', '.join(DIENSTE)}.")
        return dienst

    def _zugang(self) -> Zugaenge:
        if self._zugaenge is None:
            raise ToolFehler("Zugänge sind hier nicht eingerichtet.")
        return self._zugaenge

    async def _verbinden(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        zugang = self._zugang()
        zugang.pruefe_verfuegbar()
        dienst = self._dienst(argumente)
        if not privat:
            return NUR_PRIVAT_TEXT
        self._erwartet[(chat_id, nutzer.telegram_id)] = (
            dienst,
            time.monotonic() + GEHEIMNIS_FRIST_SEKUNDEN,
        )
        return (
            f"Schick mir jetzt als nächste Nachricht {DIENSTE[dienst]}.\n"
            "Ich lösche die Nachricht sofort aus dem Chat, speichere den Token verschlüsselt und "
            "gebe ihn nie an die KI weiter. Mit jedem anderen Befehl brichst du ab."
        )

    def erwartet_geheimnis(self, chat_id: int, telegram_id: int) -> str | None:
        """Der Dienst, für den die nächste Nachricht dieser Person ein Geheimnis ist."""
        eintrag = self._erwartet.get((chat_id, telegram_id))
        if eintrag is None:
            return None
        if time.monotonic() > eintrag[1]:
            del self._erwartet[(chat_id, telegram_id)]
            return None
        return eintrag[0]

    def brich_ab(self, chat_id: int, telegram_id: int) -> None:
        self._erwartet.pop((chat_id, telegram_id), None)

    async def nimm_geheimnis(self, nutzer: NutzerKontext, chat_id: int, geheimnis: str) -> str:
        """Verarbeitet die Nachricht nach /verbinden. Sie wird weder gespeichert noch geloggt
        noch an das Modell gegeben; der Kanal hat sie bereits aus dem Chat gelöscht."""
        dienst, _ = self._erwartet.pop((chat_id, nutzer.telegram_id))
        try:
            konto = await self._zugang().verbinde(nutzer, dienst, geheimnis)
        except ToolFehler as exc:
            await protokolliere(
                self._session_fabrik,
                user_id=nutzer.id,
                tool_name="/verbinden",
                parameter={"dienst": dienst},
                fehler="abgelehnt",
            )
            return (
                f"Das hat nicht geklappt: {exc} Es wurde nichts gespeichert. Mit /verbinden "
                f"{dienst} kannst du es noch einmal versuchen."
            )
        await protokolliere(
            self._session_fabrik,
            user_id=nutzer.id,
            tool_name="/verbinden",
            parameter={"dienst": dienst},
            ergebnis_kurz="verbunden",
        )
        return f"✅ {dienst.capitalize()} ist verbunden, Konto: {konto or 'unbekannt'}."

    async def _trennen(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        dienst = self._dienst(argumente)
        entfernt = await self._zugang().trenne(nutzer, dienst)
        await protokolliere(
            self._session_fabrik,
            user_id=nutzer.id,
            tool_name="/trennen",
            parameter={"dienst": dienst},
            ergebnis_kurz="getrennt" if entfernt else "war nicht verbunden",
        )
        if not entfernt:
            return f"{dienst.capitalize()} war nicht verbunden."
        return f"{dienst.capitalize()} ist getrennt. Dein gespeicherter Token ist gelöscht."

    async def _verbunden(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        dienste = await self._zugang().liste(nutzer)
        if not dienste:
            return "Du hast noch keinen Dienst verbunden. Los geht es mit /verbinden asana."
        return "Verbunden: " + ", ".join(dienste) + ". Die Zugangsdaten selbst zeige ich nie an."
