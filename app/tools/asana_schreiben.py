"""Asana-Schreib-Tool: ein Änderungssatz, eine Freigabe."""

import logging
from collections import Counter

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    OP_ERLEDIGT,
    OP_FEHLGESCHLAGEN,
    OP_LAEUFT,
    OP_NICHT_AUSGEFUEHRT,
    AsanaOperation,
    jetzt,
)
from app.observability.audit import protokolliere

# Die Module mit weiteren Operationen tragen sich beim Import in OP_TYPEN ein.
from app.tools import (  # noqa: F401
    asana_ops_anhaenge,
    asana_ops_api,
    asana_ops_aufgaben,
    asana_ops_felder,
    asana_ops_mitglieder,
    asana_ops_verwaltung,
    asana_ops_vorlagen,
    asana_ops_wiederholung,
)
from app.tools.asana_client import AsanaClient
from app.tools.asana_operationen import (
    FARBEN,
    KATEGORIE_LOESCHEN,
    KATEGORIEN,
    LOESCH_MARKE,
    OP_TYPEN,
    ZUSATZ_BESCHREIBUNG,
    ZUSATZ_SCHEMA,
    Lauf,
    OpErgebnis,
    ist_loeschung,
    kategorie_von,
    loese_platzhalter_auf,
    pruefe_operationen,
    q,
)
from app.tools.base import (
    BasisTool,
    ToolFehler,
    ToolKontext,
    aktuelle_freigabe,
    aktueller_nutzer,
    zweifach_bestaetigt,
)

log = logging.getLogger(__name__)

STATUS_ERFOLGREICH = "erfolgreich"
STATUS_ABGEBROCHEN = "abgebrochen"
MAX_ZEILEN_IM_ERGEBNIS = 40

_VERGANGENHEIT = {
    "anlegen": "angelegt",
    "ändern": "geändert",
    "verschieben": "verschoben",
    "kommentieren": "kommentiert",
    "löschen": "gelöscht",
}


def zusammenfassung(operationen: list[dict], vergangenheit: bool = False) -> str:
    """Zahlen je Kategorie, z. B. „12 anlegen, 3 ändern, 2 🗑 löschen“."""
    anzahl = Counter(kategorie_von(op) for op in operationen)
    teile = []
    for kategorie in KATEGORIEN:
        if anzahl[kategorie]:
            wort = _VERGANGENHEIT[kategorie] if vergangenheit else kategorie
            symbol = "🗑 " if kategorie == KATEGORIE_LOESCHEN else ""
            teile.append(f"{anzahl[kategorie]} {symbol}{wort}")
    return ", ".join(teile)


