"""Weitere Operationen für Aufgaben: Mehrfachzuordnung, Unteraufgaben umhängen, Reihenfolge,
Kommentare bearbeiten und löschen, Genehmigungen, „Gefällt mir“."""

from app.tools.asana_client import kuerze_text
from app.tools.asana_operationen import (
    KATEGORIE_AENDERN,
    KATEGORIE_LOESCHEN,
    KATEGORIE_VERSCHIEBEN,
    LOESCH_MARKE,
    MAX_KOMMENTAR_VORSCHAU,
    REF_FELDER,
    TYP_NAME,
    WAHL_FELDER,
    ZUSATZ_BESCHREIBUNG,
    ZUSATZ_SCHEMA,
    Lauf,
    OpErgebnis,
    bez,
    q,
    registriere,
)
from app.tools.base import ToolFehler

GENEHMIGUNG_STATUS = {
    "offen": "pending",
    "genehmigt": "approved",
    "abgelehnt": "rejected",
    "aenderungen_noetig": "changes_requested",
}

REF_FELDER.update(
    {
        "vor_aufgabe_gid": "aufgabe",
        "nach_aufgabe_gid": "aufgabe",
        "nach_abschnitt_gid": "abschnitt",
        "kommentar_gid": "kommentar",
    }
)
TYP_NAME["kommentar"] = "einen Kommentar"
WAHL_FELDER["status"] = tuple(GENEHMIGUNG_STATUS)
ZUSATZ_SCHEMA.update(
    {
        "vor_aufgabe_gid": {"type": "string", "description": "Einfügen vor dieser Aufgabe"},
        "nach_aufgabe_gid": {"type": "string", "description": "Einfügen nach dieser Aufgabe"},
        "nach_abschnitt_gid": {"type": "string", "description": "Einfügen nach diesem Abschnitt"},
        "kommentar_gid": {
            "type": "string",
            "description": "GID eines Kommentars (aus asana_aufgabe_details)",
        },
        "status": {
            "type": "string",
            "enum": list(GENEHMIGUNG_STATUS),
            "description": "Stand einer Genehmigung",
        },
        "genehmigung": {
            "type": "boolean",
            "description": "aufgabe_anlegen: Aufgabe vom Typ Genehmigung anlegen",
        },
    }
)
ZUSATZ_BESCHREIBUNG.extend(
    [
        "- aufgabe_zu_projekt_hinzufuegen: gid, projekt, abschnitt, vor_aufgabe_gid ODER "
        "nach_aufgabe_gid (die Aufgabe bleibt in ihren bisherigen Projekten)",
        "- aufgabe_aus_projekt_entfernen: gid, projekt",
        "- unteraufgabe_umhaengen: gid, uebergeordnet, vor_aufgabe_gid ODER nach_aufgabe_gid",
        "- unteraufgabe_zu_aufgabe_machen: gid, optional projekt und abschnitt, damit sie "
        "danach in einem Projekt sichtbar ist",
        "- reihenfolge_aendern: abschnitt plus entweder aufgabe_gid mit vor_aufgabe_gid ODER "
        "nach_aufgabe_gid (Aufgabe im Abschnitt umsortieren) oder vor_abschnitt_gid ODER "
        "nach_abschnitt_gid (den Abschnitt im Projekt umsortieren)",
        "- kommentar_bearbeiten: kommentar_gid, text (nur eigene Kommentare)",
        "- kommentar_loeschen: kommentar_gid (nur eigene Kommentare, zählt als Löschung)",
        "- aufgabe_genehmigung: gid, status (offen, genehmigt, abgelehnt, aenderungen_noetig); "
        "macht die Aufgabe zur Genehmigung, falls sie noch keine ist. Eine neue Genehmigung "
        "legt aufgabe_anlegen mit genehmigung=true an",
        "- aufgabe_gefaellt_mir: gid, entfernen",
    ]
)


def _position(op: dict, vor: str, nach: str, vor_api: str, nach_api: str) -> dict:
    if vor in op and nach in op:
        raise ToolFehler(f"Bitte nur „{vor}“ oder „{nach}“ angeben, nicht beide.")
    if vor in op:
        return {vor_api: op[vor]}
    if nach in op:
        return {nach_api: op[nach]}
    return {}


