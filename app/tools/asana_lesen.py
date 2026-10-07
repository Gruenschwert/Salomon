"""Asana-Lese-Tools (laufen ohne Freigabe)."""

from datetime import date, timedelta

from app.tools.asana_client import (
    MAX_EINTRAEGE,
    AsanaClient,
    AsanaFehler,
    begrenze,
    kuerze_text,
    pruefe_gid,
)
from app.tools.base import BasisTool, ToolFehler, ToolKontext

# So viele Einträge werden höchstens geladen, wenn erst danach gefiltert oder gezählt wird.
MAX_GELADEN = 500
MAX_KOMMENTARE = 10
MAX_UNTERAUFGABEN = 30
MAX_BESCHREIBUNG_ZEICHEN = 2000
MAX_KOMMENTAR_ZEICHEN = 500
ICH = "me"

_AUFGABEN_FELDER = (
    "name",
    "completed",
    "due_on",
    "due_at",
    "assignee.name",
    "memberships.project.name",
    "memberships.section.name",
)


class AsanaLeseTool(BasisTool):
    """Gemeinsame Basis; ohne `name` findet die Registry sie nicht als eigenes Tool."""

    def __init__(self, kontext: ToolKontext) -> None:
        super().__init__(kontext)
        self.asana = AsanaClient(kontext)


class AsanaProjekteSuchen(AsanaLeseTool):
    name = "asana_projekte_suchen"
    beschreibung = (
        "Sucht Asana-Projekte nach Namen. Ohne Suchbegriff werden alle Projekte gelistet. "
        "Rückgabe je Projekt: GID, Name, Team, Besitzer, Fälligkeit, Link. Nutze es, um die GID "
        "eines Projekts zu ermitteln, bevor du andere Asana-Tools aufrufst."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "suche": {"type": "string", "description": "Teil des Projektnamens (optional)"},
            "nur_aktive": {
                "type": "boolean",
                "description": "Archivierte Projekte ausblenden (Standard: true)",
            },
        },
    }

    async def ausfuehren(self, suche: str = "", nur_aktive: bool = True) -> dict:
        async with self.asana as asana:
            params = {"workspace": await asana.workspace_gid()}
            if nur_aktive:
                params["archived"] = "false"
            projekte, weitere = await asana.liste(
                "/projects",
                params,
                felder=("name", "team.name", "owner.name", "due_on", "archived", "permalink_url"),
                max_eintraege=MAX_GELADEN,
            )
        treffer = [
            {
                "gid": p["gid"],
                "name": p.get("name", ""),
                "team": _name(p.get("team")),
                "besitzer": _name(p.get("owner")),
                "faellig": p.get("due_on"),
                "archiviert": bool(p.get("archived")),
                "link": p.get("permalink_url", ""),
            }
            for p in projekte
            if _passt(suche, p.get("name"))
        ]
        return begrenze(treffer, weitere)


