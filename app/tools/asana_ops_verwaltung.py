"""Operationen für Zeiterfassung, Statusmeldungen, Projekt-Briefing, Portfolios und Ziele."""

import html

from app.tools.asana_client import kuerze_text
from app.tools.asana_operationen import (
    BOOL_FELDER,
    DATUM_FELDER,
    FARBEN,
    GID_FELDER,
    KATEGORIE_AENDERN,
    KATEGORIE_ANLEGEN,
    KATEGORIE_LOESCHEN,
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
    datum_text,
    merke_neu,
    q,
    registriere,
)
from app.tools.base import ToolFehler

# Erlaubte Werte von status_type je Elternobjekt, laut Asana-Doku zu Status-Updates
STAND_JE_ART = {
    "Projekt": ("on_track", "at_risk", "off_track", "on_hold", "complete", "dropped"),
    "Portfolio": ("on_track", "at_risk", "off_track", "on_hold", "complete", "dropped"),
    "Ziel": ("on_track", "at_risk", "off_track", "achieved", "partial", "missed", "dropped"),
}
STAND = tuple(dict.fromkeys(wert for werte in STAND_JE_ART.values() for wert in werte))
ZIEL_STAND = ("green", "yellow", "red", "achieved", "partial", "missed", "dropped")
PORTFOLIO_FARBEN = tuple(farbe for farbe in FARBEN if farbe != "none")

REF_FELDER.update({"portfolio_gid": "portfolio", "ziel_gid": "ziel", "teilziel_gid": "ziel"})
TYP_NAME.update({"portfolio": "ein Portfolio", "ziel": "ein Ziel"})
GID_FELDER.update({"zeiteintrag_gid", "statusmeldung_gid"})
ZAHL_FELDER.add("minuten")
DATUM_FELDER.add("datum")
BOOL_FELDER.add("oeffentlich")
WAHL_FELDER.update({"stand": STAND, "ziel_stand": ZIEL_STAND})
ZUSATZ_SCHEMA.update(
    {
        "portfolio_gid": {"type": "string", "description": "GID eines Portfolios oder Platzhalter"},
        "ziel_gid": {"type": "string", "description": "GID eines Ziels oder Platzhalter"},
        "teilziel_gid": {"type": "string", "description": "GID eines Ziels, das Teilziel wird"},
        "zeiteintrag_gid": {"type": "string"},
        "statusmeldung_gid": {"type": "string"},
        "minuten": {"type": "integer", "minimum": 1, "description": "Dauer in Minuten"},
        "datum": {"type": "string", "description": "JJJJ-MM-TT"},
        "titel": {"type": "string"},
        "stand": {"type": "string", "enum": list(STAND)},
        "ziel_stand": {"type": "string", "enum": list(ZIEL_STAND)},
        "oeffentlich": {"type": "boolean"},
    }
)
ZUSATZ_BESCHREIBUNG.extend(
    [
        "- zeit_erfassen: aufgabe_gid, minuten, datum (Standard: heute)",
        "- zeit_aendern: zeiteintrag_gid, minuten, datum; zeit_loeschen: zeiteintrag_gid",
        "- statusmeldung_erstellen: projekt ODER portfolio_gid ODER ziel_gid, dazu stand "
        "(Projekt/Portfolio: on_track, at_risk, off_track, on_hold, complete, dropped; Ziel: "
        "on_track, at_risk, off_track, achieved, partial, missed, dropped), titel, text",
        "- statusmeldung_loeschen: statusmeldung_gid",
        "- projektbriefing_setzen: projekt, text, titel (legt das Briefing an oder ersetzt es); "
        "projektbriefing_loeschen: projekt",
        "- portfolio_anlegen: name, farbe, oeffentlich; Platzhalter $o1",
        "- portfolio_aendern: portfolio_gid, name, farbe, oeffentlich, faellig",
        "- portfolio_loeschen: portfolio_gid",
        "- portfolio_projekt_hinzufuegen, portfolio_projekt_entfernen: portfolio_gid, projekt",
        "- ziel_anlegen: name, beschreibung, faellig, startdatum, besitzer_gid, team_gid; "
        "Platzhalter $z1",
        "- ziel_aendern: ziel_gid, name, beschreibung, faellig, startdatum, besitzer_gid, "
        "ziel_stand (green, yellow, red, achieved, partial, missed, dropped; nur bei Zielen mit "
        "Messgröße)",
        "- ziel_loeschen: ziel_gid",
        "- ziel_aufgabe_oder_projekt_verknuepfen: ziel_gid plus genau eines von projekt, "
        "aufgabe_gid, portfolio_gid, teilziel_gid",
    ]
)