async def _positions_text(op: dict, lauf: Lauf) -> str:
    if "vor_aufgabe_gid" in op:
        return f", vor {bez(await lauf.aufgabe(op['vor_aufgabe_gid']))}"
    if "nach_aufgabe_gid" in op:
        return f", nach {bez(await lauf.aufgabe(op['nach_aufgabe_gid']))}"
    return ""


def _projekte(aufgabe: dict) -> list[dict]:
    return [m["project"] for m in aufgabe.get("memberships") or [] if m.get("project")]


@registriere(
    "aufgabe_zu_projekt_hinzufuegen",
    KATEGORIE_AENDERN,
    pflicht={"gid", "projekt"},
    optional={"abschnitt", "vor_aufgabe_gid", "nach_aufgabe_gid"},
    gid_typ="aufgabe",
)
class AufgabeZuProjektHinzufuegen:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, dict, dict | None, dict]:
        aufgabe = await lauf.aufgabe(op["gid"])
        projekt = await lauf.projekt(op["projekt"])
        abschnitt = await lauf.abschnitt(op["abschnitt"]) if "abschnitt" in op else None
        if abschnitt and (abschnitt.get("project") or {}).get("gid") != projekt.get("gid"):
            raise ToolFehler(
                f"Der Abschnitt {bez(abschnitt)} gehört nicht zum Projekt {bez(projekt)}."
            )
        daten = {
            "project": op["projekt"],
            **({"section": op["abschnitt"]} if abschnitt else {}),
            **_position(op, "vor_aufgabe_gid", "nach_aufgabe_gid", "insert_before", "insert_after"),
        }
        return aufgabe, projekt, abschnitt, daten

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe, projekt, abschnitt, _ = await AufgabeZuProjektHinzufuegen._plan(op, lauf)
        ort = bez(projekt) + (f" / {bez(abschnitt)}" if abschnitt else "")
        return f"Zuordnen: {bez(aufgabe)} zusätzlich zum Projekt {ort}" + await _positions_text(
            op, lauf
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe, projekt, _, daten = await AufgabeZuProjektHinzufuegen._plan(op, lauf)
        await lauf.asana.post(f"/tasks/{op['gid']}/addProject", daten)
        lauf.vergiss("aufgabe", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"{q(aufgabe.get('name'))} zum Projekt {q(projekt.get('name'))} hinzugefügt",
            link=aufgabe.get("permalink_url", ""),
            felder=("projekte",),
            vorher={"projekte": [p.get("name", "") for p in _projekte(aufgabe)]},
        )


@registriere(
    "aufgabe_aus_projekt_entfernen",
    KATEGORIE_AENDERN,
    pflicht={"gid", "projekt"},
    gid_typ="aufgabe",
)
class AufgabeAusProjektEntfernen:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, dict, str]:
        aufgabe = await lauf.aufgabe(op["gid"])
        projekt = await lauf.projekt(op["projekt"])
        andere = [p for p in _projekte(aufgabe) if p.get("gid") != projekt.get("gid")]
        hinweis = ""
        if not aufgabe.get("neu") and len(andere) == len(_projekte(aufgabe)):
            hinweis = " (liegt gar nicht in diesem Projekt)"
        elif not aufgabe.get("neu") and not andere and not aufgabe.get("parent"):
            hinweis = " – ⚠️ danach liegt sie in keinem Projekt mehr"
        return aufgabe, projekt, hinweis

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe, projekt, hinweis = await AufgabeAusProjektEntfernen._plan(op, lauf)
        return f"Zuordnung lösen: {bez(aufgabe)} aus dem Projekt {bez(projekt)}{hinweis}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe, projekt, _ = await AufgabeAusProjektEntfernen._plan(op, lauf)
        await lauf.asana.post(f"/tasks/{op['gid']}/removeProject", {"project": op["projekt"]})
        lauf.vergiss("aufgabe", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"{q(aufgabe.get('name'))} aus dem Projekt {q(projekt.get('name'))} entfernt",
            link=aufgabe.get("permalink_url", ""),
            felder=("projekte",),
            vorher={"projekte": [p.get("name", "") for p in _projekte(aufgabe)]},
        )


