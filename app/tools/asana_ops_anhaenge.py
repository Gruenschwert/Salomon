"""Operationen für Anhänge: Datei aus dem Chat oder externer Link anhängen, Anhang löschen."""

import re

from app.db.models import TelegramDatei
from app.medien import groesse_text
from app.tools.asana_operationen import (
    KATEGORIE_ANLEGEN,
    KATEGORIE_LOESCHEN,
    LOESCH_MARKE,
    REF_FELDER,
    TYP_NAME,
    ZUSATZ_BESCHREIBUNG,
    ZUSATZ_SCHEMA,
    Lauf,
    OpErgebnis,
    bez,
    q,
    registriere,
)
from app.tools.base import ToolFehler, aktueller_nutzer

# Telegram gibt Bots nur Dateien bis 20 MB heraus, Asana nimmt Anhänge bis 100 MB.
TELEGRAM_MAX_MB = 20
ASANA_MAX_MB = 100
DATEI_MUSTER = re.compile(r"^datei:(\d{1,12})$")

REF_FELDER["anhang_gid"] = "anhang"
TYP_NAME["anhang"] = "einen Anhang"
ZUSATZ_SCHEMA.update(
    {
        "datei": {
            "type": "string",
            "description": "Verweis auf eine Datei aus dem Chat, z. B. datei:12 (steht in der "
            "Nachricht des Nutzers)",
        },
        "url": {"type": "string", "description": "Adresse eines externen Links (https://…)"},
        "anhang_gid": {"type": "string", "description": "GID eines Anhangs"},
    }
)
ZUSATZ_BESCHREIBUNG.extend(
    [
        "- anhang_hinzufuegen: aufgabe_gid ODER projekt, dazu entweder datei (Verweis datei:N "
        "auf ein Foto oder Dokument aus dem Chat) oder url + name (externer Link); name benennt "
        "eine Datei optional um",
        "- anhang_loeschen: anhang_gid (aus asana_anhaenge_anzeigen; zählt als Löschung)",
    ]
)


async def _ziel(op: dict, lauf: Lauf) -> tuple[str, dict]:
    """Die Aufgabe oder das Projekt, an dem der Anhang hängt, als (Wort, Objekt)."""
    if ("aufgabe_gid" in op) == ("projekt" in op):
        raise ToolFehler("Bitte genau eines angeben: „aufgabe_gid“ oder „projekt“.")
    if "aufgabe_gid" in op:
        return "Aufgabe", await lauf.aufgabe(op["aufgabe_gid"])
    return "Projekt", await lauf.projekt(op["projekt"])


async def _datei(op: dict, lauf: Lauf) -> TelegramDatei:
    """Liest den Verweis und prüft, dass die Datei vom anfragenden Nutzer stammt."""
    treffer = DATEI_MUSTER.match(op["datei"])
    datei = None
    if treffer:
        async with lauf.kontext.session_fabrik() as session:
            datei = await session.get(TelegramDatei, int(treffer.group(1)))
    if datei is None or datei.user_id != aktueller_nutzer.get().id:
        raise ToolFehler(
            f"Die Datei „{op['datei']}“ gibt es nicht. Verwende genau den Verweis aus der "
            "Nachricht des Nutzers (datei:N) oder bitte ihn, die Datei noch einmal zu schicken."
        )
    if (datei.groesse or 0) > TELEGRAM_MAX_MB * 1024 * 1024:
        raise ToolFehler(
            f"Die Datei ist größer als {TELEGRAM_MAX_MB} MB. So große Dateien gibt Telegram "
            "nicht an Bots heraus; bitte direkt in Asana hochladen."
        )
    return datei


def _pruefe_art(op: dict) -> None:
    if ("datei" in op) == ("url" in op):
        raise ToolFehler("Bitte genau eines angeben: „datei“ oder „url“.")
    if "url" in op:
        if not op["url"].startswith(("https://", "http://")):
            raise ToolFehler("„url“ muss mit https:// oder http:// beginnen.")
        if "name" not in op:
            raise ToolFehler("Ein externer Link braucht einen „name“.")


@registriere(
    "anhang_hinzufuegen",
    KATEGORIE_ANLEGEN,
    pflicht=set(),
    optional={"aufgabe_gid", "projekt", "datei", "url", "name"},
)
class AnhangHinzufuegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        _pruefe_art(op)
        wort, ziel = await _ziel(op, lauf)
        if "url" in op:
            return f"Anhängen: Link {q(op['name'])} ({op['url']}) an {wort} {bez(ziel)}"
        datei = await _datei(op, lauf)
        name = op.get("name") or datei.name
        return f"Anhängen: Datei {q(name)} ({groesse_text(datei.groesse)}) an {wort} {bez(ziel)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        _pruefe_art(op)
        wort, ziel = await _ziel(op, lauf)
        if "url" in op:
            name = op["name"]
            neu = await lauf.asana.post_formular(
                "/attachments",
                {
                    "parent": ziel["gid"],
                    "resource_subtype": "external",
                    "url": op["url"],
                    "name": name,
                },
            )
        else:
            datei = await _datei(op, lauf)
            name = op.get("name") or datei.name
            inhalt = await lauf.kontext.dateien.lade(datei.file_id)
            if len(inhalt) > ASANA_MAX_MB * 1024 * 1024:
                raise ToolFehler(f"Asana nimmt nur Anhänge bis {ASANA_MAX_MB} MB an.")
            neu = await lauf.asana.post_formular(
                "/attachments",
                {"parent": ziel["gid"], "name": name},
                datei=(name, inhalt, datei.medientyp or "application/octet-stream"),
            )
        return OpErgebnis(
            gid=neu["gid"],
            text=f"{q(name)} an {wort} {q(ziel.get('name'))} angehängt",
            link=ziel.get("permalink_url", ""),
            felder=("anhang",),
        )


@registriere("anhang_loeschen", KATEGORIE_LOESCHEN, pflicht={"anhang_gid"})
class AnhangLoeschen:
    @staticmethod
    async def _anhang(op: dict, lauf: Lauf) -> dict:
        return await lauf.hole("anhang", op["anhang_gid"], "/attachments", ("name", "parent.name"))

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        anhang = await AnhangLoeschen._anhang(op, lauf)
        ort = (anhang.get("parent") or {}).get("name")
        return f"{LOESCH_MARKE} Anhang {bez(anhang)}" + (f" von {q(ort)}" if ort else "")

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        anhang = await AnhangLoeschen._anhang(op, lauf)
        await lauf.asana.delete(f"/attachments/{op['anhang_gid']}")
        lauf.vergiss("anhang", op["anhang_gid"])
        return OpErgebnis(
            gid=op["anhang_gid"],
            text=f"Anhang {q(anhang.get('name'))} gelöscht",
            vorher={"name": anhang.get("name", "")},
        )