async def _portfolio(ref: str, lauf: Lauf) -> dict:
    return await lauf.hole(
        "portfolio", ref, "/portfolios", ("name", "color", "public", "due_on", "permalink_url")
    )


async def _ziel(ref: str, lauf: Lauf) -> dict:
    return await lauf.hole(
        "ziel", ref, "/goals", ("name", "notes", "due_on", "start_on", "owner.name", "status")
    )


def _minuten(op: dict) -> int:
    minuten = op["minuten"]
    if minuten != int(minuten) or minuten <= 0:
        raise ToolFehler("„minuten“ muss eine ganze Zahl größer als 0 sein.")
    return int(minuten)


def _dauer(minuten: int | None) -> str:
    if minuten is None:
        return "(leer)"
    stunden, rest = divmod(int(minuten), 60)
    return f"{stunden} h {rest} min" if stunden else f"{rest} min"


# --------------------------------------------------------------------------------------
# Zeiterfassung
# --------------------------------------------------------------------------------------


@registriere(
    "zeit_erfassen", KATEGORIE_ANLEGEN, pflicht={"aufgabe_gid", "minuten"}, optional={"datum"}
)
class ZeitErfassen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        tag = f" am {datum_text(op['datum'])}" if "datum" in op else " (heute)"
        return f"Zeit erfassen: {_dauer(_minuten(op))} für {bez(aufgabe)}{tag}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        daten: dict = {"duration_minutes": _minuten(op)}
        if "datum" in op:
            daten["entered_on"] = op["datum"]
        neu = await lauf.asana.post(f"/tasks/{op['aufgabe_gid']}/time_tracking_entries", daten)
        return OpErgebnis(
            gid=neu["gid"],
            text=f"{_dauer(daten['duration_minutes'])} für {q(aufgabe.get('name'))} erfasst",
            link=aufgabe.get("permalink_url", ""),
            felder=("zeit",),
        )


async def _zeiteintrag(op: dict, lauf: Lauf) -> dict:
    return await lauf.hole(
        "zeiteintrag",
        op["zeiteintrag_gid"],
        "/time_tracking_entries",
        ("duration_minutes", "entered_on", "task.name"),
    )


@registriere(
    "zeit_aendern", KATEGORIE_AENDERN, pflicht={"zeiteintrag_gid"}, optional={"minuten", "datum"}
)
class ZeitAendern:
    @staticmethod
    def _daten(op: dict) -> dict:
        daten: dict = {}
        if "minuten" in op:
            daten["duration_minutes"] = _minuten(op)
        if "datum" in op:
            daten["entered_on"] = op["datum"]
        if not daten:
            raise ToolFehler("zeit_aendern braucht „minuten“ oder „datum“.")
        return daten

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        daten = ZeitAendern._daten(op)
        eintrag = await _zeiteintrag(op, lauf)
        teile = []
        if "duration_minutes" in daten:
            teile.append(
                f"{_dauer(eintrag.get('duration_minutes'))} → {_dauer(daten['duration_minutes'])}"
            )
        if "entered_on" in daten:
            teile.append(
                f"Datum {datum_text(eintrag.get('entered_on'))} → {datum_text(daten['entered_on'])}"
            )
        aufgabe = (eintrag.get("task") or {}).get("name")
        return f"Zeiteintrag ändern ({q(aufgabe)}): " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = ZeitAendern._daten(op)
        eintrag = await _zeiteintrag(op, lauf)
        await lauf.asana.put(f"/time_tracking_entries/{op['zeiteintrag_gid']}", daten)
        lauf.vergiss("zeiteintrag", op["zeiteintrag_gid"])
        return OpErgebnis(
            gid=op["zeiteintrag_gid"],
            text="Zeiteintrag geändert",
            felder=tuple(sorted(set(op) - {"operation", "zeiteintrag_gid"})),
            vorher={
                "minuten": eintrag.get("duration_minutes"),
                "datum": eintrag.get("entered_on"),
            },
        )


