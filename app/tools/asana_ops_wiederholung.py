"""Wiederkehrende Aufgaben – experimentell.

Die Asana-API liest und setzt Wiederholungen über das Feld `recurrence`, beschreibt es aber
nicht in der Dokumentation. Deshalb wird hier keine Struktur gebaut: Eine Wiederholung wird
ausschließlich 1:1 von einer Aufgabe übernommen, an der sie in Asana von Hand eingerichtet
wurde.
"""

import json

from app.tools.asana_client import AsanaFehler
from app.tools.asana_operationen import (
    KATEGORIE_AENDERN,
    REF_FELDER,
    ZUSATZ_BESCHREIBUNG,
    ZUSATZ_SCHEMA,
    Lauf,
    OpErgebnis,
    bez,
    q,
    registriere,
)
from app.tools.base import ToolFehler

WIEDERHOLUNG_FELD = "recurrence"
MAX_STRUKTUR_ZEICHEN = 200
EXPERIMENTELL_TEXT = (
    "Wiederholungen über die Schnittstelle sind experimentell, weil Asana sie nicht "
    "dokumentiert. Bitte stelle die Wiederholung in Asana selbst ein: Aufgabe öffnen, auf das "
    "Datum klicken, dann „Wiederholen“."
)

REF_FELDER["vorlage_aufgabe_gid"] = "aufgabe"
ZUSATZ_SCHEMA["vorlage_aufgabe_gid"] = {
    "type": "string",
    "description": "GID einer Aufgabe, an der die gewünschte Wiederholung in Asana von Hand "
    "eingerichtet wurde",
}
ZUSATZ_BESCHREIBUNG.extend(
    [
        "- wiederholung_setzen (experimentell): gid, vorlage_aufgabe_gid. Übernimmt die "
        "Wiederholung unverändert von der Vorlage-Aufgabe; eine Wiederholung frei zu "
        "beschreiben geht nicht",
        "- wiederholung_entfernen (experimentell): gid, optional vorlage_aufgabe_gid einer "
        "Aufgabe ohne Wiederholung",
    ]
)


async def _mit_wiederholung(ref: str, lauf: Lauf) -> dict:
    """Liest die Aufgabe samt ihrer Wiederholungsregel, so wie Asana sie liefert."""
    if ref in lauf.neu:
        return lauf.neu[ref]
    try:
        return await lauf.hole(
            "wiederholung", ref, "/tasks", ("name", WIEDERHOLUNG_FELD, "permalink_url")
        )
    except AsanaFehler as exc:
        if exc.status == 400:
            raise ToolFehler(
                f"Asana liefert die Wiederholung nicht. {EXPERIMENTELL_TEXT}"
            ) from None
        raise


def _kurz(struktur: object) -> str:
    text = json.dumps(struktur, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= MAX_STRUKTUR_ZEICHEN else text[: MAX_STRUKTUR_ZEICHEN - 1] + "…"


async def _setze(op: dict, lauf: Lauf, struktur: object) -> None:
    try:
        await lauf.asana.put(f"/tasks/{op['gid']}", {WIEDERHOLUNG_FELD: struktur})
    except AsanaFehler as exc:
        raise AsanaFehler(f"{exc} {EXPERIMENTELL_TEXT}", status=exc.status) from None
    lauf.vergiss("wiederholung", op["gid"])
    lauf.vergiss("aufgabe", op["gid"])


@registriere(
    "wiederholung_setzen",
    KATEGORIE_AENDERN,
    pflicht={"gid", "vorlage_aufgabe_gid"},
    gid_typ="aufgabe",
)
class WiederholungSetzen:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, dict, object]:
        if op["gid"] == op["vorlage_aufgabe_gid"]:
            raise ToolFehler("Ziel und Vorlage sind dieselbe Aufgabe.")
        aufgabe = await _mit_wiederholung(op["gid"], lauf)
        vorlage = await _mit_wiederholung(op["vorlage_aufgabe_gid"], lauf)
        struktur = vorlage.get(WIEDERHOLUNG_FELD)
        if not struktur:
            raise ToolFehler(
                f"An der Aufgabe {bez(vorlage)} ist keine Wiederholung eingerichtet. Als Vorlage "
                "taugt nur eine Aufgabe, bei der die Wiederholung in Asana von Hand eingestellt "
                "wurde."
            )
        return aufgabe, vorlage, struktur

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe, vorlage, struktur = await WiederholungSetzen._plan(op, lauf)
        return (
            f"Wiederholung (experimentell): {bez(aufgabe)} bekommt die Wiederholung von "
            f"{bez(vorlage)}: {_kurz(struktur)}"
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe, vorlage, struktur = await WiederholungSetzen._plan(op, lauf)
        await _setze(op, lauf, struktur)
        return OpErgebnis(
            gid=op["gid"],
            text=f"Wiederholung von {q(vorlage.get('name'))} auf {q(aufgabe.get('name'))} "
            "übertragen",
            link=aufgabe.get("permalink_url", ""),
            felder=("wiederholung",),
            vorher={"wiederholung": aufgabe.get(WIEDERHOLUNG_FELD)},
        )


@registriere(
    "wiederholung_entfernen",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional={"vorlage_aufgabe_gid"},
    gid_typ="aufgabe",
)
class WiederholungEntfernen:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, object]:
        """Der Wert für „keine Wiederholung“: von einer Aufgabe ohne Wiederholung übernommen,
        sonst der leere Wert."""
        aufgabe = await _mit_wiederholung(op["gid"], lauf)
        if "vorlage_aufgabe_gid" not in op:
            return aufgabe, None
        vorlage = await _mit_wiederholung(op["vorlage_aufgabe_gid"], lauf)
        return aufgabe, vorlage.get(WIEDERHOLUNG_FELD)

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe, _ = await WiederholungEntfernen._plan(op, lauf)
        bisher = aufgabe.get(WIEDERHOLUNG_FELD)
        return f"Wiederholung entfernen (experimentell): {bez(aufgabe)}" + (
            f", bisher {_kurz(bisher)}" if bisher else ""
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe, struktur = await WiederholungEntfernen._plan(op, lauf)
        await _setze(op, lauf, struktur)
        return OpErgebnis(
            gid=op["gid"],
            text=f"Wiederholung bei {q(aufgabe.get('name'))} entfernt",
            link=aufgabe.get("permalink_url", ""),
            felder=("wiederholung",),
            vorher={"wiederholung": aufgabe.get(WIEDERHOLUNG_FELD)},
        )
