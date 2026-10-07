"""Operationen für benutzerdefinierte Felder: anlegen, ändern, löschen, Optionen, Projekte."""

from app.tools import asana_feldwerte
from app.tools.asana_operationen import (
    BOOL_FELDER,
    KATEGORIE_AENDERN,
    KATEGORIE_ANLEGEN,
    KATEGORIE_LOESCHEN,
    LISTEN_FELDER,
    LOESCH_MARKE,
    REF_FELDER,
    TYP_NAME,
    WAHL_FELDER,
    ZAHL_FELDER,
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

REF_FELDER["feld_gid"] = "feld"
TYP_NAME["feld"] = "ein Feld"
WAHL_FELDER["typ"] = asana_feldwerte.TYPEN
LISTEN_FELDER.add("optionen")
ZAHL_FELDER.add("nachkommastellen")
BOOL_FELDER.add("wichtig")
ZUSATZ_SCHEMA.update(
    {
        "feld_gid": {
            "type": "string",
            "description": "GID eines benutzerdefinierten Feldes oder Platzhalter",
        },
        "typ": {"type": "string", "enum": list(asana_feldwerte.TYPEN)},
        "optionen": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Namen der Auswahloptionen (nur enum und multi_enum)",
        },
        "nachkommastellen": {"type": "integer", "minimum": 0, "maximum": 6},
        "wichtig": {
            "type": "boolean",
            "description": "Feld in der Listenansicht des Projekts anzeigen",
        },
        "felder": {
            "type": "object",
            "description": "Benutzerdefinierte Felder einer Aufgabe: Feldname oder Feld-GID als "
            "Schlüssel. Wert je Typ: text Text, number Zahl, enum Name oder GID der Option, "
            "multi_enum Liste davon, date JJJJ-MM-TT, people Liste von Nutzern; null leert",
        },
    }
)
ZUSATZ_BESCHREIBUNG.extend(
    [
        "- feld_anlegen: name, typ (text, number, enum, multi_enum, date, people), optionen, "
        "nachkommastellen, beschreibung; Platzhalter $f1",
        "- feld_aendern: feld_gid, name, beschreibung, nachkommastellen",
        "- feld_loeschen: feld_gid (löscht das Feld im ganzen Workspace samt allen Werten)",
        "- feldoption_hinzufuegen: feld_gid, name",
        "- feld_zu_projekt_hinzufuegen: projekt, feld_gid, wichtig",
        "- feld_aus_projekt_entfernen: projekt, feld_gid",
        "- aufgabe_anlegen und aufgabe_aendern kennen zusätzlich „felder“ für die Werte "
        "benutzerdefinierter Felder; Namen werden aufgelöst",
    ]
)

_FELDER = ("name", "resource_subtype", "precision", "description", "enum_options.name")


async def _feld(op: dict, lauf: Lauf) -> dict:
    return await lauf.hole("feld", op["feld_gid"], "/custom_fields", _FELDER)


def _pruefe_stellen(op: dict) -> None:
    stellen = op.get("nachkommastellen")
    if stellen is not None and (stellen != int(stellen) or not 0 <= stellen <= 6):
        raise ToolFehler("„nachkommastellen“ muss eine ganze Zahl von 0 bis 6 sein.")


@registriere(
    "feld_anlegen",
    KATEGORIE_ANLEGEN,
    pflicht={"name", "typ"},
    optional={"optionen", "nachkommastellen", "beschreibung"},
    erzeugt="feld",
)
class FeldAnlegen:
    @staticmethod
    def _pruefe(op: dict) -> None:
        auswahl = op["typ"] in ("enum", "multi_enum")
        if auswahl and "optionen" not in op:
            raise ToolFehler("Ein Auswahlfeld braucht mindestens eine Option in „optionen“.")
        if not auswahl and "optionen" in op:
            raise ToolFehler("„optionen“ gibt es nur bei den Typen enum und multi_enum.")
        if "nachkommastellen" in op and op["typ"] != "number":
            raise ToolFehler("„nachkommastellen“ gibt es nur beim Typ number.")
        _pruefe_stellen(op)

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        FeldAnlegen._pruefe(op)
        text = f"Anlegen: Feld {q(op['name'])} (Typ {op['typ']}"
        if "optionen" in op:
            text += ", Optionen: " + ", ".join(op["optionen"])
        if "nachkommastellen" in op:
            text += f", {int(op['nachkommastellen'])} Nachkommastellen"
        merke_neu(
            op,
            lauf,
            resource_subtype=op["typ"],
            enum_options=[{"gid": "", "name": name} for name in op.get("optionen", [])],
        )
        return text + ")"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        FeldAnlegen._pruefe(op)
        daten: dict = {
            "workspace": await lauf.asana.workspace_gid(),
            "name": op["name"],
            "resource_subtype": op["typ"],
        }
        if "optionen" in op:
            daten["enum_options"] = [{"name": name} for name in op["optionen"]]
        if "nachkommastellen" in op:
            daten["precision"] = int(op["nachkommastellen"])
        if "beschreibung" in op:
            daten["description"] = op["beschreibung"]
        neu = await lauf.asana.post("/custom_fields", daten)
        lauf.zwischenspeicher.pop("workspace_felder", None)
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Feld {q(op['name'])} angelegt",
            felder=tuple(sorted(set(op) - {"operation", "platzhalter"})),
        )


