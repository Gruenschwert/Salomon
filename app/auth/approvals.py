"""Freigabe-Flow: Schreibende Tools laufen erst nach ✅ des anfragenden Nutzers."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app.auth.users import finde_erlaubten_nutzer
from app.channels.base import FreigabeAnfrage
from app.db.models import (
    STATUS_ABGELAUFEN,
    STATUS_ABGELEHNT,
    STATUS_BESTAETIGUNG,
    STATUS_GENEHMIGT,
    STATUS_OFFEN,
    Approval,
    User,
    jetzt,
)
from app.observability.audit import EREIGNIS_UNBEKANNT, protokolliere
from app.tools.base import (
    Tool,
    ToolKontext,
    aktuelle_freigabe,
    aktueller_nutzer,
    zweifach_bestaetigt,
)
from app.tools.registry import Registry, fuehre_tool_aus

FREIGABE_GUELTIGKEIT = timedelta(minutes=15)

STATUS_NICHT_GEFUNDEN = "nicht_gefunden"
STATUS_FREMDER_NUTZER = "fremder_nutzer"
STATUS_BEREITS_ENTSCHIEDEN = "bereits_entschieden"

MAX_KURZTEXT_ZEICHEN = 200
# So viele und so alte Freigaben nennt `stand` höchstens.
STAND_ANZAHL = 3
STAND_FENSTER = timedelta(hours=2)


@dataclass(frozen=True)
class Entscheidung:
    status: str
    text: str
    # True: Der Kanal schreibt den Text in den Gesprächsverlauf, damit Claude das Ergebnis kennt.
    im_verlauf: bool = False
    user_id: int | None = None
    # True: `text` ist die zweite Rückfrage; der Kanal zeigt sie mit eigenen Buttons.
    rueckfrage: bool = False

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
        marke = aktueller_nutzer.set(user)
        try:
            vorschau = await tool.bereite_vor(**params)
        finally:
            aktueller_nutzer.reset(marke)
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
        anzahl, kompakt = tool.vorschau_darstellung(vorschau, params)
        return FreigabeAnfrage(
            approval_id=approval.id, vorschau_text=vorschau, anzahl=anzahl, kompakt_text=kompakt
        )

    async def verwerfen(self, approval_id: int, grund: str) -> int | None:
        """Verwirft eine Freigabe, die den Nutzer nicht erreicht hat. Liefert dessen User-ID.

        Ohne sichtbare Buttons darf nichts offen bleiben, was später ausgeführt werden könnte.
        """
        async with self._session_fabrik() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                return None
            await session.execute(
                update(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.status.in_([STATUS_OFFEN, STATUS_BESTAETIGUNG]),
                )
                .values(status=STATUS_ABGELEHNT, entschieden_am=jetzt())
            )
            await session.commit()
        await protokolliere(
            self._session_fabrik,
            user_id=approval.user_id,
            tool_name=approval.tool_name,
            parameter={"freigabe": approval_id},
            ergebnis_kurz=f"Freigabe #{approval_id} verworfen",
            fehler=f"nicht zustellbar: {grund}",
        )
        return approval.user_id

    async def stand(self, user_id: int, zeitpunkt: datetime | None = None) -> str:
        """Kurzer Stand der letzten Freigaben eines Nutzers für den System-Prompt.

        So weiß Claude bei „mach das“, ob etwas offen, verworfen, abgelaufen oder erledigt ist.
        """
        zeitpunkt = zeitpunkt or jetzt()
        async with self._session_fabrik() as session:
            letzte = list(
                await session.scalars(
                    select(Approval)
                    .where(Approval.user_id == user_id)
                    .order_by(Approval.id.desc())
                    .limit(STAND_ANZAHL)
                )
            )
        zeilen = []
        for approval in letzte:
            alter = zeitpunkt - approval.erstellt_am
            if alter > STAND_FENSTER:
                continue
            status = approval.status
            if status in (STATUS_OFFEN, STATUS_BESTAETIGUNG) and alter > FREIGABE_GUELTIGKEIT:
                status = "abgelaufen, nichts wurde ausgeführt"
            elif status == STATUS_OFFEN:
                status = "wartet auf ✅ oder ❌ des Nutzers"
            elif status == STATUS_BESTAETIGUNG:
                status = "wartet auf die zweite Bestätigung zum Löschen"
            elif status == STATUS_ABGELEHNT:
                status = "verworfen, nichts wurde ausgeführt"
            elif status == STATUS_GENEHMIGT:
                status = "freigegeben und gelaufen (Ergebnis steht im Verlauf)"
            minuten = max(0, int(alter.total_seconds() // 60))
            kurz = _kurztext(approval.vorschau_text)
            zeilen.append(f"- #{approval.id} vor {minuten} min: {kurz} – {status}")
        return "\n".join(zeilen)

    async def entscheiden(
        self,
        approval_id: int,
        telegram_id: int,
        genehmigt: bool,
        zeitpunkt: datetime | None = None,
        bestaetigt: bool = False,
    ) -> Entscheidung | None:
        """Verarbeitet einen Klick auf ✅ / ❌. None bedeutet: Klick eines unbekannten Nutzers.

        `bestaetigt=True` ist der Klick auf die zweite Rückfrage („🗑 Ja, löschen“). Er zählt
        nur, wenn das erste ✅ schon da ist.
        """
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
            tool = self._registry.hole(approval.tool_name)
            frage = None
            # Ablehnen und Ablaufen gehen in beiden Stufen, Zustimmen nur in der passenden.
            erlaubte_stufen = [STATUS_OFFEN, STATUS_BESTAETIGUNG]
            if zeitpunkt - approval.erstellt_am > FREIGABE_GUELTIGKEIT:
                neuer_status = STATUS_ABGELAUFEN
            elif not genehmigt:
                neuer_status = STATUS_ABGELEHNT
            elif bestaetigt:
                neuer_status = STATUS_GENEHMIGT
                erlaubte_stufen = [STATUS_BESTAETIGUNG]
            else:
                if tool is not None:
                    frage = tool.zweite_bestaetigung(approval.vorschau_text, **approval.parameter)
                neuer_status = STATUS_BESTAETIGUNG if frage else STATUS_GENEHMIGT
                erlaubte_stufen = [STATUS_OFFEN]
            # Bedingtes Update: Jede Stufe kann nur einmal entschieden werden (Doppelklick).
            ergebnis = await session.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status.in_(erlaubte_stufen))
                .values(status=neuer_status, entschieden_am=zeitpunkt)
            )
            await session.commit()
            if ergebnis.rowcount != 1:
                return Entscheidung(
                    STATUS_BEREITS_ENTSCHIEDEN, "Diese Freigabe wurde bereits entschieden."
                )

        if neuer_status == STATUS_BESTAETIGUNG:
            await protokolliere(
                self._session_fabrik,
                user_id=user.id,
                tool_name=approval.tool_name,
                parameter={"freigabe": approval.id},
                ergebnis_kurz=f"Freigabe #{approval.id}: zweite Bestätigung angefragt",
            )
            return Entscheidung(STATUS_BESTAETIGUNG, frage, user_id=user.id, rueckfrage=True)
        if neuer_status == STATUS_GENEHMIGT:
            return await self._ausfuehren(approval, user, zweifach=bestaetigt)

        await protokolliere(
            self._session_fabrik,
            user_id=user.id,
            tool_name=approval.tool_name,
            parameter=approval.parameter,
            ergebnis_kurz=f"Freigabe #{approval.id} {neuer_status}",
        )
        verlauf = {"im_verlauf": bool(tool and tool.ergebnis_im_verlauf), "user_id": user.id}
        if neuer_status == STATUS_ABGELAUFEN:
            return Entscheidung(
                STATUS_ABGELAUFEN,
                "⌛ Diese Freigabe ist abgelaufen (15 Minuten) und wurde nicht ausgeführt.",
                **verlauf,
            )
        return Entscheidung(
            STATUS_ABGELEHNT, f"❌ Verworfen: {_kurztext(approval.vorschau_text)}", **verlauf
        )

    async def _ausfuehren(self, approval: Approval, user: User, zweifach: bool) -> Entscheidung:
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
        marke = aktuelle_freigabe.set(approval.id)
        marke_zweifach = zweifach_bestaetigt.set(zweifach)
        try:
            ergebnis = await fuehre_tool_aus(tool, approval.parameter, user, self._kontext)
        finally:
            zweifach_bestaetigt.reset(marke_zweifach)
            aktuelle_freigabe.reset(marke)
        kurz = _kurztext(approval.vorschau_text)
        if ergebnis.fehler:
            # `ergebnis.text` stammt aus einem ToolFehler oder ist die neutrale Meldung.
            text = f"⚠️ Ausführung fehlgeschlagen: {kurz}\n{ergebnis.text}"
        else:
            text = tool.ergebnis_text(ergebnis.daten) or f"✅ Ausgeführt: {kurz}"
        return Entscheidung(
            STATUS_GENEHMIGT, text, im_verlauf=tool.ergebnis_im_verlauf, user_id=user.id
        )

    async def anzahl_offen(self, zeitpunkt: datetime | None = None) -> int:
        """Noch nicht abgelaufene Freigaben, die auf einen Klick warten."""
        grenze = (zeitpunkt or jetzt()) - FREIGABE_GUELTIGKEIT
        async with self._session_fabrik() as session:
            return await session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(
                    Approval.status.in_([STATUS_OFFEN, STATUS_BESTAETIGUNG]),
                    Approval.erstellt_am >= grenze,
                )
            )


def _kurztext(vorschau: str) -> str:
    """Erste Zeile der Vorschau; mehrzeilige Vorschauen werden nicht wiederholt."""
    zeile = vorschau.strip().split("\n", 1)[0]
    return zeile if len(zeile) <= MAX_KURZTEXT_ZEICHEN else zeile[: MAX_KURZTEXT_ZEICHEN - 1] + "…"