@registriere("zeit_loeschen", KATEGORIE_LOESCHEN, pflicht={"zeiteintrag_gid"})
class ZeitLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        eintrag = await _zeiteintrag(op, lauf)
        aufgabe = (eintrag.get("task") or {}).get("name")
        return (
            f"{LOESCH_MARKE} Zeiteintrag {_dauer(eintrag.get('duration_minutes'))} vom "
            f"{datum_text(eintrag.get('entered_on'))} an {q(aufgabe)}"
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        eintrag = await _zeiteintrag(op, lauf)
        await lauf.asana.delete(f"/time_tracking_entries/{op['zeiteintrag_gid']}")
        lauf.vergiss("zeiteintrag", op["zeiteintrag_gid"])
        return OpErgebnis(
            gid=op["zeiteintrag_gid"],
            text="Zeiteintrag gelöscht",
            vorher={
                "minuten": eintrag.get("duration_minutes"),
                "datum": eintrag.get("entered_on"),
            },
        )


# --------------------------------------------------------------------------------------
# Statusmeldungen
# --------------------------------------------------------------------------------------


async def _elternobjekt(op: dict, lauf: Lauf) -> tuple[str, dict]:
    angegeben = [feld for feld in ("projekt", "portfolio_gid", "ziel_gid") if feld in op]
    if len(angegeben) != 1:
        raise ToolFehler("Bitte genau eines angeben: „projekt“, „portfolio_gid“ oder „ziel_gid“.")
    if "projekt" in op:
        return "Projekt", await lauf.projekt(op["projekt"])
    if "portfolio_gid" in op:
        return "Portfolio", await _portfolio(op["portfolio_gid"], lauf)
    return "Ziel", await _ziel(op["ziel_gid"], lauf)


@registriere(
    "statusmeldung_erstellen",
    KATEGORIE_ANLEGEN,
    pflicht={"stand", "titel"},
    optional={"projekt", "portfolio_gid", "ziel_gid", "text"},
)
class StatusmeldungErstellen:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[str, dict]:
        art, eltern = await _elternobjekt(op, lauf)
        if op["stand"] not in STAND_JE_ART[art]:
            raise ToolFehler(
                f"Für ein {art} ist der Stand „{op['stand']}“ nicht möglich. Erlaubt sind: "
                f"{', '.join(STAND_JE_ART[art])}."
            )
        return art, eltern

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        art, eltern = await StatusmeldungErstellen._plan(op, lauf)
        text = f", Text: „{kuerze_text(op['text'], 200)}“" if "text" in op else ""
        return f"Statusmeldung für {art} {bez(eltern)}: {op['stand']}, Titel {q(op['titel'])}{text}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        art, eltern = await StatusmeldungErstellen._plan(op, lauf)
        daten = {"parent": eltern["gid"], "status_type": op["stand"], "title": op["titel"]}
        if "text" in op:
            daten["text"] = op["text"]
        neu = await lauf.asana.post("/status_updates", daten)
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Statusmeldung {q(op['titel'])} für {art} {q(eltern.get('name'))} erstellt",
            link=eltern.get("permalink_url", ""),
            ist_projekt=art == "Projekt",
            felder=("stand", "titel", "text"),
        )