class AsanaAenderungenAusfuehren(BasisTool):
    name = "asana_aenderungen_ausfuehren"
    beschreibung = (
        "Ändert Asana über einen Änderungssatz: eine Liste von Operationen, die der Nutzer "
        "gemeinsam mit EINEM Klick freigibt. Das ist der einzige Weg, in Asana etwas zu "
        "ändern. Es wird nichts ausgeführt, bevor der Nutzer freigegeben hat. Packe "
        "zusammengehörige Änderungen in einen Satz; die Operationen laufen in der angegebenen "
        "Reihenfolge.\n"
        "Jede Operation hat das Feld „operation“ und dazu ihre eigenen Felder:\n"
        "- projekt_anlegen: name, beschreibung, team_gid, faellig, farbe\n"
        "- projekt_aendern: gid, name, beschreibung, faellig, besitzer_gid, farbe\n"
        "- projekt_archivieren: gid, archiviert (false = wiederherstellen)\n"
        "- abschnitt_anlegen: projekt, name, vor_abschnitt_gid\n"
        "- abschnitt_umbenennen: gid, name\n"
        "- aufgabe_anlegen: name, beschreibung, projekt, abschnitt, uebergeordnet (macht sie "
        "zur Unteraufgabe), meilenstein, startdatum, startzeit, faellig, faellig_um, "
        "zustaendig_gid, tags, follower. Termine gibt es in genau drei Formen: (1) nur "
        "Fälligkeit: faellig (JJJJ-MM-TT) ODER faellig_um (Datum plus Uhrzeit, z. B. "
        "2026-10-15T14:30); (2) ganze Tage von–bis: startdatum + faellig; (3) Zeitfenster "
        "von–bis mit Uhrzeit: startzeit + faellig_um. Start ohne Uhrzeit und Fälligkeit mit "
        "Uhrzeit (oder umgekehrt) lassen sich nicht mischen. Uhrzeiten sind Ortszeit "
        "Europe/Berlin\n"
        "- aufgabe_aendern: gid plus jedes Feld aus aufgabe_anlegen. Ein leerer Wert löscht "
        "startdatum, startzeit, faellig, faellig_um oder zustaendig_gid. Wird nur Start oder "
        "nur Fälligkeit geändert, bleibt der andere Wert, muss aber in der Form dazu passen. "
        "tags und follower werden "
        "hinzugefügt, Vorhandenes bleibt; projekt/abschnitt fügen die Aufgabe zusätzlich hinzu\n"
        "- aufgabe_erledigen: gid, erledigt (false = wieder öffnen)\n"
        "- aufgabe_verschieben: gid, projekt, abschnitt, aus_projekt_entfernen\n"
        "- kommentar_hinzufuegen: aufgabe_gid, text\n"
        "- tag_anlegen: name, farbe\n"
        "- tag_zuweisen: aufgabe_gid, tag_gid, entfernen\n"
        "- abhaengigkeit_setzen: aufgabe_gid, haengt_ab_von_gid, entfernen\n"
        "- projekt_loeschen, abschnitt_loeschen, aufgabe_loeschen: gid. Löschen braucht eine "
        "zweite Bestätigung des Nutzers und ist nicht rückgängig zu machen. Die GID muss aus "
        "einem Lese-Tool stammen, nie nach Name allein löschen. Abschnitte lassen sich nur "
        "leer löschen\n" + "\n".join(ZUSATZ_BESCHREIBUNG) + "\n"
        "Platzhalter: Eine …_anlegen-Operation kann „platzhalter“ setzen ($p1 für Projekte, "
        "$s1 für Abschnitte, $a1 für Aufgaben, $t1 für Tags). Spätere Operationen desselben "
        "Satzes verwenden den Platzhalter überall dort, wo sonst eine GID steht. Alle anderen "
        "GIDs müssen aus einem Lese-Tool stammen. Ein Termin ist eine Aufgabe oder ein "
        "Meilenstein mit Fälligkeit, auf Wunsch als Zeitfenster mit Start- und Endzeit."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "operationen": {
                "type": "array",
                "minItems": 1,
                "description": "Operationen in der Reihenfolge der Ausführung",
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": sorted(OP_TYPEN)},
                        "platzhalter": {
                            "type": "string",
                            "description": "Nur bei …_anlegen: Name für das neue Objekt, z. B. $a1",
                        },
                        "gid": {"type": "string", "description": "GID des Objekts"},
                        "name": {"type": "string"},
                        "beschreibung": {"type": "string"},
                        "text": {"type": "string", "description": "Text des Kommentars"},
                        "projekt": {
                            "type": "string",
                            "description": "Projekt-GID oder Platzhalter",
                        },
                        "abschnitt": {
                            "type": "string",
                            "description": "Abschnitts-GID oder Platzhalter",
                        },
                        "vor_abschnitt_gid": {"type": "string"},
                        "uebergeordnet": {
                            "type": "string",
                            "description": "GID oder Platzhalter der übergeordneten Aufgabe",
                        },
                        "aufgabe_gid": {"type": "string"},
                        "haengt_ab_von_gid": {"type": "string"},
                        "tag_gid": {"type": "string"},
                        "team_gid": {"type": "string"},
                        "besitzer_gid": {"type": "string"},
                        "zustaendig_gid": {
                            "type": ["string", "null"],
                            "description": "Nutzer-GID oder „me“",
                        },
                        "startdatum": {"type": ["string", "null"], "description": "JJJJ-MM-TT"},
                        "startzeit": {
                            "type": ["string", "null"],
                            "description": "Beginn eines Zeitfensters, JJJJ-MM-TTTHH:MM, "
                            "Ortszeit Europe/Berlin; nur zusammen mit faellig_um, nie zusammen "
                            "mit startdatum",
                        },
                        "faellig": {"type": ["string", "null"], "description": "JJJJ-MM-TT"},
                        "faellig_um": {
                            "type": ["string", "null"],
                            "description": "JJJJ-MM-TTTHH:MM, Ortszeit Europe/Berlin",
                        },
                        "farbe": {"type": "string", "enum": list(FARBEN)},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "follower": {"type": "array", "items": {"type": "string"}},
                        "meilenstein": {"type": "boolean"},
                        "erledigt": {"type": "boolean"},
                        "archiviert": {"type": "boolean"},
                        "entfernen": {"type": "boolean"},
                        "aus_projekt_entfernen": {"type": "boolean"},
                        **ZUSATZ_SCHEMA,
                    },
                    "required": ["operation"],
                },
            }
        },
        "required": ["operationen"],
    }
    schreibend = True
    ergebnis_im_verlauf = True

    def __init__(self, kontext: ToolKontext) -> None:
        super().__init__(kontext)
        self.asana = AsanaClient(kontext)

    def _pruefe(self, operationen: object) -> list[dict]:
        maximum = self.kontext.settings.asana_max_ops_per_changeset
        if isinstance(operationen, list) and len(operationen) > maximum:
            raise ToolFehler(
                f"Der Änderungssatz hat {len(operationen)} Operationen, erlaubt sind höchstens "
                f"{maximum}. Bitte in mehrere Sätze aufteilen."
            )
        ops = pruefe_operationen(operationen)
        self._pruefe_rollen(ops)
        self._pruefe_loeschungen(ops)
        return ops

    def _pruefe_rollen(self, ops: list[dict]) -> None:
        """Manche Operationen sind bestimmten Rollen vorbehalten (z. B. Team-Verwaltung)."""
        for op in ops:
            einstellung = OP_TYPEN[op["operation"]].rollen
            if einstellung is None:
                continue
            if aktueller_nutzer.get().rolle not in getattr(self.kontext.settings, einstellung):
                raise ToolFehler(
                    f"Die Operation {op['operation']} ist dieser Rolle nicht erlaubt "
                    f"({einstellung.upper()})."
                )

    def _pruefe_loeschungen(self, ops: list[dict]) -> None:
        anzahl = sum(1 for op in ops if ist_loeschung(op))
        if not anzahl:
            return
        settings = self.kontext.settings
        if not settings.asana_delete_enabled:
            raise ToolFehler(
                "Löschen in Asana ist abgeschaltet (ASANA_DELETE_ENABLED=false). Es wurde "
                "nichts vorgeschlagen."
            )
        if aktueller_nutzer.get().rolle not in settings.asana_delete_roles:
            raise ToolFehler("Dieser Nutzer darf in Asana nichts löschen.")
        if anzahl > settings.asana_max_deletes_per_changeset:
            raise ToolFehler(
                f"Der Änderungssatz enthält {anzahl} Löschoperationen, erlaubt sind höchstens "
                f"{settings.asana_max_deletes_per_changeset}. Bitte aufteilen."
            )

    def zweite_bestaetigung(
        self, vorschau_text: str, operationen: list | None = None
    ) -> str | None:
        """Sätze mit Löschungen brauchen nach dem ersten ✅ eine zweite Rückfrage."""
        ops = [op for op in operationen or [] if isinstance(op, dict)]
        anzahl = sum(1 for op in ops if op.get("operation") in OP_TYPEN and ist_loeschung(op))
        if not anzahl:
            return None
        zeilen = [f"Wirklich löschen? {anzahl} {'Objekt' if anzahl == 1 else 'Objekte'}"]
        zeilen += [zeile for zeile in vorschau_text.splitlines() if LOESCH_MARKE in zeile]
        zeilen.append("Gelöschtes kann in der Regel nicht zuverlässig wiederhergestellt werden.")
        andere = len(ops) - anzahl
        if andere == 1:
            zeilen.append(
                "Auch die eine übrige Operation des Satzes läuft erst nach dieser Bestätigung."
            )
        elif andere:
            zeilen.append(
                f"Auch die übrigen {andere} Operationen des Satzes laufen erst nach dieser "
                "Bestätigung."
            )
        return "\n".join(zeilen)

    def vorschau(self, **params) -> str:
        raise ToolFehler("Die Vorschau braucht den aktuellen Stand aus Asana.")

    async def bereite_vor(self, operationen: list | None = None) -> str:
        """Liest den aktuellen Zustand und beschreibt jede Operation. Ändert nichts."""
        ops = self._pruefe(operationen)
        zeilen = [f"Asana-Änderungssatz: {zusammenfassung(ops)}"]
        async with self.asana as asana:
            lauf = Lauf(asana, self.kontext)
            for nummer, op in enumerate(ops, start=1):
                try:
                    zeile = await OP_TYPEN[op["operation"]].vorschau(op, lauf)
                except ToolFehler as exc:
                    raise ToolFehler(f"Operation {nummer}: {exc}") from None
                zeilen.append(f"{nummer}. {zeile}")
        return "\n".join(zeilen)

    async def ausfuehren(self, operationen: list | None = None) -> dict:
        approval_id = aktuelle_freigabe.get()
        if approval_id is None:
            raise ToolFehler("Asana-Änderungen laufen nur nach einer Freigabe durch den Nutzer.")
        ops = self._pruefe(operationen)
        if any(ist_loeschung(op) for op in ops) and not zweifach_bestaetigt.get():
            raise ToolFehler("Löschen läuft nur nach der zweiten Bestätigung durch den Nutzer.")
        if not await self._reserviere(approval_id, ops):
            raise ToolFehler(
                "Dieser Änderungssatz wurde bereits ausgeführt. Es wird nichts erneut ausgeführt."
            )

        eintraege = [
            {"nr": nummer, "operation": op["operation"], "status": OP_NICHT_AUSGEFUEHRT}
            for nummer, op in enumerate(ops, start=1)
        ]
        audit: list[dict] = []
        gids: dict[str, str] = {}
        link = projekt_link = ""
        async with self.asana as asana:
            lauf = Lauf(asana, self.kontext)
            for eintrag, op in zip(eintraege, ops, strict=True):
                await self._setze(approval_id, eintrag["nr"], status=OP_LAEUFT)
                try:
                    ergebnis: OpErgebnis = await OP_TYPEN[op["operation"]].ausfuehren(
                        loese_platzhalter_auf(op, gids), lauf
                    )
                except ToolFehler as exc:
                    fehler = str(exc)
                except Exception as exc:
                    log.exception("Unerwarteter Fehler in Operation %s", op["operation"])
                    fehler = f"Interner Fehler ({type(exc).__name__})"
                else:
                    fehler = None
                if fehler is not None:
                    # Kein Wiederholen, kein Zurückrollen: Der Satz hört hier auf.
                    eintrag.update(status=OP_FEHLGESCHLAGEN, fehler=fehler, text=_kurz(op))
                    audit.append(
                        {
                            "nr": eintrag["nr"],
                            "operation": op["operation"],
                            **_api_angaben(op),
                            "ergebnis": fehler,
                        }
                    )
                    await self._setze(
                        approval_id, eintrag["nr"], status=OP_FEHLGESCHLAGEN, fehler=fehler
                    )
                    break
                if "platzhalter" in op:
                    gids[op["platzhalter"]] = ergebnis.gid
                link = link or ergebnis.link
                if ergebnis.ist_projekt:
                    projekt_link = projekt_link or ergebnis.link
                eintrag.update(status=OP_ERLEDIGT, gid=ergebnis.gid, text=ergebnis.text)
                audit.append(
                    {
                        "nr": eintrag["nr"],
                        "operation": op["operation"],
                        "gid": ergebnis.gid,
                        "felder": list(ergebnis.felder),
                        "vorher": ergebnis.vorher,
                        **ergebnis.audit,
                        "ergebnis": OP_ERLEDIGT,
                    }
                )
                await self._setze(
                    approval_id,
                    eintrag["nr"],
                    status=OP_ERLEDIGT,
                    gid=ergebnis.gid,
                    ausgefuehrt_am=jetzt(),
                )
        await self._schliesse_ab(approval_id)

        erledigt = sum(1 for e in eintraege if e["status"] == OP_ERLEDIGT)
        for eintrag, op in zip(eintraege, ops, strict=True):
            if eintrag["status"] == OP_NICHT_AUSGEFUEHRT:
                audit.append(
                    {
                        "nr": eintrag["nr"],
                        "operation": op["operation"],
                        "ergebnis": OP_NICHT_AUSGEFUEHRT,
                    }
                )
        await protokolliere(
            self.kontext.session_fabrik,
            user_id=aktueller_nutzer.get().id,
            tool_name=self.name,
            parameter={"freigabe": approval_id, "operationen": audit},
            ergebnis_kurz=f"Änderungssatz: {erledigt} von {len(ops)} Operationen erledigt",
            fehler=None if erledigt == len(ops) else "abgebrochen",
        )
        return {
            "status": STATUS_ERFOLGREICH if erledigt == len(ops) else STATUS_ABGEBROCHEN,
            "erledigt": erledigt,
            "gesamt": len(ops),
            "zusammenfassung": zusammenfassung(
                [op for e, op in zip(eintraege, ops, strict=True) if e["status"] == OP_ERLEDIGT],
                vergangenheit=True,
            ),
            "link": projekt_link or link,
            "operationen": eintraege,
        }

    def ergebnis_text(self, ergebnis: dict) -> str:
        eintraege = ergebnis["operationen"]
        link = f"\n{ergebnis['link']}" if ergebnis.get("link") else ""
        if ergebnis["status"] == STATUS_ERFOLGREICH:
            return f"✅ Asana-Änderungssatz ausgeführt: {ergebnis['zusammenfassung']}.{link}"
        fehlgeschlagen = next(e for e in eintraege if e["status"] == OP_FEHLGESCHLAGEN)
        erledigt = [e for e in eintraege if e["status"] == OP_ERLEDIGT]
        offen = [e for e in eintraege if e["status"] == OP_NICHT_AUSGEFUEHRT]
        zeilen = [
            f"⚠️ Asana-Änderungssatz abgebrochen bei Operation {fehlgeschlagen['nr']} "
            f"von {ergebnis['gesamt']}.",
            f"Erledigt ({len(erledigt)}):" if erledigt else "Erledigt: nichts.",
        ]
        zeilen += [f"{e['nr']}. {e['text']}" for e in erledigt[:MAX_ZEILEN_IM_ERGEBNIS]]
        if len(erledigt) > MAX_ZEILEN_IM_ERGEBNIS:
            zeilen.append(f"… und {len(erledigt) - MAX_ZEILEN_IM_ERGEBNIS} weitere")
        zeilen.append("Fehlgeschlagen:")
        zeilen.append(
            f"{fehlgeschlagen['nr']}. {fehlgeschlagen['text']} – {fehlgeschlagen['fehler']}"
        )
        if offen:
            bereich = (
                str(offen[0]["nr"]) if len(offen) == 1 else f"{offen[0]['nr']}–{offen[-1]['nr']}"
            )
            zeilen.append(f"Nicht mehr ausgeführt: Operation {bereich} ({len(offen)}).")
        zeilen.append("Es wurde nichts wiederholt und nichts zurückgerollt.")
        return "\n".join(zeilen) + link

    async def _reserviere(self, approval_id: int, ops: list[dict]) -> bool:
        """Legt die Operationen der Freigabe an. False: Der Satz lief schon einmal an."""
        async with self.kontext.session_fabrik() as session:
            vorhanden = await session.scalar(
                select(func.count())
                .select_from(AsanaOperation)
                .where(AsanaOperation.approval_id == approval_id)
            )
            if vorhanden:
                return False
            session.add_all(
                AsanaOperation(approval_id=approval_id, position=nummer, art=op["operation"])
                for nummer, op in enumerate(ops, start=1)
            )
            try:
                await session.commit()
            except IntegrityError:
                return False
        return True

    async def _setze(self, approval_id: int, position: int, **werte) -> None:
        async with self.kontext.session_fabrik() as session:
            await session.execute(
                update(AsanaOperation)
                .where(
                    AsanaOperation.approval_id == approval_id,
                    AsanaOperation.position == position,
                )
                .values(**werte)
            )
            await session.commit()

    async def _schliesse_ab(self, approval_id: int) -> None:
        """Alles, was nach einem Abbruch noch offen ist, gilt als nicht ausgeführt."""
        async with self.kontext.session_fabrik() as session:
            await session.execute(
                update(AsanaOperation)
                .where(
                    AsanaOperation.approval_id == approval_id,
                    AsanaOperation.status.notin_((OP_ERLEDIGT, OP_FEHLGESCHLAGEN)),
                )
                .values(status=OP_NICHT_AUSGEFUEHRT)
            )
            await session.commit()


def _api_angaben(op: dict) -> dict:
    """Beim allgemeinen API-Aufruf gehören Methode, Pfad und Body auch bei Fehlern ins Log."""
    return {feld: op[feld] for feld in ("methode", "pfad", "abfrage", "body") if feld in op}


def _kurz(op: dict) -> str:
    return op["operation"] + (f" {q(op['name'])}" if op.get("name") else "")
