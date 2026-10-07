"""Benutzerdefinierte Felder an Aufgaben: Namen in GIDs auflösen und Werte ins Format der
Asana-API bringen. Enthält keine Operationen und kein Tool."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.tools.base import ToolFehler

ICH = "me"
FELD_FELDER = (
    "name",
    "resource_subtype",
    "display_value",
    "precision",
    "enum_options.name",
    "enum_options.enabled",
)
TYPEN = ("text", "number", "enum", "multi_enum", "date", "people")
MAX_GELADEN = 500


async def workspace_felder(lauf) -> list[dict]:
    if "workspace_felder" not in lauf.zwischenspeicher:
        felder, _ = await lauf.asana.liste(
            f"/workspaces/{await lauf.asana.workspace_gid()}/custom_fields",
            felder=FELD_FELDER,
            max_eintraege=MAX_GELADEN,
        )
        lauf.zwischenspeicher["workspace_felder"] = felder
    return lauf.zwischenspeicher["workspace_felder"]


async def projekt_felder(lauf, projekt_gid: str) -> list[dict]:
    schluessel = ("projekt_felder", projekt_gid)
    if schluessel not in lauf.zwischenspeicher:
        einstellungen, _ = await lauf.asana.liste(
            f"/projects/{projekt_gid}/custom_field_settings",
            felder=tuple(f"custom_field.{feld}" for feld in FELD_FELDER),
            max_eintraege=MAX_GELADEN,
        )
        lauf.zwischenspeicher[schluessel] = [
            e["custom_field"] for e in einstellungen if e.get("custom_field")
        ]
    return lauf.zwischenspeicher[schluessel]


def _gleich(a: str | None, b: str) -> bool:
    return (a or "").strip().casefold() == b.strip().casefold()


def finde_feld(schluessel: str, verfuegbar: list[dict]) -> dict | None:
    """Sucht ein Feld nach GID oder Namen. Mehrere Treffer sind ein Fehler."""
    treffer = [f for f in verfuegbar if f["gid"] == schluessel]
    if not treffer:
        treffer = [f for f in verfuegbar if _gleich(f.get("name"), schluessel)]
    if len(treffer) > 1:
        auswahl = ", ".join(f"{f.get('name', '')} ({f['gid']})" for f in treffer)
        raise ToolFehler(
            f"Der Feldname „{schluessel}“ ist mehrdeutig: {auswahl}. Bitte den Nutzer fragen "
            "und die GID verwenden."
        )
    return treffer[0] if treffer else None


async def loese_felder_auf(
    lauf, felder: dict, verfuegbar: list[dict] | None, ort: str
) -> list[tuple[dict, object, str]]:
    """Liefert je Eintrag (Feld, Wert im API-Format, lesbarer Wert).

    `verfuegbar` sind die Felder, die an der Aufgabe eingerichtet sind; None bedeutet, dass
    das noch nicht feststeht (neues Projekt im selben Satz) – dann zählt der Workspace.
    """
    ergebnis = []
    for schluessel, wert in felder.items():
        schluessel = str(schluessel)
        if schluessel in lauf.neu:
            feld = lauf.neu[schluessel]
        else:
            feld = finde_feld(schluessel, verfuegbar) if verfuegbar is not None else None
            if feld is None:
                im_workspace = finde_feld(schluessel, await workspace_felder(lauf))
                if im_workspace is None:
                    raise ToolFehler(
                        f"Das Feld „{schluessel}“ gibt es nicht. Mit asana_felder_anzeigen "
                        "nachsehen oder mit feld_anlegen anlegen."
                    )
                if verfuegbar is not None:
                    raise ToolFehler(
                        f"Das Feld „{im_workspace.get('name')}“ ist {ort} nicht eingerichtet. "
                        "Sag das dem Nutzer und biete an, es mit feld_zu_projekt_hinzufuegen "
                        f"(feld_gid {im_workspace['gid']}) hinzuzufügen."
                    )
                feld = im_workspace
        api_wert, text = await _wert(lauf, feld, wert)
        ergebnis.append((feld, api_wert, text))
    return ergebnis


async def _wert(lauf, feld: dict, wert: object) -> tuple[object, str]:
    """Wertformate laut Asana-Doku: text String, number Zahl, enum Options-GID, multi_enum
    Liste von Options-GIDs, date Objekt mit `date`, people Liste von Nutzer-GIDs."""
    name = feld.get("name", "")
    typ = feld.get("resource_subtype")
    if wert is None or wert == "" or wert == []:
        return None, "(leer)"
    if typ == "text":
        if not isinstance(wert, str | int | float) or isinstance(wert, bool):
            raise ToolFehler(f"Das Feld „{name}“ erwartet einen Text.")
        return str(wert), str(wert)
    if typ == "number":
        if isinstance(wert, str):
            try:
                wert = float(wert.replace(",", "."))
            except ValueError:
                raise ToolFehler(f"Das Feld „{name}“ erwartet eine Zahl.") from None
        if isinstance(wert, bool) or not isinstance(wert, int | float):
            raise ToolFehler(f"Das Feld „{name}“ erwartet eine Zahl.")
        zahl = int(wert) if float(wert).is_integer() else float(wert)
        return zahl, str(zahl).replace(".", ",")
    if typ == "enum":
        if isinstance(wert, list):
            raise ToolFehler(f"Das Feld „{name}“ erlaubt nur eine Option.")
        option = _option(feld, wert)
        return option["gid"], option.get("name", "")
    if typ == "multi_enum":
        optionen = [_option(feld, w) for w in (wert if isinstance(wert, list) else [wert])]
        return [o["gid"] for o in optionen], ", ".join(o.get("name", "") for o in optionen)
    if typ == "date":
        return _datum(name, wert, lauf.zone)
    if typ == "people":
        nutzer = [
            await lauf.nutzer_aufloesen(w) for w in (wert if isinstance(wert, list) else [wert])
        ]
        return [n["gid"] for n in nutzer], ", ".join(n.get("name", "") for n in nutzer)
    raise ToolFehler(f"Das Feld „{name}“ hat den Typ {typ}; den kann ich nicht setzen.")


def _option(feld: dict, wert: object) -> dict:
    name = feld.get("name", "")
    if not isinstance(wert, str | int) or isinstance(wert, bool):
        raise ToolFehler(f"Das Feld „{name}“ erwartet den Namen oder die GID einer Option.")
    wert = str(wert)
    aktive = [o for o in feld.get("enum_options") or [] if o.get("enabled", True)]
    treffer = [o for o in aktive if o["gid"] == wert]
    if not treffer:
        treffer = [o for o in aktive if _gleich(o.get("name"), wert)]
    if len(treffer) == 1:
        return treffer[0]
    auswahl = ", ".join(o.get("name", "") for o in aktive) or "keine"
    if treffer:
        raise ToolFehler(
            f"Die Option „{wert}“ im Feld „{name}“ ist mehrdeutig. Bitte die GID verwenden."
        )
    raise ToolFehler(
        f"Das Feld „{name}“ hat keine Option „{wert}“. Vorhanden: {auswahl}. Eine neue Option "
        "legt feldoption_hinzufuegen an."
    )


def _datum(name: str, wert: object, zone: ZoneInfo) -> tuple[dict, str]:
    fehler = ToolFehler(
        f"Das Feld „{name}“ erwartet ein Datum (JJJJ-MM-TT) oder Datum mit Uhrzeit."
    )
    if isinstance(wert, dict):
        wert = wert.get("date_time") or wert.get("date")
    if not isinstance(wert, str):
        raise fehler
    try:
        if len(wert) <= 10:
            tag = date.fromisoformat(wert)
            return {"date": tag.isoformat()}, f"{tag:%d.%m.%Y}"
        zeitpunkt = datetime.fromisoformat(wert)
    except ValueError:
        raise fehler from None
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.replace(tzinfo=zone)
    lokal = zeitpunkt.astimezone(zone)
    return (
        {"date_time": zeitpunkt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
        f"{lokal:%d.%m.%Y %H:%M}",
    )