@registriere("statusmeldung_loeschen", KATEGORIE_LOESCHEN, pflicht={"statusmeldung_gid"})
class StatusmeldungLoeschen:
    @staticmethod
    async def _meldung(op: dict, lauf: Lauf) -> dict:
        return await lauf.hole(
            "statusmeldung",
            op["statusmeldung_gid"],
            "/status_updates",
            ("title", "status_type", "parent.name"),
        )

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        meldung = await StatusmeldungLoeschen._meldung(op, lauf)
        ort = (meldung.get("parent") or {}).get("name")
        return f"{LOESCH_MARKE} Statusmeldung {q(meldung.get('title'))}" + (
            f" von {q(ort)}" if ort else ""
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        meldung = await StatusmeldungLoeschen._meldung(op, lauf)
        await lauf.asana.delete(f"/status_updates/{op['statusmeldung_gid']}")
        return OpErgebnis(
            gid=op["statusmeldung_gid"],
            text=f"Statusmeldung {q(meldung.get('title'))} gelöscht",
            vorher={"titel": meldung.get("title", ""), "stand": meldung.get("status_type")},
        )


# --------------------------------------------------------------------------------------
# Projekt-Briefing
# --------------------------------------------------------------------------------------


async def _briefing(op: dict, lauf: Lauf) -> dict | None:
    """Das vorhandene Briefing des Projekts oder None."""
    if op["projekt"] in lauf.neu:
        return None
    projekt = await lauf.hole(
        "projekt_briefing", op["projekt"], "/projects", ("project_brief.title",)
    )
    return projekt.get("project_brief")


@registriere(
    "projektbriefing_setzen", KATEGORIE_AENDERN, pflicht={"projekt", "text"}, optional={"titel"}
)
class ProjektbriefingSetzen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        projekt = await lauf.projekt(op["projekt"])
        vorhanden = await _briefing(op, lauf)
        verb = "ersetzen" if vorhanden else "anlegen"
        return f"Projekt-Briefing {verb}: {bez(projekt)}, Text: „{kuerze_text(op['text'], 200)}“"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        projekt = await lauf.projekt(op["projekt"])
        vorhanden = await _briefing(op, lauf)
        # Asana erwartet formatierten Text als HTML mit <body> als Wurzel.
        daten = {"html_text": f"<body>{html.escape(op['text'], quote=False)}</body>"}
        if "titel" in op:
            daten["title"] = op["titel"]
        if vorhanden:
            await lauf.asana.put(f"/project_briefs/{vorhanden['gid']}", daten)
            gid = vorhanden["gid"]
        else:
            gid = (await lauf.asana.post(f"/projects/{op['projekt']}/project_briefs", daten))["gid"]
        lauf.vergiss("projekt_briefing", op["projekt"])
        return OpErgebnis(
            gid=gid,
            text=f"Briefing für Projekt {q(projekt.get('name'))} "
            + ("ersetzt" if vorhanden else "angelegt"),
            link=projekt.get("permalink_url", ""),
            ist_projekt=True,
            felder=("briefing",),
            vorher={"briefing_vorhanden": bool(vorhanden)},
        )


@registriere("projektbriefing_loeschen", KATEGORIE_LOESCHEN, pflicht={"projekt"})
class ProjektbriefingLoeschen:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, dict]:
        projekt = await lauf.projekt(op["projekt"])
        vorhanden = await _briefing(op, lauf)
        if not vorhanden:
            raise ToolFehler(f"Das Projekt {bez(projekt)} hat kein Briefing.")
        return projekt, vorhanden

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        projekt, _ = await ProjektbriefingLoeschen._plan(op, lauf)
        return f"{LOESCH_MARKE} Briefing des Projekts {bez(projekt)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        projekt, vorhanden = await ProjektbriefingLoeschen._plan(op, lauf)
        await lauf.asana.delete(f"/project_briefs/{vorhanden['gid']}")
        lauf.vergiss("projekt_briefing", op["projekt"])
        return OpErgebnis(
            gid=vorhanden["gid"],
            text=f"Briefing des Projekts {q(projekt.get('name'))} gelöscht",
            vorher={"titel": vorhanden.get("title", "")},
        )


# --------------------------------------------------------------------------------------
# Portfolios
# --------------------------------------------------------------------------------------


def _portfolio_daten(op: dict) -> dict:
    daten: dict = {}
    if "name" in op:
        daten["name"] = op["name"]
    if "farbe" in op:
        if op["farbe"] not in PORTFOLIO_FARBEN:
            raise ToolFehler("Portfolios haben immer eine Farbe; „none“ gibt es hier nicht.")
        daten["color"] = op["farbe"]
    if "oeffentlich" in op:
        daten["public"] = op["oeffentlich"]
    if "faellig" in op:
        daten["due_on"] = op["faellig"]
    return daten


