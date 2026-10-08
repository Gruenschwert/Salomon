"""Token-/Kosten-Zähler und Tageslimit."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import Settings
from app.db.models import Usage
from app.db.session import SessionFabrik
from app.observability.alerts import Alarme

MIO = Decimal(1_000_000)
WARNSCHWELLE = Decimal("0.8")


def als_euro(betrag: Decimal) -> str:
    return f"{betrag:.2f} €".replace(".", ",")


class Kosten:
    def __init__(self, settings: Settings, session_fabrik: SessionFabrik, alarme: Alarme) -> None:
        self._settings = settings
        self._session_fabrik = session_fabrik
        self._alarme = alarme

    def heute(self) -> date:
        return datetime.now(ZoneInfo(self._settings.tz)).date()

    def berechne_eur(self, input_tokens: int, output_tokens: int) -> Decimal:
        usd = (
            input_tokens * self._settings.price_input_usd_per_mtok
            + output_tokens * self._settings.price_output_usd_per_mtok
        ) / MIO
        return usd * self._settings.usd_eur_rate

    async def heute_eur(self) -> Decimal:
        """Kosten aller Nutzer am heutigen Tag."""
        async with self._session_fabrik() as session:
            summe = await session.scalar(
                select(func.sum(Usage.kosten_eur)).where(Usage.datum == self.heute())
            )
        return Decimal(summe or 0)

    async def limit_erreicht(self) -> bool:
        return await self.heute_eur() >= self._settings.daily_cost_limit_eur

    async def verbuche(self, user_id: int, input_tokens: int, output_tokens: int) -> None:
        """Bucht den Verbrauch eines API-Aufrufs und warnt die Admins bei 80 % des Tageslimits."""
        kosten = self.berechne_eur(input_tokens, output_tokens)
        vorher = await self.heute_eur()
        async with self._session_fabrik() as session:
            session.add(
                Usage(
                    datum=self.heute(),
                    user_id=user_id,
                    eingabe_tokens=input_tokens,
                    ausgabe_tokens=output_tokens,
                    kosten_usd=kosten / self._settings.usd_eur_rate,
                    kosten_eur=kosten,
                )
            )
            await session.commit()

        limit = self._settings.daily_cost_limit_eur
        schwelle = limit * WARNSCHWELLE
        if vorher < schwelle <= vorher + kosten:
            await self._alarme.melde(
                f"⚠️ 80 % des Tageslimits erreicht: {als_euro(vorher + kosten)} "
                f"von {als_euro(limit)}."
            )
