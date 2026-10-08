"""Operationen für Mitglieder und Follower von Projekten, Follower von Aufgaben und Teams."""

from app.auth.rechte import ASANA_TEAMS
from app.tools.asana_operationen import (
    KATEGORIE_AENDERN,
    KATEGORIE_ANLEGEN,
    KATEGORIE_LOESCHEN,
    LISTEN_FELDER,
    LOESCH_MARKE,
    WAHL_FELDER,
    ZUSATZ_BESCHREIBUNG,
    ZUSATZ_SCHEMA,
    Lauf,
    OpErgebnis,
    bez,
    merke_neu,
    q,
    registriere,
)
from app.tools.base import ToolFehler

# Sichtbarkeit eines Teams laut Asana-Doku (Feld visibility)
TEAM_SICHTBARKEIT = {"geheim": "secret", "auf_anfrage": "request_to_join", "oeffentlich": "public"}

LISTEN_FELDER.add("nutzer")
WAHL_FELDER["team_sichtbarkeit"] = tuple(TEAM_SICHTBARKEIT)
ZUSATZ_SCHEMA.update(
    {
        "nutzer": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Nutzer als Name, E-Mail, GID oder „me“; Namen werden aufgelöst",
        },
        "team_sichtbarkeit": {"type": "string", "enum": list(TEAM_SICHTBARKEIT)},
        "sichtbarkeit": {
            "type": "string",
            "enum": ["privat", "team", "oeffentlich"],
            "description": "Projekt: privat, für das Team sichtbar oder für alle im Workspace",
        },
        "standardansicht": {"type": "string", "enum": ["list", "board", "calendar", "timeline"]},
        "notizen": {"type": "string", "description": "Beschreibungstext des Projekts"},
    }
)
ZUSATZ_BESCHREIBUNG.extend(
    [
        "- projekt_aendern kennt zusätzlich sichtbarkeit (privat, team, oeffentlich), "
        "standardansicht (list, board, calendar, timeline) und notizen",
        "- projekt_mitglied_hinzufuegen, projekt_mitglied_entfernen: projekt, nutzer",
        "- projekt_follower_hinzufuegen, projekt_follower_entfernen: projekt, nutzer",
        "- follower_hinzufuegen, follower_entfernen: aufgabe_gid, nutzer",
        "- team_anlegen: name, beschreibung, team_sichtbarkeit (geheim, auf_anfrage, "
        "oeffentlich); Platzhalter $m1. Nur für Admins",
        "- team_aendern: team_gid, name, beschreibung, team_sichtbarkeit. Nur für Admins",
        "- team_mitglied_hinzufuegen: team_gid, nutzer. Nur für Admins",
        "- team_mitglied_entfernen: team_gid, nutzer (zählt als Löschung). Nur für Admins",
    ]
)


async def _nutzer(op: dict, lauf: Lauf) -> list[dict]:
    """Löst die Angaben in „nutzer“ auf; mehrdeutige Namen sind ein Fehler."""
    return [await lauf.nutzer_aufloesen(wert) for wert in op["nutzer"]]


def _namen(nutzer: list[dict]) -> str:
    return ", ".join(n.get("name", "") or n["gid"] for n in nutzer)


def _projekt_operation(art: str, pfad: str, feld: str, vorschau_text: str, ergebnis_text: str):
    """Die vier Projekt-Operationen unterscheiden sich nur in Endpunkt, Feld und Wortlaut."""

    class ProjektNutzer:
        @staticmethod
        async def vorschau(op: dict, lauf: Lauf) -> str:
            projekt = await lauf.projekt(op["projekt"])
            return vorschau_text.format(namen=_namen(await _nutzer(op, lauf)), projekt=bez(projekt))

        @staticmethod
        async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
            projekt = await lauf.projekt(op["projekt"])
            nutzer = await _nutzer(op, lauf)
            # Laut Doku ein Text mit den Nutzern, durch Komma getrennt.
            await lauf.asana.post(
                f"/projects/{op['projekt']}/{pfad}", {feld: ",".join(n["gid"] for n in nutzer)}
            )
            return OpErgebnis(
                gid=op["projekt"],
                text=ergebnis_text.format(namen=_namen(nutzer), projekt=q(projekt.get("name"))),
                link=projekt.get("permalink_url", ""),
                ist_projekt=True,
                felder=(feld,),
            )

    registriere(art, KATEGORIE_AENDERN, pflicht={"projekt", "nutzer"})(ProjektNutzer)


_projekt_operation(
    "projekt_mitglied_hinzufuegen",
    "addMembers",
    "members",
    "Mitglied hinzufügen: {namen} zum Projekt {projekt}",
    "{namen} als Mitglied zum Projekt {projekt} hinzugefügt",
)
_projekt_operation(
    "projekt_mitglied_entfernen",
    "removeMembers",
    "members",
    "Mitglied entfernen: {namen} aus dem Projekt {projekt}",
    "{namen} als Mitglied aus dem Projekt {projekt} entfernt",
)
_projekt_operation(
    "projekt_follower_hinzufuegen",
    "addFollowers",
    "followers",
    "Follower hinzufügen: {namen} zum Projekt {projekt}",
    "{namen} als Follower zum Projekt {projekt} hinzugefügt",
)
_projekt_operation(
    "projekt_follower_entfernen",
    "removeFollowers",
    "followers",
    "Follower entfernen: {namen} aus dem Projekt {projekt}",
    "{namen} als Follower aus dem Projekt {projekt} entfernt",
)


