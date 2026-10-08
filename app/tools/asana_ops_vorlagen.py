"""Operationen für Vorlagen und Kopien. Asana erledigt sie im Hintergrund als Job, auf den
hier gewartet wird."""

from datetime import date

from app.tools.asana_client import AsanaFehler
from app.tools.asana_operationen import (
    BOOL_FELDER,
    DATUM_FELDER,
    GID_FELDER,
    KATEGORIE_ANLEGEN,
    LISTEN_FELDER,
    OBJEKT_FELDER,
    ZUSATZ_BESCHREIBUNG,
    ZUSATZ_SCHEMA,
    Lauf,
    OpErgebnis,
    bez,
    datum_text,
    merke_neu,
    q,
    registriere,
)
from app.tools.base import ToolFehler

JOB_TIMEOUT_SEKUNDEN = 60
JOB_ABSTAND_SEKUNDEN = 2
# Laut Asana-Doku zu POST /tasks/{gid}/duplicate bzw. /projects/{gid}/duplicate
AUFGABE_EINSCHLIESSEN = (
    "assignee",
    "attachments",
    "dates",
    "dependencies",
    "followers",
    "notes",
    "parent",
    "projects",
    "subtasks",
    "tags",
)
PROJEKT_EINSCHLIESSEN = (
    "allocations",
    "forms",
    "members",
    "notes",
    "permissions",
    "task_assignee",
    "task_attachments",
    "task_dates",
    "task_dependencies",
    "task_followers",
    "task_notes",
    "task_projects",
    "task_subtasks",
    "task_tags",
    "task_templates",
    "task_type_default",
)

GID_FELDER.add("vorlage_gid")
OBJEKT_FELDER.update({"termine", "rollen"})
LISTEN_FELDER.add("einschliessen")
DATUM_FELDER.update({"termine_ab", "termine_bis"})
BOOL_FELDER.add("wochenenden_ueberspringen")
ZUSATZ_SCHEMA.update(
    {
        "vorlage_gid": {"type": "string", "description": "GID der Projekt- oder Aufgabenvorlage"},
        "termine": {
            "type": "object",
            "description": "Datumsvariablen der Projektvorlage: Name oder GID der Variable als "
            "Schlüssel, Datum JJJJ-MM-TT als Wert (siehe asana_vorlagen_anzeigen)",
        },
        "rollen": {
            "type": "object",
            "description": "Rollen der Projektvorlage: Name oder GID der Rolle als Schlüssel, "
            "Nutzer (GID, Name oder „me“) als Wert",
        },
        "einschliessen": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Was beim Duplizieren mitkopiert wird. Aufgabe: "
            + ", ".join(AUFGABE_EINSCHLIESSEN)
            + ". Projekt: "
            + ", ".join(PROJEKT_EINSCHLIESSEN),
        },
        "termine_ab": {
            "type": "string",
            "description": "Projektkopie: erstes Startdatum, alle Termine verschieben sich mit",
        },
        "termine_bis": {
            "type": "string",
            "description": "Projektkopie: letztes Fälligkeitsdatum, alle Termine verschieben sich",
        },
        "wochenenden_ueberspringen": {"type": "boolean"},
    }
)
ZUSATZ_BESCHREIBUNG.extend(
    [
        "- projekt_aus_vorlage: vorlage_gid, name, team_gid, termine (alle Datumsvariablen der "
        "Vorlage), rollen; Platzhalter $p1 steht danach für das neue Projekt",
        "- aufgabe_aus_vorlage: vorlage_gid, name",
        "- aufgabe_duplizieren: gid, name, einschliessen",
        "- projekt_duplizieren: gid, name, einschliessen, termine_ab ODER termine_bis, "
        "wochenenden_ueberspringen",
    ]
)