@registriere(
    "unteraufgabe_umhaengen",
    KATEGORIE_VERSCHIEBEN,
    pflicht={"gid", "uebergeordnet"},
    optional={"vor_aufgabe_gid", "nach_aufgabe_gid"},
    gid_typ="aufgabe",
)
class UnteraufgabeUmhaengen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        if op["gid"] == op["uebergeordnet"]:
            raise ToolFehler("Eine Aufgabe kann nicht ihre eigene Unteraufgabe sein.")
        _position(op, "vor_aufgabe_gid", "nach_aufgabe_gid", "insert_before", "insert_after")
        aufgabe = await lauf.aufgabe(op["gid"])
        ziel = await lauf.aufgabe(op["uebergeordnet"])
        alt = (aufgabe.get("parent") or {}).get("name")
        von = f"von {q(alt)} " if alt else ""
        return f"Umhängen: {bez(aufgabe)} {von}unter {bez(ziel)}" + await _positions_text(op, lauf)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["gid"])
        ziel = await lauf.aufgabe(op["uebergeordnet"])
        daten = {
            "parent": op["uebergeordnet"],
            **_position(op, "vor_aufgabe_gid", "nach_aufgabe_gid", "insert_before", "insert_after"),
        }
        await lauf.asana.post(f"/tasks/{op['gid']}/setParent", daten)
        lauf.vergiss("aufgabe", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"{q(aufgabe.get('name'))} unter {q(ziel.get('name'))} gehängt",
            link=aufgabe.get("permalink_url", ""),
            felder=("uebergeordnet",),
            vorher={"uebergeordnet": (aufgabe.get("parent") or {}).get("name", "")},
        )


@registriere(
    "unteraufgabe_zu_aufgabe_machen",
    KATEGORIE_VERSCHIEBEN,
    pflicht={"gid"},
    optional={"projekt", "abschnitt"},
    gid_typ="aufgabe",
)
class UnteraufgabeZuAufgabeMachen:
    @staticmethod
    async def _ziel(op: dict, lauf: Lauf) -> tuple[dict | None, dict | None]:
        abschnitt = await lauf.abschnitt(op["abschnitt"]) if "abschnitt" in op else None
        if "projekt" in op:
            projekt = await lauf.projekt(op["projekt"])
        else:
            projekt = (abschnitt or {}).get("project")
        return projekt, abschnitt

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["gid"])
        if not aufgabe.get("neu") and not aufgabe.get("parent"):
            raise ToolFehler(f"{bez(aufgabe)} ist keine Unteraufgabe.")
        projekt, abschnitt = await UnteraufgabeZuAufgabeMachen._ziel(op, lauf)
        text = f"Herauslösen: {bez(aufgabe)} wird eine eigenständige Aufgabe"
        if alt := (aufgabe.get("parent") or {}).get("name"):
            text += f" (bisher unter {q(alt)})"
        if projekt:
            text += f", im Projekt {bez(projekt)}" + (f" / {bez(abschnitt)}" if abschnitt else "")
        elif not _projekte(aufgabe):
            text += " – ⚠️ ohne Projekt ist sie danach nur noch über die Suche zu finden"
        return text

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["gid"])
        projekt, abschnitt = await UnteraufgabeZuAufgabeMachen._ziel(op, lauf)
        await lauf.asana.post(f"/tasks/{op['gid']}/setParent", {"parent": None})
        lauf.vergiss("aufgabe", op["gid"])
        if projekt:
            ziel = {
                "project": projekt["gid"],
                **({"section": abschnitt["gid"]} if abschnitt else {}),
            }
            try:
                await lauf.asana.post(f"/tasks/{op['gid']}/addProject", ziel)
            except ToolFehler as exc:
                raise ToolFehler(
                    f"{exc} (bereits herausgelöst, aber noch keinem Projekt zugeordnet)"
                ) from None
        return OpErgebnis(
            gid=op["gid"],
            text=f"{q(aufgabe.get('name'))} ist jetzt eine eigenständige Aufgabe",
            link=aufgabe.get("permalink_url", ""),
            felder=("uebergeordnet",),
            vorher={"uebergeordnet": (aufgabe.get("parent") or {}).get("name", "")},
        )