def _aufgaben_follower(art: str, pfad: str, verb: str, partizip: str) -> None:
    class AufgabenFollower:
        @staticmethod
        async def vorschau(op: dict, lauf: Lauf) -> str:
            aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
            return f"Follower {verb}: {_namen(await _nutzer(op, lauf))} bei {bez(aufgabe)}"

        @staticmethod
        async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
            aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
            nutzer = await _nutzer(op, lauf)
            await lauf.asana.post(
                f"/tasks/{op['aufgabe_gid']}/{pfad}", {"followers": [n["gid"] for n in nutzer]}
            )
            return OpErgebnis(
                gid=op["aufgabe_gid"],
                text=f"Follower {_namen(nutzer)} bei {q(aufgabe.get('name'))} {partizip}",
                link=aufgabe.get("permalink_url", ""),
                felder=("follower",),
            )

    registriere(art, KATEGORIE_AENDERN, pflicht={"aufgabe_gid", "nutzer"})(AufgabenFollower)


_aufgaben_follower("follower_hinzufuegen", "addFollowers", "hinzufügen", "hinzugefügt")
_aufgaben_follower("follower_entfernen", "removeFollowers", "entfernen", "entfernt")


# --------------------------------------------------------------------------------------
# Teams (nur mit dem Recht asana.teams)
# --------------------------------------------------------------------------------------


def _team_daten(op: dict) -> dict:
    daten: dict = {}
    if "name" in op:
        daten["name"] = op["name"]
    if "beschreibung" in op:
        daten["description"] = op["beschreibung"]
    if "team_sichtbarkeit" in op:
        daten["visibility"] = TEAM_SICHTBARKEIT[op["team_sichtbarkeit"]]
    return daten


@registriere(
    "team_anlegen",
    KATEGORIE_ANLEGEN,
    pflicht={"name"},
    optional={"beschreibung", "team_sichtbarkeit"},
    erzeugt="team",
    recht=ASANA_TEAMS,
)
class TeamAnlegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        merke_neu(op, lauf)
        sichtbar = f" ({op['team_sichtbarkeit']})" if "team_sichtbarkeit" in op else ""
        return f"Anlegen: Team {q(op['name'])}{sichtbar}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = {**_team_daten(op), "organization": await lauf.asana.workspace_gid()}
        neu = await lauf.asana.post("/teams", daten)
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Team {q(op['name'])} angelegt",
            felder=tuple(sorted(set(op) - {"operation", "platzhalter"})),
        )


@registriere(
    "team_aendern",
    KATEGORIE_AENDERN,
    pflicht={"team_gid"},
    optional={"name", "beschreibung", "team_sichtbarkeit"},
    recht=ASANA_TEAMS,
)
class TeamAendern:
    @staticmethod
    async def _team(op: dict, lauf: Lauf) -> dict:
        return await lauf.hole(
            "team_voll", op["team_gid"], "/teams", ("name", "description", "visibility")
        )

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        daten = _team_daten(op)
        if not daten:
            raise ToolFehler("team_aendern braucht mindestens ein zu änderndes Feld.")
        team = await TeamAendern._team(op, lauf)
        teile = []
        if "name" in daten:
            teile.append(f"Name → {q(daten['name'])}")
        if "description" in daten:
            teile.append("Beschreibung geändert")
        if "visibility" in daten:
            teile.append(
                f"Sichtbarkeit {team.get('visibility') or '(leer)'} → {daten['visibility']}"
            )
        return f"Ändern: Team {bez(team)}: " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = _team_daten(op)
        if not daten:
            raise ToolFehler("team_aendern braucht mindestens ein zu änderndes Feld.")
        team = await TeamAendern._team(op, lauf)
        await lauf.asana.put(f"/teams/{op['team_gid']}", daten)
        lauf.vergiss("team_voll", op["team_gid"])
        lauf.vergiss("team", op["team_gid"])
        return OpErgebnis(
            gid=op["team_gid"],
            text=f"Team {q(team.get('name'))} geändert",
            felder=tuple(sorted(set(op) - {"operation", "team_gid"})),
            vorher={"name": team.get("name", ""), "sichtbarkeit": team.get("visibility")},
        )


@registriere(
    "team_mitglied_hinzufuegen",
    KATEGORIE_AENDERN,
    pflicht={"team_gid", "nutzer"},
    recht=ASANA_TEAMS,
)
class TeamMitgliedHinzufuegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        team = await lauf.team(op["team_gid"])
        return f"Mitglied hinzufügen: {_namen(await _nutzer(op, lauf))} zum Team {bez(team)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        team = await lauf.team(op["team_gid"])
        nutzer = await _nutzer(op, lauf)
        # Der Endpunkt nimmt je Aufruf genau einen Nutzer.
        for eintrag in nutzer:
            await lauf.asana.post(f"/teams/{op['team_gid']}/addUser", {"user": eintrag["gid"]})
        return OpErgebnis(
            gid=op["team_gid"],
            text=f"{_namen(nutzer)} zum Team {q(team.get('name'))} hinzugefügt",
            felder=("mitglieder",),
        )


@registriere(
    "team_mitglied_entfernen",
    KATEGORIE_LOESCHEN,
    pflicht={"team_gid", "nutzer"},
    recht=ASANA_TEAMS,
)
class TeamMitgliedEntfernen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        team = await lauf.team(op["team_gid"])
        return (
            f"{LOESCH_MARKE} Mitgliedschaft von {_namen(await _nutzer(op, lauf))} "
            f"im Team {bez(team)}"
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        team = await lauf.team(op["team_gid"])
        nutzer = await _nutzer(op, lauf)
        for eintrag in nutzer:
            await lauf.asana.post(f"/teams/{op['team_gid']}/removeUser", {"user": eintrag["gid"]})
        return OpErgebnis(
            gid=op["team_gid"],
            text=f"{_namen(nutzer)} aus dem Team {q(team.get('name'))} entfernt",
            felder=("mitglieder",),
            vorher={"mitglieder": [n.get("name", "") for n in nutzer]},
        )