@registriere(
    "portfolio_anlegen",
    KATEGORIE_ANLEGEN,
    pflicht={"name"},
    optional={"farbe", "oeffentlich"},
    erzeugt="portfolio",
)
class PortfolioAnlegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        _portfolio_daten(op)
        merke_neu(op, lauf)
        sichtbar = "für alle im Workspace sichtbar" if op.get("oeffentlich") else "privat"
        return f"Anlegen: Portfolio {q(op['name'])} ({sichtbar})"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = {**_portfolio_daten(op), "workspace": await lauf.asana.workspace_gid()}
        neu = await lauf.asana.post("/portfolios", daten, felder=("name", "permalink_url"))
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Portfolio {q(op['name'])} angelegt",
            link=neu.get("permalink_url", ""),
            felder=tuple(sorted(set(op) - {"operation", "platzhalter"})),
        )


@registriere(
    "portfolio_aendern",
    KATEGORIE_AENDERN,
    pflicht={"portfolio_gid"},
    optional={"name", "farbe", "oeffentlich", "faellig"},
)
class PortfolioAendern:
    @staticmethod
    def _daten(op: dict) -> dict:
        daten = _portfolio_daten(op)
        if not daten:
            raise ToolFehler("portfolio_aendern braucht mindestens ein zu änderndes Feld.")
        return daten

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        daten = PortfolioAendern._daten(op)
        portfolio = await _portfolio(op["portfolio_gid"], lauf)
        teile = []
        for feld, neu in daten.items():
            if feld == "name":
                teile.append(f"Name → {q(neu)}")
            elif feld == "color":
                teile.append(f"Farbe {portfolio.get('color') or '(leer)'} → {neu}")
            elif feld == "public":
                teile.append("wird für alle sichtbar" if neu else "wird privat")
            else:
                teile.append(f"Fällig {datum_text(portfolio.get('due_on'))} → {datum_text(neu)}")
        return f"Ändern: Portfolio {bez(portfolio)}: " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = PortfolioAendern._daten(op)
        portfolio = await _portfolio(op["portfolio_gid"], lauf)
        await lauf.asana.put(f"/portfolios/{op['portfolio_gid']}", daten)
        lauf.vergiss("portfolio", op["portfolio_gid"])
        return OpErgebnis(
            gid=op["portfolio_gid"],
            text=f"Portfolio {q(portfolio.get('name'))} geändert",
            link=portfolio.get("permalink_url", ""),
            felder=tuple(sorted(set(op) - {"operation", "portfolio_gid"})),
            vorher={feld: portfolio.get(feld) for feld in daten},
        )


@registriere("portfolio_loeschen", KATEGORIE_LOESCHEN, pflicht={"portfolio_gid"})
class PortfolioLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        portfolio = await _portfolio(op["portfolio_gid"], lauf)
        return f"{LOESCH_MARKE} Portfolio {bez(portfolio)} (die Projekte darin bleiben erhalten)"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        portfolio = await _portfolio(op["portfolio_gid"], lauf)
        await lauf.asana.delete(f"/portfolios/{op['portfolio_gid']}")
        lauf.vergiss("portfolio", op["portfolio_gid"])
        return OpErgebnis(
            gid=op["portfolio_gid"],
            text=f"Portfolio {q(portfolio.get('name'))} gelöscht",
            vorher={"name": portfolio.get("name", "")},
        )


def _portfolio_projekt(art: str, pfad: str, vorschau_text: str, ergebnis_text: str) -> None:
    class PortfolioProjekt:
        @staticmethod
        async def vorschau(op: dict, lauf: Lauf) -> str:
            portfolio = await _portfolio(op["portfolio_gid"], lauf)
            projekt = await lauf.projekt(op["projekt"])
            return vorschau_text.format(projekt=bez(projekt), portfolio=bez(portfolio))

        @staticmethod
        async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
            portfolio = await _portfolio(op["portfolio_gid"], lauf)
            projekt = await lauf.projekt(op["projekt"])
            await lauf.asana.post(
                f"/portfolios/{op['portfolio_gid']}/{pfad}", {"item": op["projekt"]}
            )
            return OpErgebnis(
                gid=op["portfolio_gid"],
                text=ergebnis_text.format(
                    projekt=q(projekt.get("name")), portfolio=q(portfolio.get("name"))
                ),
                link=portfolio.get("permalink_url", ""),
                felder=("projekte",),
            )

    registriere(art, KATEGORIE_AENDERN, pflicht={"portfolio_gid", "projekt"})(PortfolioProjekt)