class AsanaAufgabenSuchen(AsanaLeseTool):
    name = "asana_aufgaben_suchen"
    beschreibung = (
        "Sucht Asana-Aufgaben. Filter: Text im Namen, Projekt, Zuständiger, Fälligkeit von/bis. "
        "Für „meine Aufgaben“ zustaendig_gid=me. Mindestens ein Filter ist nötig. Rückgabe je "
        "Aufgabe: GID, Name, Fälligkeit, Zuständiger, Abschnitt, Projekt, erledigt. Höchstens "
        "30 Treffer."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text im Aufgabennamen"},
            "projekt_gid": {"type": "string", "description": "GID des Projekts"},
            "zustaendig_gid": {
                "type": "string",
                "description": "GID des Zuständigen oder „me“ für den Inhaber des Asana-Tokens",
            },
            "faellig_von": {"type": "string", "description": "Fällig ab, JJJJ-MM-TT (einschl.)"},
            "faellig_bis": {"type": "string", "description": "Fällig bis, JJJJ-MM-TT (einschl.)"},
            "nur_offene": {
                "type": "boolean",
                "description": "Nur nicht erledigte Aufgaben (Standard: true)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_EINTRAEGE,
                "description": "Maximale Anzahl (Standard und Höchstwert: 30)",
            },
        },
    }

    def __init__(self, kontext: ToolKontext) -> None:
        super().__init__(kontext)
        # Die Workspace-Suche gibt es nur in bezahlten Tarifen; nach dem ersten 402 wird sie
        # nicht mehr versucht.
        self._suche_verfuegbar = True

    async def ausfuehren(
        self,
        text: str = "",
        projekt_gid: str = "",
        zustaendig_gid: str = "",
        faellig_von: str = "",
        faellig_bis: str = "",
        nur_offene: bool = True,
        limit: int = MAX_EINTRAEGE,
    ) -> dict:
        text = (text or "").strip()
        projekt_gid = pruefe_gid(projekt_gid, "projekt_gid") if projekt_gid else ""
        if zustaendig_gid and zustaendig_gid != ICH:
            zustaendig_gid = pruefe_gid(zustaendig_gid, "zustaendig_gid")
        von = _datum(faellig_von, "faellig_von")
        bis = _datum(faellig_bis, "faellig_bis")
        limit = max(1, min(int(limit), MAX_EINTRAEGE))
        if not (text or projekt_gid or zustaendig_gid or von or bis):
            raise ToolFehler("Bitte mindestens einen Filter angeben.")

        async with self.asana as asana:
            aufgaben = weitere = None
            if self._suche_verfuegbar:
                try:
                    aufgaben, weitere = await self._ueber_suche(
                        asana, text, projekt_gid, zustaendig_gid, von, bis, nur_offene
                    )
                except AsanaFehler as exc:
                    if exc.status != 402:
                        raise
                    self._suche_verfuegbar = False
            if aufgaben is None:
                aufgaben, weitere = await self._ueber_liste(
                    asana, projekt_gid, zustaendig_gid, nur_offene
                )

        # Beide Wege werden hier noch einmal gefiltert: Die Liste kennt die Filter gar nicht,
        # und die Suche wird bewusst etwas zu weit gefasst abgefragt.
        treffer = [
            _aufgabe_kurz(a)
            for a in aufgaben
            if _passt(text, a.get("name"))
            and not (nur_offene and a.get("completed"))
            and _im_zeitraum(a.get("due_on"), von, bis)
        ]
        treffer.sort(key=lambda a: (a["faellig"] is None, a["faellig"] or ""))
        return begrenze(treffer, weitere, maximum=limit)

    async def _ueber_suche(
        self,
        asana: AsanaClient,
        text: str,
        projekt_gid: str,
        zustaendig_gid: str,
        von: date | None,
        bis: date | None,
        nur_offene: bool,
    ) -> tuple[list[dict], bool]:
        params = {
            "text": text or None,
            "projects.any": projekt_gid or None,
            "assignee.any": zustaendig_gid or None,
            # Einen Tag weiter gefasst, damit die Grenztage sicher enthalten sind.
            "due_on.after": (von - timedelta(days=1)).isoformat() if von else None,
            "due_on.before": (bis + timedelta(days=1)).isoformat() if bis else None,
            "completed": "false" if nur_offene else None,
            "sort_by": "due_date",
            "sort_ascending": "true",
            "limit": 100,
        }
        pfad = f"/workspaces/{await asana.workspace_gid()}/tasks/search"
        aufgaben = await asana.get(pfad, params, felder=_AUFGABEN_FELDER)
        return aufgaben, len(aufgaben) >= 100

    async def _ueber_liste(
        self, asana: AsanaClient, projekt_gid: str, zustaendig_gid: str, nur_offene: bool
    ) -> tuple[list[dict], bool]:
        if projekt_gid:
            params = {"project": projekt_gid}
        elif zustaendig_gid:
            params = {"assignee": zustaendig_gid, "workspace": await asana.workspace_gid()}
        else:
            raise ToolFehler(
                "Die Suche über den ganzen Workspace ist im aktuellen Asana-Tarif nicht "
                "enthalten. Bitte zusätzlich projekt_gid oder zustaendig_gid angeben."
            )
        if nur_offene:
            params["completed_since"] = "now"
        aufgaben, weitere = await asana.liste(
            "/tasks", params, felder=_AUFGABEN_FELDER, max_eintraege=MAX_GELADEN
        )
        if projekt_gid and zustaendig_gid:
            ich = (await asana.get("/users/me"))["gid"] if zustaendig_gid == ICH else zustaendig_gid
            aufgaben = [a for a in aufgaben if (a.get("assignee") or {}).get("gid") == ich]
        return aufgaben, weitere


