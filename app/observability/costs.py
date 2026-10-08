"""Kosten: Buchung je Modellantwort, Auswertung und Tageslimits.

Hier stehen nur Zahlen (Person, Modell, Tokens, Beträge), nie Inhalte von Nachrichten.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent.preise import kosten_usd, preis_fuer
from app.config import Settings
from app.db.models import Usage, User
from app.db.session import SessionFabrik
from app.observability.alerts import Alarme

log = logging.getLogger(__name__)

WARNSCHWELLE = Decimal("0.8")
ERLAUBTE_ZEITRAEUME = (1, 3, 7, 30)


def als_euro(betrag: Decimal) -> str:
    return f"{betrag:.2f} €".replace(".", ",")


@dataclass(frozen=True)
class Verbrauch:
    """Token-Zahlen einer Modellantwort, nach Art getrennt."""

    eingabe: int = 0
    ausgabe: int = 0
    cache_lesen: int = 0
    cache_schreiben: int = 0


@dataclass
class Auswertung:
    von: date
    bis: date
    summe_eur: Decimal = Decimal(0)
    anfragen: int = 0
    # Modell -> (EUR, Anfragen)
    je_modell: dict[str, tuple[Decimal, int]] = field(default_factory=dict)
    # Grund der Modellwahl -> Anfragen
    je_grund: dict[str, int] = field(default_factory=dict)
    groesster_tag: tuple[date, Decimal] | None = None


class Kosten:
    def __init__(self, settings: Settings, session_fabrik: SessionFabrik, alarme: Alarme) -> None:
        self._settings = settings
        self._session_fabrik = session_fabrik
        self._alarme = alarme
        self._unbekannte_modelle: set[str] = set()

    def heute(self) -> date:
        return datetime.now(ZoneInfo(self._settings.tz)).date()

    def berechne(self, modell: str, verbrauch: Verbrauch) -> tuple[Decimal, Decimal]:
        """Kosten einer Modellantwort als (USD, EUR)."""
        preis, bekannt = preis_fuer(
            modell,
            self._settings.price_input_usd_per_mtok,
            self._settings.price_output_usd_per_mtok,
        )
        if not bekannt and modell not in self._unbekannte_modelle:
            self._unbekannte_modelle.add(modell)
            log.warning(
                "Kein Preis für das Modell %s hinterlegt; es gelten PRICE_INPUT_USD_PER_MTOK "
                "und PRICE_OUTPUT_USD_PER_MTOK",
                modell,
            )
        usd = kosten_usd(
            preis,
            verbrauch.eingabe,
            verbrauch.ausgabe,
            verbrauch.cache_lesen,
            verbrauch.cache_schreiben,
        )
        return usd, usd * self._settings.usd_eur_rate

    def berechne_eur(self, input_tokens: int, output_tokens: int, modell: str = "") -> Decimal:
        return self.berechne(modell, Verbrauch(input_tokens, output_tokens))[1]

    # ---------------------------------------------------------------- Summen und Limits

    async def _summe(self, tag: date, user_id: int | None = None) -> Decimal:
        bedingungen = [Usage.datum == tag]
        if user_id is not None:
            bedingungen.append(Usage.user_id == user_id)
        async with self._session_fabrik() as session:
            summe = await session.scalar(select(func.sum(Usage.kosten_eur)).where(*bedingungen))
        return Decimal(summe or 0)

    async def heute_eur(self, user_id: int | None = None) -> Decimal:
        """Kosten heute: einer Person oder, ohne Angabe, aller zusammen."""
        return await self._summe(self.heute(), user_id)

    async def limit_von(self, user_id: int) -> Decimal:
        """Das Tageslimit der Person: ihr eigenes oder der Standard."""
        async with self._session_fabrik() as session:
            eigenes = await session.scalar(select(User.tageslimit_eur).where(User.id == user_id))
        return Decimal(eigenes) if eigenes is not None else self._settings.daily_cost_limit_eur

    async def limit_erreicht(self, user_id: int | None = None) -> str | None:
        """Meldung, wenn KI-Anfragen heute gesperrt sind; None, wenn noch Luft ist.

        Geprüft werden das Limit der Person und das Gesamtlimit aller.
        """
        if user_id is not None:
            limit = await self.limit_von(user_id)
            eigene = await self.heute_eur(user_id)
            if eigene >= limit:
                return (
                    f"Dein Tageslimit von {als_euro(limit)} ist erreicht (heute "
                    f"{als_euro(eigene)}). KI-Anfragen gehen ab morgen wieder. Befehle wie "
                    "/kosten funktionieren weiter."
                )
        gesamt = self._settings.daily_cost_limit_total_eur
        if await self.heute_eur() >= gesamt:
            return (
                f"Das gemeinsame Tageslimit aller von {als_euro(gesamt)} ist erreicht. "
                "KI-Anfragen gehen ab morgen wieder. Befehle wie /kosten funktionieren weiter."
            )
        return None

    async def setze_limit(self, user_id: int, euro: Decimal | None) -> None:
        async with self._session_fabrik() as session:
            user = await session.get(User, user_id)
            user.tageslimit_eur = euro
            await session.commit()

    # ---------------------------------------------------------------- Buchen

    async def verbuche(
        self,
        nutzer: object,
        input_tokens: int = 0,
        output_tokens: int = 0,
        *,
        modell: str = "",
        cache_lese_tokens: int = 0,
        cache_schreib_tokens: int = 0,
        grund: str = "",
    ) -> Decimal:
        """Bucht eine Modellantwort und warnt bei 80 % des Tageslimits. Liefert die Kosten in EUR.

        `nutzer` ist der NutzerKontext (oder die Nutzer-ID; dann gibt es keine persönliche
        Warnung, weil die Telegram-ID fehlt).
        """
        user_id = getattr(nutzer, "nutzer_id", nutzer)
        verbrauch = Verbrauch(input_tokens, output_tokens, cache_lese_tokens, cache_schreib_tokens)
        usd, eur = self.berechne(modell, verbrauch)
        eigene_vorher = await self.heute_eur(user_id)
        alle_vorher = await self.heute_eur()
        async with self._session_fabrik() as session:
            session.add(
                Usage(
                    datum=self.heute(),
                    user_id=user_id,
                    modell=modell[:80],
                    eingabe_tokens=input_tokens,
                    ausgabe_tokens=output_tokens,
                    cache_lese_tokens=cache_lese_tokens,
                    cache_schreib_tokens=cache_schreib_tokens,
                    kosten_usd=usd,
                    kosten_eur=eur,
                    grund_modellwahl=grund[:200],
                )
            )
            await session.commit()

        limit = await self.limit_von(user_id)
        if eigene_vorher < limit * WARNSCHWELLE <= eigene_vorher + eur:
            telegram_id = getattr(nutzer, "telegram_id", None)
            if telegram_id is not None:
                await self._alarme.an_person(
                    telegram_id,
                    f"Hinweis: Du hast 80 % deines Tageslimits erreicht "
                    f"({als_euro(eigene_vorher + eur)} von {als_euro(limit)}).",
                )
        gesamt = self._settings.daily_cost_limit_total_eur
        if alle_vorher < gesamt * WARNSCHWELLE <= alle_vorher + eur:
            await self._alarme.melde(
                f"⚠️ 80 % des gemeinsamen Tageslimits erreicht: {als_euro(alle_vorher + eur)} "
                f"von {als_euro(gesamt)}."
            )
        return eur

    # ---------------------------------------------------------------- Auswertung

    def _zeitraum(self, tage: int) -> tuple[date, date]:
        bis = self.heute()
        return bis - timedelta(days=tage - 1), bis

    async def auswertung(self, user_id: int, tage: int) -> Auswertung:
        """Die eigenen Kosten der letzten `tage` Tage einschließlich heute."""
        von, bis = self._zeitraum(tage)
        ergebnis = Auswertung(von, bis)
        zeitraum = (Usage.user_id == user_id, Usage.datum >= von, Usage.datum <= bis)
        async with self._session_fabrik() as session:
            for modell, summe, anzahl in await session.execute(
                select(Usage.modell, func.sum(Usage.kosten_eur), func.count())
                .where(*zeitraum)
                .group_by(Usage.modell)
                .order_by(func.sum(Usage.kosten_eur).desc())
            ):
                ergebnis.je_modell[modell or "unbekannt"] = (Decimal(summe), anzahl)
                ergebnis.summe_eur += Decimal(summe)
                ergebnis.anfragen += anzahl
            for grund, anzahl in await session.execute(
                select(Usage.grund_modellwahl, func.count())
                .where(*zeitraum, Usage.grund_modellwahl != "")
                .group_by(Usage.grund_modellwahl)
                .order_by(func.count().desc())
            ):
                ergebnis.je_grund[grund] = anzahl
            groesster = (
                await session.execute(
                    select(Usage.datum, func.sum(Usage.kosten_eur))
                    .where(*zeitraum)
                    .group_by(Usage.datum)
                    .order_by(func.sum(Usage.kosten_eur).desc(), Usage.datum.desc())
                    .limit(1)
                )
            ).first()
        if groesster is not None:
            ergebnis.groesster_tag = (groesster[0], Decimal(groesster[1]))
        return ergebnis

    async def auswertung_alle(self, tage: int) -> list[tuple[str, Decimal, int]]:
        """Kosten je Person als (Name, EUR, Anfragen). Nur Zahlen, nie Inhalte."""
        von, bis = self._zeitraum(tage)
        async with self._session_fabrik() as session:
            zeilen = await session.execute(
                select(User.anzeigename, User.telegram_id, func.sum(Usage.kosten_eur), func.count())
                .join(User, User.id == Usage.user_id)
                .where(Usage.datum >= von, Usage.datum <= bis)
                .group_by(User.id)
                .order_by(func.sum(Usage.kosten_eur).desc(), User.id)
            )
            return [
                (name or f"Telegram-ID {telegram_id}", Decimal(summe), anzahl)
                for name, telegram_id, summe, anzahl in zeilen
            ]