_portfolio_projekt(
    "portfolio_projekt_hinzufuegen",
    "addItem",
    "Projekt {projekt} ins Portfolio {portfolio} aufnehmen",
    "Projekt {projekt} ins Portfolio {portfolio} aufgenommen",
)
_portfolio_projekt(
    "portfolio_projekt_entfernen",
    "removeItem",
    "Projekt {projekt} aus dem Portfolio {portfolio} herausnehmen",
    "Projekt {projekt} aus dem Portfolio {portfolio} herausgenommen",
)


# --------------------------------------------------------------------------------------
# Ziele
# --------------------------------------------------------------------------------------


def _ziel_daten(op: dict, ziel: dict | None = None) -> dict:
    daten: dict = {}
    if "name" in op:
        daten["name"] = op["name"]
    if "beschreibung" in op:
        daten["notes"] = op["beschreibung"]
    if "faellig" in op:
        daten["due_on"] = op["faellig"]
    if "startdatum" in op:
        # Laut Doku geht ein Start nur zusammen mit einer Fälligkeit.
        if not (op.get("faellig") or (ziel or {}).get("due_on") or (ziel or {}).get("neu")):
            raise ToolFehler("Ein Startdatum braucht bei einem Ziel auch eine Fälligkeit.")
        daten["start_on"] = op["startdatum"]
        daten.setdefault("due_on", (ziel or {}).get("due_on"))
        if daten["due_on"] is None:
            del daten["due_on"]
    if "besitzer_gid" in op:
        daten["owner"] = op["besitzer_gid"]
    if "team_gid" in op:
        daten["team"] = op["team_gid"]
    if "ziel_stand" in op:
        daten["status"] = op["ziel_stand"]
    return daten


@registriere(
    "ziel_anlegen",
    KATEGORIE_ANLEGEN,
    pflicht={"name"},
    optional={"beschreibung", "faellig", "startdatum", "besitzer_gid", "team_gid"},
    erzeugt="ziel",
)
class ZielAnlegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        daten = _ziel_daten(op)
        teile = []
        if "team" in daten:
            teile.append(f"Team {q((await lauf.team(daten['team'])).get('name'))}")
        if "start_on" in daten:
            teile.append(f"Start {datum_text(daten['start_on'])}")
        if "due_on" in daten:
            teile.append(f"fällig {datum_text(daten['due_on'])}")
        if "owner" in daten:
            teile.append(f"Besitzer {(await lauf.nutzer(daten['owner'])).get('name', '')}")
        merke_neu(op, lauf)
        return f"Anlegen: Ziel {q(op['name'])}" + (f" ({', '.join(teile)})" if teile else "")

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = {**_ziel_daten(op), "workspace": await lauf.asana.workspace_gid()}
        neu = await lauf.asana.post("/goals", daten)
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Ziel {q(op['name'])} angelegt",
            felder=tuple(sorted(set(op) - {"operation", "platzhalter"})),
        )