class AsanaAufgabeDetails(AsanaLeseTool):
    name = "asana_aufgabe_details"
    beschreibung = (
        "Liefert alle Angaben zu einer Asana-Aufgabe: Felder, Unteraufgaben, Tags, "
        "Abhängigkeiten und die letzten 10 Kommentare. Nutze es vor jeder Änderung an einer "
        "Aufgabe, um den aktuellen Stand zu kennen."
    )
    parameter_schema = {
        "type": "object",
        "properties": {"aufgabe_gid": {"type": "string", "description": "GID der Aufgabe"}},
        "required": ["aufgabe_gid"],
    }

    async def ausfuehren(self, aufgabe_gid: str) -> dict:
        gid = pruefe_gid(aufgabe_gid, "aufgabe_gid")
        async with self.asana as asana:
            aufgabe = await asana.get(
                f"/tasks/{gid}",
                felder=(
                    *_AUFGABEN_FELDER,
                    "notes",
                    "start_on",
                    "resource_subtype",
                    "completed_at",
                    "created_at",
                    "modified_at",
                    "parent.name",
                    "tags.name",
                    "followers.name",
                    "permalink_url",
                ),
            )
            unteraufgaben, mehr_unteraufgaben = await asana.liste(
                f"/tasks/{gid}/subtasks",
                felder=("name", "completed", "due_on", "assignee.name"),
                max_eintraege=MAX_UNTERAUFGABEN,
            )
            abhaengigkeiten, _ = await asana.liste(
                f"/tasks/{gid}/dependencies", felder=("name", "completed")
            )
            verlauf, _ = await asana.liste(
                f"/tasks/{gid}/stories",
                felder=("type", "text", "created_at", "created_by.name"),
                max_eintraege=MAX_GELADEN,
            )
        kommentare = [s for s in verlauf if s.get("type") == "comment"][-MAX_KOMMENTARE:]
        ergebnis = {
            **_aufgabe_kurz(aufgabe),
            "beschreibung": kuerze_text(aufgabe.get("notes"), MAX_BESCHREIBUNG_ZEICHEN),
            "startdatum": aufgabe.get("start_on"),
            "meilenstein": aufgabe.get("resource_subtype") == "milestone",
            "uebergeordnet": _gid_und_name(aufgabe.get("parent")),
            "tags": [_gid_und_name(t) for t in aufgabe.get("tags") or []],
            "follower": [_gid_und_name(f) for f in aufgabe.get("followers") or []],
            "erstellt_am": aufgabe.get("created_at"),
            "geaendert_am": aufgabe.get("modified_at"),
            "erledigt_am": aufgabe.get("completed_at"),
            "link": aufgabe.get("permalink_url", ""),
            "unteraufgaben": [
                {
                    "gid": u["gid"],
                    "name": u.get("name", ""),
                    "erledigt": bool(u.get("completed")),
                    "faellig": u.get("due_on"),
                    "zustaendig": _name(u.get("assignee")),
                }
                for u in unteraufgaben
            ],
            "haengt_ab_von": [
                {"gid": a["gid"], "name": a.get("name", ""), "erledigt": bool(a.get("completed"))}
                for a in abhaengigkeiten
            ],
            "kommentare": [
                {
                    "von": _name(k.get("created_by")),
                    "am": k.get("created_at"),
                    "text": kuerze_text(k.get("text"), MAX_KOMMENTAR_ZEICHEN),
                }
                for k in kommentare
            ],
        }
        if mehr_unteraufgaben:
            ergebnis["hinweis"] = f"Es gibt mehr als {MAX_UNTERAUFGABEN} Unteraufgaben."
        return ergebnis