@registriere(
    "feld_aendern",
    KATEGORIE_AENDERN,
    pflicht={"feld_gid"},
    optional={"name", "beschreibung", "nachkommastellen"},
)
class FeldAendern:
    @staticmethod
    def _daten(op: dict) -> dict:
        _pruefe_stellen(op)
        daten: dict = {}
        if "name" in op:
            daten["name"] = op["name"]
        if "beschreibung" in op:
            daten["description"] = op["beschreibung"]
        if "nachkommastellen" in op:
            daten["precision"] = int(op["nachkommastellen"])
        if not daten:
            raise ToolFehler("feld_aendern braucht mindestens ein zu änderndes Feld.")
        return daten

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        feld = await _feld(op, lauf)
        teile = []
        for schluessel, neu in FeldAendern._daten(op).items():
            if schluessel == "name":
                teile.append(f"Name → {q(neu)}")
            elif schluessel == "description":
                teile.append("Beschreibung geändert")
            else:
                teile.append(f"Nachkommastellen {feld.get('precision')} → {neu}")
        return f"Ändern: Feld {bez(feld)}: " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = FeldAendern._daten(op)
        feld = await _feld(op, lauf)
        await lauf.asana.put(f"/custom_fields/{op['feld_gid']}", daten)
        lauf.vergiss("feld", op["feld_gid"])
        return OpErgebnis(
            gid=op["feld_gid"],
            text=f"Feld {q(feld.get('name'))} geändert",
            felder=tuple(sorted(set(op) - {"operation", "feld_gid"})),
            vorher={
                "name": feld.get("name", ""),
                "nachkommastellen": feld.get("precision"),
            },
        )


@registriere("feld_loeschen", KATEGORIE_LOESCHEN, pflicht={"feld_gid"})
class FeldLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        feld = await _feld(op, lauf)
        return f"{LOESCH_MARKE} Feld {bez(feld)} im ganzen Workspace samt allen Werten"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        feld = await _feld(op, lauf)
        await lauf.asana.delete(f"/custom_fields/{op['feld_gid']}")
        lauf.vergiss("feld", op["feld_gid"])
        return OpErgebnis(
            gid=op["feld_gid"],
            text=f"Feld {q(feld.get('name'))} gelöscht",
            vorher={"name": feld.get("name", ""), "typ": feld.get("resource_subtype")},
        )


@registriere("feldoption_hinzufuegen", KATEGORIE_AENDERN, pflicht={"feld_gid", "name"})
class FeldoptionHinzufuegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        feld = await _feld(op, lauf)
        if not feld.get("neu") and feld.get("resource_subtype") not in ("enum", "multi_enum"):
            raise ToolFehler(f"Das Feld {bez(feld)} ist kein Auswahlfeld und hat keine Optionen.")
        return f"Option {q(op['name'])} zum Feld {bez(feld)} hinzufügen"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        feld = await _feld(op, lauf)
        neu = await lauf.asana.post(
            f"/custom_fields/{op['feld_gid']}/enum_options", {"name": op["name"]}
        )
        lauf.vergiss("feld", op["feld_gid"])
        return OpErgebnis(
            gid=neu.get("gid", op["feld_gid"]),
            text=f"Option {q(op['name'])} zum Feld {q(feld.get('name'))} hinzugefügt",
            felder=("optionen",),
        )


@registriere(
    "feld_zu_projekt_hinzufuegen",
    KATEGORIE_AENDERN,
    pflicht={"projekt", "feld_gid"},
    optional={"wichtig"},
)
class FeldZuProjektHinzufuegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        feld = await _feld(op, lauf)
        projekt = await lauf.projekt(op["projekt"])
        return f"Feld {bez(feld)} im Projekt {bez(projekt)} einrichten"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        feld = await _feld(op, lauf)
        projekt = await lauf.projekt(op["projekt"])
        daten = {"custom_field": op["feld_gid"]}
        if "wichtig" in op:
            daten["is_important"] = op["wichtig"]
        await lauf.asana.post(f"/projects/{op['projekt']}/addCustomFieldSetting", daten)
        lauf.zwischenspeicher.pop(("projekt_felder", op["projekt"]), None)
        return OpErgebnis(
            gid=op["projekt"],
            text=f"Feld {q(feld.get('name'))} im Projekt {q(projekt.get('name'))} eingerichtet",
            link=projekt.get("permalink_url", ""),
            ist_projekt=True,
            felder=("felder",),
        )


@registriere("feld_aus_projekt_entfernen", KATEGORIE_AENDERN, pflicht={"projekt", "feld_gid"})
class FeldAusProjektEntfernen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        feld = await _feld(op, lauf)
        projekt = await lauf.projekt(op["projekt"])
        return (
            f"Feld {bez(feld)} aus dem Projekt {bez(projekt)} entfernen (die Werte an den "
            "Aufgaben dieses Projekts verschwinden, das Feld selbst bleibt)"
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        feld = await _feld(op, lauf)
        projekt = await lauf.projekt(op["projekt"])
        await lauf.asana.post(
            f"/projects/{op['projekt']}/removeCustomFieldSetting",
            {"custom_field": op["feld_gid"]},
        )
        lauf.zwischenspeicher.pop(("projekt_felder", op["projekt"]), None)
        return OpErgebnis(
            gid=op["projekt"],
            text=f"Feld {q(feld.get('name'))} aus dem Projekt {q(projekt.get('name'))} entfernt",
            link=projekt.get("permalink_url", ""),
            ist_projekt=True,
            felder=("felder",),
        )
