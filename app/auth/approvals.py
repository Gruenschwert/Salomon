"""Freigabe-Flow: Schreibende Tools laufen erst nach ✅ des anfragenden Nutzers."""

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app.auth.kontext import NutzerKontext
from app.auth.tresor import Tresor, TresorFehler
from app.auth.users import finde_erlaubten_nutzer
from app.channels.base import FreigabeAnfrage
from app.db.models import (
    STATUS_ABGELAUFEN,
    STATUS_ABGELEHNT,
    STATUS_BESTAETIGUNG,
    STATUS_GENEHMIGT,
    STATUS_OFFEN,
    Approval,
    jetzt,
)
from app.db.session import db_sitzung
from app.mail.lauf import aktueller_mail_lauf
from app.mail.schutz import ENTFERNT, VERSIEGELT, warnung_anderer_dienst
from app.observability.audit import EREIGNIS_UNBEKANNT, protokolliere
from app.tools.base import (
    Tool,
    ToolFehler,
    ToolKontext,
    aktuelle_freigabe,
    aktueller_nutzer,
    zweifach_bestaetigt,
)
from app.tools.registry import Registry, audit_angaben, fuehre_tool_aus

log = logging.getLogger(__name__)

FREIGABE_GUELTIGKEIT = timedelta(minutes=15)
ZWECK_FREIGABE = "freigabe"
NICHT_LESBAR_TEXT = (
    "⚠️ Diese Freigabe lässt sich nicht mehr lesen; nichts wurde ausgeführt. Bitte stoße die "
    "Aktion noch einmal an."
)

STATUS_NICHT_GEFUNDEN = "nicht_gefunden"
NICHT_DEINE_FREIGABE_TEXT = (
    "Diese Freigabe gibt es nicht (mehr) oder sie gehört jemand anderem. Bestätigen kann nur, "
    "wer den Änderungssatz angestoßen hat."
)
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
        """False, wenn der Klick nichts entschieden hat: Die Buttons bleiben dann stehen, weil
        die Freigabe für die Person, der sie gehört, noch offen sein kann."""
        return self.status != STATUS_NICHT_GEFUNDEN