class AsanaAbschnitteAnzeigen(AsanaLeseTool):
    name = "asana_abschnitte_anzeigen"
    beschreibung = (
        "Zeigt die Abschnitte eines Asana-Projekts mit GID und Anzahl offener Aufgaben. Nutze "
        "es, um Abschnitts-GIDs zu ermitteln oder einen Überblick über ein Projekt zu geben."
    )
    parameter_schema = {
        "type": "object",
        "properties": {"projekt_gid": {"type": "string", "description": "GID des Projekts"}},
        "required": ["projekt_gid"],
    }

    async def ausfuehren(self, projekt_gid: str) -> dict:
        gid = pruefe_gid(projekt_gid, "projekt_gid")
        async with self.asana as asana:
            abschnitte, weitere = await asana.liste(f"/projects/{gid}/sections", felder=("name",))
            eintraege = []
            for abschnitt in abschnitte:
                offene, mehr = await asana.liste(
                    f"/sections/{abschnitt['gid']}/tasks",
                    {"completed_since": "now"},
                    max_eintraege=MAX_GELADEN,
                )
                eintraege.append(
                    {
                        "gid": abschnitt["gid"],
                        "name": abschnitt.get("name", ""),
                        "offene_aufgaben": f"mehr als {MAX_GELADEN}" if mehr else len(offene),
                    }
                )
        return begrenze(eintraege, weitere)


class AsanaNutzerSuchen(AsanaLeseTool):
    name = "asana_nutzer_suchen"
    beschreibung = (
        "Sucht Asana-Nutzer nach Name oder Teil der E-Mail-Adresse. Rückgabe: GID, Name, "
        "E-Mail. Nutze es, bevor du jemanden als Zuständigen setzt; nur ein eindeutiger "
        "Treffer darf verwendet werden."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "suche": {"type": "string", "description": "Name oder Teil der E-Mail-Adresse"}
        },
        "required": ["suche"],
    }

    async def ausfuehren(self, suche: str) -> dict:
        if not isinstance(suche, str) or not suche.strip():
            raise ToolFehler("Der Suchbegriff fehlt.")
        async with self.asana as asana:
            nutzer, weitere = await asana.liste(
                "/users",
                {"workspace": await asana.workspace_gid()},
                felder=("name", "email"),
                max_eintraege=MAX_GELADEN,
            )
        treffer = [
            {"gid": n["gid"], "name": n.get("name", ""), "email": n.get("email", "")}
            for n in nutzer
            if _passt(suche, n.get("name")) or _passt(suche, n.get("email"))
        ]
        return begrenze(treffer, weitere)


class AsanaTagsAnzeigen(AsanaLeseTool):
    name = "asana_tags_anzeigen"
    beschreibung = (
        "Listet die Tags des Asana-Workspaces, optional gefiltert nach Namen. Rückgabe: GID, Name."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "suche": {"type": "string", "description": "Teil des Tag-Namens (optional)"}
        },
    }

    async def ausfuehren(self, suche: str = "") -> dict:
        async with self.asana as asana:
            tags, weitere = await asana.liste(
                "/tags",
                {"workspace": await asana.workspace_gid()},
                felder=("name",),
                max_eintraege=MAX_GELADEN,
            )
        treffer = [
            {"gid": t["gid"], "name": t.get("name", "")}
            for t in tags
            if _passt(suche, t.get("name"))
        ]
        return begrenze(treffer, weitere)


def _passt(suche: str | None, wert: str | None) -> bool:
    return not (suche or "").strip() or (suche or "").strip().casefold() in (wert or "").casefold()


def _name(objekt: dict | None) -> str:
    return (objekt or {}).get("name", "")


def _gid_und_name(objekt: dict | None) -> dict | None:
    return {"gid": objekt["gid"], "name": objekt.get("name", "")} if objekt else None


def _datum(wert: str, feld: str) -> date | None:
    if not wert:
        return None
    try:
        return date.fromisoformat(wert)
    except (TypeError, ValueError):
        raise ToolFehler(f"„{feld}“ muss ein Datum im Format JJJJ-MM-TT sein.") from None


def _im_zeitraum(faellig: str | None, von: date | None, bis: date | None) -> bool:
    if von is None and bis is None:
        return True
    if not faellig:
        return False
    tag = date.fromisoformat(faellig)
    return (von is None or tag >= von) and (bis is None or tag <= bis)


def _aufgabe_kurz(aufgabe: dict) -> dict:
    zugehoerigkeiten = aufgabe.get("memberships") or []
    return {
        "gid": aufgabe["gid"],
        "name": aufgabe.get("name", ""),
        "faellig": aufgabe.get("due_on"),
        "faellig_um": aufgabe.get("due_at"),
        "zustaendig": _name(aufgabe.get("assignee")),
        "projekte": [
            {"projekt": _name(z.get("project")), "abschnitt": _name(z.get("section"))}
            for z in zugehoerigkeiten
        ],
        "erledigt": bool(aufgabe.get("completed")),
    }