@registriere(
    "reihenfolge_aendern",
    KATEGORIE_VERSCHIEBEN,
    pflicht={"abschnitt"},
    optional={
        "aufgabe_gid",
        "vor_aufgabe_gid",
        "nach_aufgabe_gid",
        "vor_abschnitt_gid",
        "nach_abschnitt_gid",
    },
)
class ReihenfolgeAendern:
    @staticmethod
    def _daten(op: dict) -> tuple[str, dict]:
        """Liefert, was umsortiert wird („aufgabe“ oder „abschnitt“), und die Position."""
        aufgabe = _position(
            op, "vor_aufgabe_gid", "nach_aufgabe_gid", "insert_before", "insert_after"
        )
        abschnitt = _position(
            op, "vor_abschnitt_gid", "nach_abschnitt_gid", "before_section", "after_section"
        )
        if "aufgabe_gid" in op:
            if abschnitt or not aufgabe:
                raise ToolFehler(
                    "Zum Umsortieren einer Aufgabe „vor_aufgabe_gid“ oder „nach_aufgabe_gid“ "
                    "angeben."
                )
            return "aufgabe", aufgabe
        if aufgabe or not abschnitt:
            raise ToolFehler(
                "Zum Umsortieren eines Abschnitts „vor_abschnitt_gid“ oder „nach_abschnitt_gid“ "
                "angeben, zum Umsortieren einer Aufgabe zusätzlich „aufgabe_gid“."
            )
        return "abschnitt", abschnitt

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        was, _ = ReihenfolgeAendern._daten(op)
        abschnitt = await lauf.abschnitt(op["abschnitt"])
        if was == "aufgabe":
            aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
            position = (await _positions_text(op, lauf)).removeprefix(", ")
            return f"Umsortieren: {bez(aufgabe)} im Abschnitt {bez(abschnitt)} {position}"
        if "vor_abschnitt_gid" in op:
            position = f"vor {bez(await lauf.abschnitt(op['vor_abschnitt_gid']))}"
        else:
            position = f"nach {bez(await lauf.abschnitt(op['nach_abschnitt_gid']))}"
        return f"Umsortieren: Abschnitt {bez(abschnitt)} {position}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        was, position = ReihenfolgeAendern._daten(op)
        abschnitt = await lauf.abschnitt(op["abschnitt"])
        if was == "aufgabe":
            aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
            await lauf.asana.post(
                f"/sections/{op['abschnitt']}/addTask", {"task": op["aufgabe_gid"], **position}
            )
            lauf.vergiss("aufgabe", op["aufgabe_gid"])
            return OpErgebnis(
                gid=op["aufgabe_gid"],
                text=f"{q(aufgabe.get('name'))} im Abschnitt {q(abschnitt.get('name'))} umsortiert",
                link=aufgabe.get("permalink_url", ""),
                felder=("reihenfolge",),
            )
        projekt = (abschnitt.get("project") or {}).get("gid")
        if not projekt:
            raise ToolFehler("Zu diesem Abschnitt ist kein Projekt bekannt.")
        await lauf.asana.post(
            f"/projects/{projekt}/sections/insert", {"section": op["abschnitt"], **position}
        )
        return OpErgebnis(
            gid=op["abschnitt"],
            text=f"Abschnitt {q(abschnitt.get('name'))} umsortiert",
            felder=("reihenfolge",),
        )


# --------------------------------------------------------------------------------------
# Kommentare (nur eigene)
# --------------------------------------------------------------------------------------


async def _eigener_kommentar(op: dict, lauf: Lauf) -> dict:
    """Liest den Kommentar und stellt sicher, dass er vom Inhaber des Tokens stammt."""
    kommentar = await lauf.hole(
        "kommentar",
        op["kommentar_gid"],
        "/stories",
        ("type", "text", "created_by.name", "target.name"),
    )
    if kommentar.get("type") != "comment":
        raise ToolFehler("Das ist kein Kommentar, sondern ein Eintrag im Aktivitätsverlauf.")
    ich = await lauf.nutzer("me")
    if (kommentar.get("created_by") or {}).get("gid") != ich.get("gid"):
        autor = (kommentar.get("created_by") or {}).get("name") or "jemand anderem"
        raise ToolFehler(
            f"Der Kommentar stammt von {autor}. Bearbeiten und löschen lassen sich nur eigene "
            "Kommentare."
        )
    return kommentar