class Freigaben:
    def __init__(self, kontext: ToolKontext, registry: Registry) -> None:
        self._kontext = kontext
        self._session_fabrik = kontext.session_fabrik
        self._registry = registry

    async def anfragen(self, user: NutzerKontext, tool: Tool, params: dict) -> FreigabeAnfrage:
        """Legt eine offene Freigabe an. Das Tool wird dabei NICHT ausgeführt."""
        marke = aktueller_nutzer.set(user)
        try:
            vorschau = await tool.bereite_vor(**params)
        finally:
            aktueller_nutzer.reset(marke)
        gespeichert, kurz = params, vorschau
        if tool.vertraulich:
            # Empfänger, Betreff und Text stehen nie im Klartext in der Datenbank: Die
            # Parameter werden mit dem Schlüssel der Person versiegelt, und als Vorschau
            # bleibt nur eine Zeile ohne Inhalt. Die volle Vorschau sieht die Person im Chat.
            gespeichert = self._versiegle(user, params)
            kurz = tool.neutrale_vorschau(params)
        async with db_sitzung(self._session_fabrik, user) as session:
            approval = Approval(
                user_id=user.id, tool_name=tool.name, parameter=gespeichert, vorschau_text=kurz
            )
            session.add(approval)
            await session.commit()
        await protokolliere(
            self._session_fabrik,
            user_id=user.id,
            tool_name=tool.name,
            parameter=audit_angaben(tool, params, None)[0],
            ergebnis_kurz=f"Freigabe #{approval.id} angefragt",
        )
        anzahl, kompakt = tool.vorschau_darstellung(vorschau, params)
        # Wurde in diesem Lauf Mailinhalt gelesen, steht das in der Vorschau jedes anderen
        # Dienstes: Aus einer Mail entsteht nie von selbst eine Änderung in Asana oder Shopify.
        # Die Mail-Tools nennen ihre Herkunft selbst.
        warnung = None if tool.vertraulich else warnung_anderer_dienst(aktueller_mail_lauf.get())
        if warnung:
            vorschau = f"{vorschau}\n\n{warnung}"
            kompakt = f"{kompakt}\n\n{warnung}" if kompakt else None
        return FreigabeAnfrage(
            approval_id=approval.id, vorschau_text=vorschau, anzahl=anzahl, kompakt_text=kompakt
        )

    def _tresor(self) -> Tresor:
        try:
            tresor = Tresor.aus_settings(self._kontext.settings)
        except TresorFehler:
            raise ToolFehler("Der Schlüssel für vertrauliche Freigaben ist ungültig.") from None
        if not tresor.verfuegbar:
            raise ToolFehler("Der Schlüssel für vertrauliche Freigaben fehlt.")
        return tresor

    def _versiegle(self, user: NutzerKontext, params: dict) -> dict:
        versiegelt = self._tresor().versiegle(
            user.id, ZWECK_FREIGABE, json.dumps(params, ensure_ascii=False)
        )
        return {VERSIEGELT: base64.b64encode(versiegelt).decode("ascii")}

    def _parameter(self, approval: Approval, user: NutzerKontext) -> dict | None:
        """Die Parameter einer Freigabe im Klartext; None, wenn sie nicht mehr lesbar sind."""
        parameter = approval.parameter
        if not isinstance(parameter, dict) or VERSIEGELT not in parameter:
            return None if parameter == ENTFERNT else parameter
        try:
            versiegelt = base64.b64decode(parameter[VERSIEGELT])
            return json.loads(self._tresor().entsiegle(user.id, ZWECK_FREIGABE, versiegelt))
        except (ToolFehler, TresorFehler, ValueError):
            log.warning("Versiegelte Freigabe #%s ist nicht lesbar", approval.id)
            return None

    async def _entferne_parameter(self, approval_id: int, user: NutzerKontext) -> None:
        """Nach der Entscheidung bleibt von einer vertraulichen Freigabe kein Inhalt zurück."""
        async with db_sitzung(self._session_fabrik, user) as session:
            await session.execute(
                update(Approval).where(Approval.id == approval_id).values(parameter=ENTFERNT)
            )
            await session.commit()

    async def verwerfen(self, approval_id: int, grund: str, nutzer: NutzerKontext) -> bool:
        """Verwirft eine eigene Freigabe, die die Person nicht erreicht hat.

        Ohne sichtbare Buttons darf nichts offen bleiben, was später ausgeführt werden könnte.
        """
        async with db_sitzung(self._session_fabrik, nutzer) as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                return False
            await session.execute(
                update(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.status.in_([STATUS_OFFEN, STATUS_BESTAETIGUNG]),
                )
                .values(status=STATUS_ABGELEHNT, entschieden_am=jetzt())
            )
            await session.commit()
        tool = self._registry.hole(approval.tool_name)
        if tool is None or tool.vertraulich:
            await self._entferne_parameter(approval_id, nutzer)
        await protokolliere(
            self._session_fabrik,
            user_id=approval.user_id,
            tool_name=approval.tool_name,
            parameter={"freigabe": approval_id},
            ergebnis_kurz=f"Freigabe #{approval_id} verworfen",
            fehler=f"nicht zustellbar: {grund}",
        )
        return True

    async def stand(self, nutzer: NutzerKontext, zeitpunkt: datetime | None = None) -> str:
        """Kurzer Stand der letzten Freigaben eines Nutzers für den System-Prompt.

        So weiß Claude bei „mach das“, ob etwas offen, verworfen, abgelaufen oder erledigt ist.
        """
        zeitpunkt = zeitpunkt or jetzt()
        async with db_sitzung(self._session_fabrik, nutzer) as session:
            letzte = list(
                await session.scalars(
                    select(Approval)
                    .where(Approval.user_id == nutzer.nutzer_id)
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

        # Die Sitzung sieht nur Freigaben dieser Person. Eine fremde Freigabe ist für sie
        # deshalb gar nicht vorhanden; die zweite Prüfung ist nur ein zusätzliches Netz.
        async with db_sitzung(self._session_fabrik, user) as session:
            approval = await session.get(Approval, approval_id)
            if approval is None or approval.user_id != user.id:
                return Entscheidung(STATUS_NICHT_GEFUNDEN, NICHT_DEINE_FREIGABE_TEXT)
            tool = self._registry.hole(approval.tool_name)
            vertraulich = bool(tool and tool.vertraulich) or (
                isinstance(approval.parameter, dict) and VERSIEGELT in approval.parameter
            )
            parameter = self._parameter(approval, user)
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
                if tool is not None and parameter is not None:
                    frage = tool.zweite_bestaetigung(approval.vorschau_text, **parameter)
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
        if vertraulich:
            # Entschieden ist entschieden: Der versiegelte Inhalt wird jetzt entfernt, auch
            # vor der Ausführung, die mit den Parametern im Arbeitsspeicher läuft.
            await self._entferne_parameter(approval.id, user)
        if neuer_status == STATUS_GENEHMIGT:
            if parameter is None:
                return Entscheidung(STATUS_GENEHMIGT, NICHT_LESBAR_TEXT, user_id=user.id)
            return await self._ausfuehren(approval, user, parameter, zweifach=bestaetigt)

        await protokolliere(
            self._session_fabrik,
            user_id=user.id,
            tool_name=approval.tool_name,
            parameter={"freigabe": approval.id}
            if vertraulich or tool is None
            else audit_angaben(tool, parameter, None)[0],
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

    async def _ausfuehren(
        self, approval: Approval, user: NutzerKontext, parameter: dict, zweifach: bool
    ) -> Entscheidung:
        tool = self._registry.hole(approval.tool_name)
        if tool is None or not user.darf(*tool.erforderliche_rechte):
            await protokolliere(
                self._session_fabrik,
                user_id=user.id,
                tool_name=approval.tool_name,
                parameter={"freigabe": approval.id},
                fehler="nicht verfügbar",
            )
            return Entscheidung(
                STATUS_GENEHMIGT, "⚠️ Das Tool ist nicht mehr verfügbar; nichts wurde ausgeführt."
            )
        marke = aktuelle_freigabe.set(approval.id)
        marke_zweifach = zweifach_bestaetigt.set(zweifach)
        try:
            ergebnis = await fuehre_tool_aus(tool, parameter, user, self._kontext)
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

    async def anzahl_offen(self, nutzer: NutzerKontext, zeitpunkt: datetime | None = None) -> int:
        """Eigene, noch nicht abgelaufene Freigaben, die auf einen Klick warten."""
        grenze = (zeitpunkt or jetzt()) - FREIGABE_GUELTIGKEIT
        async with db_sitzung(self._session_fabrik, nutzer) as session:
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
