"""Freigabe-Flow: Schreibende Tools laufen erst nach ✅ des anfragenden Nutzers."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app.auth.users import finde_erlaubten_nutzer
from app.channels.base import FreigabeAnfrage
from app.db.models import (
    STATUS_ABGELAUFEN,
    STATUS_ABGELEHNT,
    STATUS_GENEHMIGT,
    STATUS_OFFEN,
    Approval,
    User,
    jetzt,
)
from app.observability.audit import EREIGNIS_UNBEKANNT, protokolliere
from app.tools.base import Tool, ToolKontext
from app.tools.registry import Registry, fuehre_tool_aus

FREIGABE_GUELTIGKEIT = timedelta(minutes=15)

STATUS_NICHT_GEFUNDEN = "nicht_gefunden"
STATUS_FREMDER_NUTZER = "fremder_nutzer"
STATUS_BEREITS_ENTSCHIEDEN = "bereits_entschieden"


@dataclass(frozen=True)
class Entscheidung:
    status: str
    text: str

    @property
    def abgeschlossen(self) -> bool:
        """False, solange die Freigabe für den eigentlichen Nutzer noch offen ist."""
        return self.status != STATUS_FREMDER_NUTZER


class Freigaben:
    def __init__(self, kontext: ToolKontext, registry: Registry) -> None:
        self._kontext = kontext
        self._session_fabrik = kontext.session_fabrik
        self._registry = registry

    async def anfragen(self, user: User, tool: Tool, params: dict) -> FreigabeAnfrage:
        """Legt eine offene Freigabe an. Das Tool wird dabei NICHT ausgeführt."""
        vorschau = tool.vorschau(**params)
        async with self._session_fabrik() as session:
            approval = Approval(
                user_id=user.id, tool_name=tool.name, parameter=params, vorschau_text=vorschau
            )
            session.add(approval)
            await session.commit()
        await protokolliere(
            self._session_fabrik,
            user_id=user.id,
            tool_name=tool.name,
            parameter=params,
            ergebnis_kurz=f"Freigabe #{approval.id} angefragt",
        )
        return FreigabeAnfrage(approval_id=approval.id, vorschau_text=vorschau)

    async def entscheiden(
        self,
        approval_id: int,
        telegram_id: int,
        genehmigt: bool,
        zeitpunkt: datetime | None = None,
    ) -> Entscheidung | None:
        """Verarbeitet einen Klick auf ✅ / ❌. None bedeutet: Klick eines unbekannten Nutzers."""
        zeitpunkt = zeitpunkt or jetzt()
        user = await finde_erlaubten_nutzer(self._session_fabrik, telegram_id)
        if user is None:
            await protokolliere(
                self._session_fabrik,
                user_id=None,
                tool_name=EREIGNIS_UNBEKANNT,
                parameter={"telegram_id": telegram_id},
            )
            return None

        async with self._session_fabrik() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                return Entscheidung(STATUS_NICHT_GEFUNDEN, "Diese Freigabe gibt es nicht (mehr).")
            if approval.user_id != user.id:
                return Entscheidung(
                    STATUS_FREMDER_NUTZER,
                    "Nur der anfragende Nutzer kann diese Freigabe entscheiden.",
                )
            if zeitpunkt - approval.erstellt_am > FREIGABE_GUELTIGKEIT:
                neuer_status = STATUS_ABGELAUFEN
            else:
                neuer_status = STATUS_GENEHMIGT if genehmigt else STATUS_ABGELEHNT
            # Bedingtes Update: nur eine offene Freigabe kann entschieden werden (Doppelklick).
            ergebnis = await session.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status == STATUS_OFFEN)
                .values(status=neuer_status, entschieden_am=zeitpunkt)
            )
            await session.commit()
            if ergebnis.rowcount != 1:
                return Entscheidung(
                    STATUS_BEREITS_ENTSCHIEDEN, "Diese Freigabe wurde bereits entschieden."
                )

        if neuer_status == STATUS_GENEHMIGT:
            return await self._ausfuehren(approval, user)

        await protokolliere(
            self._session_fabrik,
            user_id=user.id,
            tool_name=approval.tool_name,
            parameter=approval.parameter,
            ergebnis_kurz=f"Freigabe #{approval.id} {neuer_status}",
        )
        if neuer_status == STATUS_ABGELAUFEN:
            return Entscheidung(
                STATUS_ABGELAUFEN,
                "⌛ Diese Freigabe ist abgelaufen (15 Minuten) und wurde nicht ausgeführt.",
            )
        return Entscheidung(STATUS_ABGELEHNT, f"❌ Verworfen: {approval.vorschau_text}")

    async def _ausfuehren(self, approval: Approval, user: User) -> Entscheidung:
        tool = self._registry.hole(approval.tool_name)
        if tool is None or user.rolle not in tool.erlaubte_rollen:
            await protokolliere(
                self._session_fabrik,
                user_id=user.id,
                tool_name=approval.tool_name,
                parameter=approval.parameter,
                fehler="nicht verfügbar",
            )
            return Entscheidung(
                STATUS_GENEHMIGT, "⚠️ Das Tool ist nicht mehr verfügbar; nichts wurde ausgeführt."
            )
        ergebnis = await fuehre_tool_aus(tool, approval.parameter, user, self._kontext)
        if ergebnis.fehler:
            return Entscheidung(
                STATUS_GENEHMIGT, f"⚠️ Ausführung fehlgeschlagen: {approval.vorschau_text}"
            )
        return Entscheidung(STATUS_GENEHMIGT, f"✅ Ausgeführt: {approval.vorschau_text}")

    async def anzahl_offen(self, zeitpunkt: datetime | None = None) -> int:
        """Offene, noch nicht abgelaufene Freigaben."""
        grenze = (zeitpunkt or jetzt()) - FREIGABE_GUELTIGKEIT
        async with self._session_fabrik() as session:
            return await session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.status == STATUS_OFFEN, Approval.erstellt_am >= grenze)
            )