def _kommentar_text(kommentar: dict) -> str:
    ort = (kommentar.get("target") or {}).get("name")
    text = f"„{kuerze_text(kommentar.get('text'), MAX_KOMMENTAR_VORSCHAU)}“"
    return text + (f" an {q(ort)}" if ort else "")


@registriere("kommentar_bearbeiten", KATEGORIE_AENDERN, pflicht={"kommentar_gid", "text"})
class KommentarBearbeiten:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        kommentar = await _eigener_kommentar(op, lauf)
        return (
            f"Kommentar ändern: {_kommentar_text(kommentar)} → "
            f"„{kuerze_text(op['text'], MAX_KOMMENTAR_VORSCHAU)}“"
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        kommentar = await _eigener_kommentar(op, lauf)
        await lauf.asana.put(f"/stories/{op['kommentar_gid']}", {"text": op["text"]})
        lauf.vergiss("kommentar", op["kommentar_gid"])
        return OpErgebnis(
            gid=op["kommentar_gid"],
            text="Kommentar geändert",
            felder=("text",),
            vorher={"text": kuerze_text(kommentar.get("text"), 100)},
        )


@registriere("kommentar_loeschen", KATEGORIE_LOESCHEN, pflicht={"kommentar_gid"})
class KommentarLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        kommentar = await _eigener_kommentar(op, lauf)
        return f"{LOESCH_MARKE} Kommentar {_kommentar_text(kommentar)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        kommentar = await _eigener_kommentar(op, lauf)
        await lauf.asana.delete(f"/stories/{op['kommentar_gid']}")
        lauf.vergiss("kommentar", op["kommentar_gid"])
        return OpErgebnis(
            gid=op["kommentar_gid"],
            text="Kommentar gelöscht",
            vorher={"text": kuerze_text(kommentar.get("text"), 100)},
        )


# --------------------------------------------------------------------------------------
# Genehmigungen und „Gefällt mir“
# --------------------------------------------------------------------------------------


@registriere(
    "aufgabe_genehmigung",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional={"status"},
    gid_typ="aufgabe",
)
class AufgabeGenehmigung:
    @staticmethod
    def _daten(op: dict, aufgabe: dict) -> dict:
        daten: dict = {}
        if aufgabe.get("neu") or aufgabe.get("resource_subtype") != "approval":
            daten["resource_subtype"] = "approval"
        if "status" in op:
            daten["approval_status"] = GENEHMIGUNG_STATUS[op["status"]]
        return daten

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["gid"])
        daten = AufgabeGenehmigung._daten(op, aufgabe)
        if not daten:
            raise ToolFehler(
                f"{bez(aufgabe)} ist bereits eine Genehmigung; bitte „status“ angeben."
            )
        teile = []
        if "resource_subtype" in daten:
            teile.append("wird zur Genehmigung")
        if "status" in op:
            teile.append(f"Stand → {op['status']}")
        return f"Genehmigung: {bez(aufgabe)}: " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["gid"])
        daten = AufgabeGenehmigung._daten(op, aufgabe)
        if daten:
            await lauf.asana.put(f"/tasks/{op['gid']}", daten)
        lauf.vergiss("aufgabe", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"Genehmigung {q(aufgabe.get('name'))}"
            + (f" auf {op['status']} gesetzt" if "status" in op else " eingerichtet"),
            link=aufgabe.get("permalink_url", ""),
            felder=tuple(sorted(set(op) - {"operation", "gid"})) or ("aufgabentyp",),
            vorher={
                "aufgabentyp": aufgabe.get("resource_subtype"),
                "erledigt": bool(aufgabe.get("completed")),
            },
        )


@registriere(
    "aufgabe_gefaellt_mir",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional={"entfernen"},
    gid_typ="aufgabe",
)
class AufgabeGefaelltMir:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["gid"])
        return f"„Gefällt mir“ {'entfernen' if op.get('entfernen') else 'setzen'}: {bez(aufgabe)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["gid"])
        await lauf.asana.put(f"/tasks/{op['gid']}", {"liked": not op.get("entfernen", False)})
        return OpErgebnis(
            gid=op["gid"],
            text=f"„Gefällt mir“ bei {q(aufgabe.get('name'))} "
            + ("entfernt" if op.get("entfernen") else "gesetzt"),
            link=aufgabe.get("permalink_url", ""),
            felder=("gefaellt_mir",),
        )
