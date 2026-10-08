"""Modellwahl nach festen Regeln, ohne zusätzliche KI-Aufrufe.

Einfache Fragen laufen auf dem günstigen Modell, schwere Aufgaben auf dem starken, alles
andere auf dem Standardmodell. Die Stichwortlisten sind Konstanten und damit testbar und
leicht änderbar.
"""

from dataclasses import dataclass

from app.auth.kontext import NutzerKontext
from app.channels.base import EingehendeNachricht

EINFACH = "einfach"
STANDARD = "standard"
KOMPLEX = "komplex"
AUTO = "auto"
STUFEN = (EINFACH, STANDARD, KOMPLEX)

LANG_AB_ZEICHEN = 1500
KURZ_BIS_ZEICHEN = 200

# Stichwörter für schwere Aufgaben (Vergleich ohne Groß-/Kleinschreibung, als Wortteil)
KOMPLEX_STICHWOERTER = (
    "analyse",
    "analysier",
    "auswertung",
    "auswerten",
    "projektplan",
    "konzept",
    "kampagne texten",
    "kampagnentext",
    "buchhaltung",
    "vergleich",
    "strategie",
)
# Kennzeichen einfacher Lesefragen
EINFACH_STICHWOERTER = (
    "status",
    "wie viel",
    "wieviel",
    "wie viele",
    "zeig mir",
    "zeige mir",
    "liste",
    "was ist heute",
    "was steht an",
    "welche aufgaben",
)
# Wer etwas ändern will, braucht mehr als das einfache Modell.
AENDERN_STICHWOERTER = (
    "anlegen",
    "leg ",
    "lege ",
    "erstell",
    "änder",
    "aender",
    "verschieb",
    "lösch",
    "loesch",
    "hake",
    "erledig",
    "kommentier",
    "häng",
    "haeng",
    "setz",
    "benenn",
    "archivier",
    "dupliz",
    "kopier",
)


@dataclass(frozen=True)
class Modellwahl:
    stufe: str
    # Kurz und ohne Inhalte der Nachricht: landet in usage.grund_modellwahl und in /kosten
    grund: str


def _treffer(text: str, stichwoerter: tuple[str, ...]) -> str | None:
    return next((wort for wort in stichwoerter if wort in text), None)


def _hat_pdf(nachricht: EingehendeNachricht) -> bool:
    return any(
        datei.medientyp == "application/pdf" or datei.name.lower().endswith(".pdf")
        for datei in nachricht.dateien
    )


def waehle_modell(
    nachricht: EingehendeNachricht, kontext: NutzerKontext, vorgabe: str = AUTO
) -> Modellwahl:
    """Wählt die Modellstufe für eine Nachricht. `vorgabe` kommt aus /modell."""
    if vorgabe in STUFEN:
        return Modellwahl(vorgabe, f"{vorgabe}: von dir gewählt (/modell)")
    text = (nachricht.text or "").casefold()
    if nachricht.bilder or _hat_pdf(nachricht):
        return Modellwahl(KOMPLEX, "komplex: Foto oder PDF")
    if len(text) > LANG_AB_ZEICHEN:
        return Modellwahl(KOMPLEX, "komplex: lange Nachricht")
    if wort := _treffer(text, KOMPLEX_STICHWOERTER):
        return Modellwahl(KOMPLEX, f"komplex: Stichwort {wort}")
    if (
        text
        and len(text) < KURZ_BIS_ZEICHEN
        and not nachricht.dateien
        and (wort := _treffer(text, EINFACH_STICHWOERTER))
        and not _treffer(text, AENDERN_STICHWOERTER)
    ):
        return Modellwahl(EINFACH, f"einfach: kurze Lesefrage (Stichwort {wort.strip()})")
    return Modellwahl(STANDARD, "standard: keine besondere Regel")


class ModellVorgaben:
    """Was jemand mit /modell gewählt hat. Gilt für die laufende Sitzung, also bis zum
    nächsten Neustart des Bots; der Standard ist `auto`."""

    def __init__(self) -> None:
        self._vorgaben: dict[int, str] = {}

    def hole(self, nutzer_id: int) -> str:
        return self._vorgaben.get(nutzer_id, AUTO)

    def setze(self, nutzer_id: int, stufe: str) -> None:
        if stufe == AUTO:
            self._vorgaben.pop(nutzer_id, None)
        elif stufe in STUFEN:
            self._vorgaben[nutzer_id] = stufe
        else:
            raise ValueError(stufe)