async def warte_auf_job(lauf: Lauf, job: dict, ergebnis_feld: str) -> dict:
    """Fragt den Job ab, bis er fertig ist. Liefert das neue Objekt (Projekt oder Aufgabe)."""
    gewartet = 0
    while True:
        status = job.get("status")
        if status == "succeeded":
            neu = job.get(ergebnis_feld)
            if not neu or not neu.get("gid"):
                raise AsanaFehler("Asana meldet den Vorgang als fertig, nennt aber kein Ergebnis.")
            return neu
        if status == "failed":
            raise AsanaFehler("Asana konnte den Vorgang nicht abschließen (Job fehlgeschlagen).")
        if gewartet >= JOB_TIMEOUT_SEKUNDEN:
            raise AsanaFehler(
                f"Asana ist nach {JOB_TIMEOUT_SEKUNDEN} Sekunden noch nicht fertig (Job "
                f"{job.get('gid')}). Der Vorgang läuft dort vermutlich weiter; bitte in Asana "
                "nachsehen. Es wurde nichts wiederholt."
            )
        await lauf.asana.warte(JOB_ABSTAND_SEKUNDEN)
        gewartet += JOB_ABSTAND_SEKUNDEN
        job = await lauf.asana.get(
            f"/jobs/{job['gid']}",
            felder=("status", f"{ergebnis_feld}.name", f"{ergebnis_feld}.permalink_url"),
        )


async def _ist_organisation(lauf: Lauf) -> bool:
    if "ist_organisation" not in lauf.zwischenspeicher:
        workspace = await lauf.asana.get(
            f"/workspaces/{await lauf.asana.workspace_gid()}", felder=("is_organization",)
        )
        lauf.zwischenspeicher["ist_organisation"] = bool(workspace.get("is_organization"))
    return lauf.zwischenspeicher["ist_organisation"]


def _zuordnung(angaben: dict, variablen: list[dict], wort: str) -> dict[str, object]:
    """Ordnet Angaben (Name oder GID -> Wert) den Variablen der Vorlage zu: GID -> Wert."""
    zugeordnet: dict[str, object] = {}
    for schluessel, wert in angaben.items():
        schluessel = str(schluessel).strip()
        treffer = [v for v in variablen if v["gid"] == schluessel] or [
            v for v in variablen if (v.get("name") or "").casefold() == schluessel.casefold()
        ]
        if len(treffer) != 1:
            vorhanden = ", ".join(f"{v.get('name', '')} ({v['gid']})" for v in variablen) or "keine"
            raise ToolFehler(
                f"Die Vorlage kennt {wort} „{schluessel}“ nicht eindeutig. Vorhanden: {vorhanden}."
            )
        zugeordnet[treffer[0]["gid"]] = wert
    return zugeordnet