@registriere(
    "ziel_aendern",
    KATEGORIE_AENDERN,
    pflicht={"ziel_gid"},
    optional={"name", "beschreibung", "faellig", "startdatum", "besitzer_gid", "ziel_stand"},
)
class ZielAendern:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, dict]:
        ziel = await _ziel(op["ziel_gid"], lauf)
        daten = _ziel_daten(op, ziel)
        if not daten:
            raise ToolFehler("ziel_aendern braucht mindestens ein zu änderndes Feld.")
        return ziel, daten

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        ziel, daten = await ZielAendern._plan(op, lauf)
        teile = []
        if "name" in op:
            teile.append(f"Name → {q(op['name'])}")
        if "beschreibung" in op:
            teile.append("Beschreibung geändert")
        if "startdatum" in op:
            teile.append(
                f"Start {datum_text(ziel.get('start_on'))} → {datum_text(op['startdatum'])}"
            )
        if "faellig" in op:
            teile.append(f"Fällig {datum_text(ziel.get('due_on'))} → {datum_text(op['faellig'])}")
        if "besitzer_gid" in op:
            alt = (ziel.get("owner") or {}).get("name") or "(leer)"
            teile.append(f"Besitzer {alt} → {(await lauf.nutzer(op['besitzer_gid'])).get('name')}")
        if "ziel_stand" in op:
            teile.append(f"Stand {ziel.get('status') or '(leer)'} → {op['ziel_stand']}")
        return f"Ändern: Ziel {bez(ziel)}: " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        ziel, daten = await ZielAendern._plan(op, lauf)
        await lauf.asana.put(f"/goals/{op['ziel_gid']}", daten)
        lauf.vergiss("ziel", op["ziel_gid"])
        return OpErgebnis(
            gid=op["ziel_gid"],
            text=f"Ziel {q(ziel.get('name'))} geändert",
            felder=tuple(sorted(set(op) - {"operation", "ziel_gid"})),
            vorher={
                "name": ziel.get("name", ""),
                "due_on": ziel.get("due_on"),
                "start_on": ziel.get("start_on"),
                "stand": ziel.get("status"),
                "besitzer": (ziel.get("owner") or {}).get("name", ""),
            },
        )


@registriere("ziel_loeschen", KATEGORIE_LOESCHEN, pflicht={"ziel_gid"})
class ZielLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        return f"{LOESCH_MARKE} Ziel {bez(await _ziel(op['ziel_gid'], lauf))}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        ziel = await _ziel(op["ziel_gid"], lauf)
        await lauf.asana.delete(f"/goals/{op['ziel_gid']}")
        lauf.vergiss("ziel", op["ziel_gid"])
        return OpErgebnis(
            gid=op["ziel_gid"],
            text=f"Ziel {q(ziel.get('name'))} gelöscht",
            vorher={"name": ziel.get("name", "")},
        )


@registriere(
    "ziel_aufgabe_oder_projekt_verknuepfen",
    KATEGORIE_AENDERN,
    pflicht={"ziel_gid"},
    optional={"projekt", "aufgabe_gid", "portfolio_gid", "teilziel_gid"},
)
class ZielVerknuepfen:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, str, dict]:
        angegeben = [
            feld
            for feld in ("projekt", "aufgabe_gid", "portfolio_gid", "teilziel_gid")
            if feld in op
        ]
        if len(angegeben) != 1:
            raise ToolFehler(
                "Bitte genau eines angeben: „projekt“, „aufgabe_gid“, „portfolio_gid“ oder "
                "„teilziel_gid“."
            )
        ziel = await _ziel(op["ziel_gid"], lauf)
        feld = angegeben[0]
        if feld == "projekt":
            return ziel, "Projekt", await lauf.projekt(op[feld])
        if feld == "aufgabe_gid":
            return ziel, "Aufgabe", await lauf.aufgabe(op[feld])
        if feld == "portfolio_gid":
            return ziel, "Portfolio", await _portfolio(op[feld], lauf)
        if op[feld] == op["ziel_gid"]:
            raise ToolFehler("Ein Ziel kann nicht sein eigenes Teilziel sein.")
        return ziel, "Teilziel", await _ziel(op[feld], lauf)

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        ziel, art, objekt = await ZielVerknuepfen._plan(op, lauf)
        return f"Verknüpfen: {art} {bez(objekt)} unterstützt das Ziel {bez(ziel)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        ziel, art, objekt = await ZielVerknuepfen._plan(op, lauf)
        await lauf.asana.post(
            f"/goals/{op['ziel_gid']}/addSupportingRelationship",
            {"supporting_resource": objekt["gid"]},
        )
        return OpErgebnis(
            gid=op["ziel_gid"],
            text=f"{art} {q(objekt.get('name'))} mit dem Ziel {q(ziel.get('name'))} verknüpft",
            felder=("verknuepfung",),
        )