@registriere(
    "projekt_aus_vorlage",
    KATEGORIE_ANLEGEN,
    pflicht={"vorlage_gid", "name"},
    optional={"team_gid", "termine", "rollen"},
    erzeugt="projekt",
)
class ProjektAusVorlage:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, dict, list[str]]:
        """Liefert die Vorlage, den Request-Body und lesbare Angaben für die Vorschau."""
        vorlage = await lauf.hole(
            "projektvorlage",
            op["vorlage_gid"],
            "/project_templates",
            (
                "name",
                "team.name",
                "requested_dates.name",
                "requested_dates.description",
                "requested_roles.name",
            ),
        )
        variablen = vorlage.get("requested_dates") or []
        termine = _zuordnung(op.get("termine", {}), variablen, "die Datumsvariable")
        if fehlend := [v for v in variablen if v["gid"] not in termine]:
            namen = ", ".join(f"{v.get('name', '')} ({v['gid']})" for v in fehlend)
            raise ToolFehler(
                f"Die Vorlage {q(vorlage.get('name'))} braucht noch ein Datum für: {namen}. "
                "Frage den Nutzer danach und gib es in „termine“ an."
            )
        teile = []
        daten: dict = {"name": op["name"]}
        # Der Body hängt davon ab, ob der Workspace eine Organisation ist: Nur dort gibt es
        # Teams. Ohne Angabe nimmt Asana das Team der Vorlage.
        if "team_gid" in op:
            if not await _ist_organisation(lauf):
                raise ToolFehler(
                    "Dieser Workspace ist keine Organisation, deshalb gibt es keine Teams. "
                    "Bitte „team_gid“ weglassen."
                )
            daten["team"] = op["team_gid"]
            teile.append(f"Team {q((await lauf.team(op['team_gid'])).get('name'))}")
        if variablen:
            daten["requested_dates"] = []
            for variable in variablen:
                wert = termine[variable["gid"]]
                if not isinstance(wert, str):
                    raise ToolFehler("Termine der Vorlage müssen ein Datum JJJJ-MM-TT sein.")
                teile.append(f"{variable.get('name', '')} {datum_text(_datum(wert))}")
                daten["requested_dates"].append({"gid": variable["gid"], "value": wert})
        rollen = _zuordnung(op.get("rollen", {}), vorlage.get("requested_roles") or [], "die Rolle")
        if rollen:
            daten["requested_roles"] = []
            namen = {r["gid"]: r.get("name", "") for r in vorlage.get("requested_roles") or []}
            for gid, wert in rollen.items():
                nutzer = await lauf.nutzer_aufloesen(wert)
                daten["requested_roles"].append({"gid": gid, "value": nutzer["gid"]})
                teile.append(f"{namen[gid]}: {nutzer.get('name', '')}")
        return vorlage, daten, teile

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        vorlage, _, teile = await ProjektAusVorlage._plan(op, lauf)
        merke_neu(op, lauf)
        return f"Anlegen: Projekt {q(op['name'])} aus Vorlage {q(vorlage.get('name'))}" + (
            f" ({', '.join(teile)})" if teile else ""
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        vorlage, daten, _ = await ProjektAusVorlage._plan(op, lauf)
        job = await lauf.asana.post(
            f"/project_templates/{op['vorlage_gid']}/instantiateProject", daten
        )
        neu = await warte_auf_job(lauf, job, "new_project")
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Projekt {q(op['name'])} aus Vorlage {q(vorlage.get('name'))} angelegt",
            link=neu.get("permalink_url", ""),
            ist_projekt=True,
            felder=tuple(sorted(set(op) - {"operation", "platzhalter"})),
        )


def _datum(wert: str) -> str:
    try:
        return date.fromisoformat(wert).isoformat()
    except ValueError:
        raise ToolFehler("Termine der Vorlage müssen ein Datum JJJJ-MM-TT sein.") from None


@registriere(
    "aufgabe_aus_vorlage",
    KATEGORIE_ANLEGEN,
    pflicht={"vorlage_gid"},
    optional={"name"},
    erzeugt="aufgabe",
)
class AufgabeAusVorlage:
    @staticmethod
    async def _vorlage(op: dict, lauf: Lauf) -> dict:
        return await lauf.hole(
            "aufgabenvorlage", op["vorlage_gid"], "/task_templates", ("name", "project.name")
        )

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        vorlage = await AufgabeAusVorlage._vorlage(op, lauf)
        name = op.get("name") or vorlage.get("name", "")
        merke_neu({**op, "name": name}, lauf, memberships=[])
        projekt = (vorlage.get("project") or {}).get("name")
        return f"Anlegen: Aufgabe {q(name)} aus Vorlage {q(vorlage.get('name'))}" + (
            f" im Projekt {q(projekt)}" if projekt else ""
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        vorlage = await AufgabeAusVorlage._vorlage(op, lauf)
        daten = {"name": op["name"]} if "name" in op else {}
        job = await lauf.asana.post(f"/task_templates/{op['vorlage_gid']}/instantiateTask", daten)
        neu = await warte_auf_job(lauf, job, "new_task")
        name = op.get("name") or neu.get("name") or vorlage.get("name", "")
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Aufgabe {q(name)} aus Vorlage {q(vorlage.get('name'))} angelegt",
            link=neu.get("permalink_url", ""),
            felder=("name",),
        )


def _einschliessen(op: dict, erlaubt: tuple[str, ...]) -> list[str]:
    werte = op.get("einschliessen", [])
    if unbekannt := [w for w in werte if w not in erlaubt]:
        raise ToolFehler(
            f"„einschliessen“ kennt hier {', '.join(unbekannt)} nicht. Erlaubt sind: "
            f"{', '.join(erlaubt)}."
        )
    return list(werte)


@registriere(
    "aufgabe_duplizieren",
    KATEGORIE_ANLEGEN,
    pflicht={"gid", "name"},
    optional={"einschliessen"},
    erzeugt="aufgabe",
    gid_typ="aufgabe",
)
class AufgabeDuplizieren:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["gid"])
        mit = _einschliessen(op, AUFGABE_EINSCHLIESSEN)
        merke_neu(op, lauf, memberships=[])
        return f"Duplizieren: Aufgabe {bez(aufgabe)} als {q(op['name'])}" + (
            f" (mit {', '.join(mit)})" if mit else " (nur der Name)"
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["gid"])
        daten = {"name": op["name"]}
        if mit := _einschliessen(op, AUFGABE_EINSCHLIESSEN):
            daten["include"] = ",".join(mit)
        job = await lauf.asana.post(f"/tasks/{op['gid']}/duplicate", daten)
        neu = await warte_auf_job(lauf, job, "new_task")
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Aufgabe {q(aufgabe.get('name'))} als {q(op['name'])} dupliziert",
            link=neu.get("permalink_url", ""),
            felder=tuple(sorted(set(op) - {"operation", "platzhalter", "gid"})),
        )


@registriere(
    "projekt_duplizieren",
    KATEGORIE_ANLEGEN,
    pflicht={"gid", "name"},
    optional={"einschliessen", "termine_ab", "termine_bis", "wochenenden_ueberspringen"},
    erzeugt="projekt",
    gid_typ="projekt",
)
class ProjektDuplizieren:
    @staticmethod
    def _daten(op: dict) -> tuple[dict, list[str]]:
        mit = _einschliessen(op, PROJEKT_EINSCHLIESSEN)
        daten: dict = {"name": op["name"]}
        teile = []
        if "termine_ab" in op and "termine_bis" in op:
            raise ToolFehler("Bitte nur „termine_ab“ oder „termine_bis“ angeben, nicht beide.")
        if "termine_ab" in op or "termine_bis" in op:
            # Die Verschiebung setzt voraus, dass die Termine der Aufgaben mitkopiert werden.
            if "task_dates" not in mit:
                mit.append("task_dates")
            feld, wort = (
                ("start_on", "erster Start")
                if "termine_ab" in op
                else ("due_on", "letzte Fälligkeit")
            )
            wert = op.get("termine_ab") or op["termine_bis"]
            daten["schedule_dates"] = {
                "should_skip_weekends": op.get("wochenenden_ueberspringen", True),
                feld: wert,
            }
            teile.append(f"Termine verschoben, {wort} {datum_text(wert)}")
        elif "wochenenden_ueberspringen" in op:
            raise ToolFehler(
                "„wochenenden_ueberspringen“ gilt nur zusammen mit termine_ab oder termine_bis."
            )
        if mit:
            daten["include"] = ",".join(mit)
            teile.insert(0, "mit " + ", ".join(mit))
        return daten, teile

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        projekt = await lauf.projekt(op["gid"])
        _, teile = ProjektDuplizieren._daten(op)
        merke_neu(op, lauf)
        return f"Duplizieren: Projekt {bez(projekt)} als {q(op['name'])} mit allen Aufgaben" + (
            f" ({'; '.join(teile)})" if teile else ""
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        projekt = await lauf.projekt(op["gid"])
        daten, _ = ProjektDuplizieren._daten(op)
        job = await lauf.asana.post(f"/projects/{op['gid']}/duplicate", daten)
        neu = await warte_auf_job(lauf, job, "new_project")
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Projekt {q(projekt.get('name'))} als {q(op['name'])} dupliziert",
            link=neu.get("permalink_url", ""),
            ist_projekt=True,
            felder=tuple(sorted(set(op) - {"operation", "platzhalter", "gid"})),
        )
