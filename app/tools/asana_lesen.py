"""Asana-Lese-Tools (laufen ohne Freigabe)."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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
    "custom_fields.name",
    "custom_fields.display_value",
)
# Die Wiederholungsregel ist in der Asana-Doku nicht beschrieben. Lehnt Asana das Feld ab,
# wird es für den Rest der Laufzeit nicht mehr angefragt.
WIEDERHOLUNG_FELD = "recurrence"
# Für die kompakte Sammelansicht reichen wenige Felder.
_KOMPAKT_FELDER = (
    "name",
    "completed",
    "due_on",
    "assignee.name",
    "memberships.project.name",
    "memberships.section.name",
)
# Obergrenze der Sammelabfrage
MAX_SAMMEL = 300
SUCH_SEITE = 100
MAX_PROJEKTE_JE_SUCHE = 200
MAX_NAME_ZEICHEN = 60
MAX_PROJEKTNAME_ZEICHEN = 30


@dataclass(frozen=True)
class _Filter:
    text: str
    projekt: str
    abschnitt: str
    zustaendig: str
    von: date | None
    bis: date | None
    offen: bool


MAX_ANHAENGE_GEZAEHLT = 100
GLEICHZEITIGE_ZAEHLUNGEN = 5


class AsanaLeseTool(BasisTool):
    """Gemeinsame Basis; ohne `name` findet die Registry sie nicht als eigenes Tool."""

    def __init__(self, kontext: ToolKontext) -> None:
        super().__init__(kontext)
        self.asana = AsanaClient(kontext)
        self._wiederholung_lesbar = True

    async def _mit_wiederholung(self, abruf, felder: tuple[str, ...]):
        """Ruft `abruf(felder)` auf und fragt die Wiederholungsregel mit an, solange das geht."""
        if self._wiederholung_lesbar:
            try:
                return await abruf((*felder, WIEDERHOLUNG_FELD))
            except AsanaFehler as exc:
                if exc.status != 400:
                    raise
                self._wiederholung_lesbar = False
        return await abruf(felder)

    async def _zaehle_anhaenge(self, asana: AsanaClient, gid: str) -> int | str | None:
        """Anzahl der Anhänge; None, wenn sie sich nicht lesen lassen."""
        try:
            anhaenge, weitere = await asana.liste(
                "/attachments", {"parent": gid}, max_eintraege=MAX_ANHAENGE_GEZAEHLT
            )
        except AsanaFehler:
            return None
        return f"mehr als {MAX_ANHAENGE_GEZAEHLT}" if weitere else len(anhaenge)


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
        "Sucht Asana-Aufgaben über den ganzen Workspace oder eingegrenzt und holt dabei alle "
        "Seiten selbst (bis 300 Aufgaben). Filter: text, projekt, abschnitt, zustaendig (GID "
        "oder „ich“), faellig_von, faellig_bis (beide einschließlich), ueberfaellig (fällig vor "
        "heute und offen), nur_offen. Mindestens ein Filter ist nötig. Für Sammelaufgaben wie "
        "„alle überfälligen“ genügt EIN Aufruf mit ueberfaellig=true; frage nicht Projekt für "
        "Projekt ab. Rückgabe: gesamt (Anzahl der Treffer) und je Aufgabe eine kompakte Zeile "
        "„gid | fällig | projekt | name“. Mit details=true stattdessen höchstens 30 Aufgaben "
        "mit Zuständigem, Abschnitt, benutzerdefinierten Feldern, Anzahl der Anhänge und "
        "Wiederholungsregel."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text im Aufgabennamen"},
            "projekt": {"type": "string", "description": "GID des Projekts"},
            "abschnitt": {"type": "string", "description": "GID des Abschnitts"},
            "zustaendig": {
                "type": "string",
                "description": "GID des Zuständigen oder „ich“ für den Inhaber des Asana-Tokens",
            },
            "faellig_von": {"type": "string", "description": "Fällig ab, JJJJ-MM-TT (einschl.)"},
            "faellig_bis": {
                "type": "string",
                "description": "Stichtag: fällig am oder vor diesem Tag, JJJJ-MM-TT",
            },
            "ueberfaellig": {
                "type": "boolean",
                "description": "Nur offene Aufgaben, die vor heute fällig waren",
            },
            "nur_offen": {
                "type": "boolean",
                "description": "Nur nicht erledigte Aufgaben (Standard: true)",
            },
            "details": {
                "type": "boolean",
                "description": "Ausführliche Angaben statt kompakter Zeilen (höchstens 30)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SAMMEL,
                "description": "Maximale Anzahl (Standard: 300, mit details 30)",
            },
        },
    }
    # 300 kompakte Zeilen brauchen mehr Platz als das übliche Ergebnis.
    max_ergebnis_zeichen = 40000

    def __init__(self, kontext: ToolKontext) -> None:
        super().__init__(kontext)
        # Die Workspace-Suche gibt es nur in bezahlten Tarifen; nach dem ersten 402 wird sie
        # nicht mehr versucht.
        self._suche_verfuegbar = True

    def _heute(self) -> date:
        return datetime.now(ZoneInfo(self.kontext.settings.tz)).date()

    async def ausfuehren(
        self,
        text: str = "",
        projekt: str = "",
        abschnitt: str = "",
        zustaendig: str = "",
        faellig_von: str = "",
        faellig_bis: str = "",
        ueberfaellig: bool = False,
        nur_offen: bool | None = None,
        details: bool = False,
        limit: int | None = None,
        # Frühere Namen der Parameter bleiben gültig.
        projekt_gid: str = "",
        zustaendig_gid: str = "",
        nur_offene: bool | None = None,
    ) -> dict:
        text = (text or "").strip()
        projekt = projekt or projekt_gid
        projekt = pruefe_gid(projekt, "projekt") if projekt else ""
        abschnitt = pruefe_gid(abschnitt, "abschnitt") if abschnitt else ""
        zustaendig = str(zustaendig or zustaendig_gid).strip()
        if zustaendig.casefold() in ("ich", ICH):
            zustaendig = ICH
        elif zustaendig:
            zustaendig = pruefe_gid(zustaendig, "zustaendig")
        offen = next((w for w in (nur_offen, nur_offene) if w is not None), True)
        von = _datum(faellig_von, "faellig_von")
        bis = _datum(faellig_bis, "faellig_bis")
        if ueberfaellig:
            gestern = self._heute() - timedelta(days=1)
            bis = min(bis, gestern) if bis else gestern
            offen = True
        maximum = MAX_EINTRAEGE if details else MAX_SAMMEL
        limit = max(1, min(int(limit or maximum), maximum))
        if not (text or projekt or abschnitt or zustaendig or von or bis):
            raise ToolFehler("Bitte mindestens einen Filter angeben.")
        filter_ = _Filter(text, projekt, abschnitt, zustaendig, von, bis, offen)
        felder = _AUFGABEN_FELDER if details else _KOMPAKT_FELDER

        async with self.asana as asana:
            aufgaben = None
            if self._suche_verfuegbar:
                try:
                    aufgaben, weitere = await self._ueber_suche(asana, filter_, felder, details)
                except AsanaFehler as exc:
                    if exc.status != 402:
                        raise
                    self._suche_verfuegbar = False
            if aufgaben is None:
                aufgaben, weitere = await self._ueber_listen(asana, filter_, felder, details)

        # Alle Wege werden hier noch einmal gefiltert: Die Listen kennen die Filter gar nicht,
        # und die Suche wird bewusst etwas zu weit gefasst abgefragt.
        gefiltert = [
            a
            for a in aufgaben
            if _passt(text, a.get("name"))
            and not (offen and a.get("completed"))
            and _im_zeitraum(a.get("due_on"), von, bis)
            and (not abschnitt or abschnitt in _abschnitt_gids(a))
        ]
        gefiltert.sort(key=lambda a: (a.get("due_on") is None, a.get("due_on") or ""))
        if details:
            ergebnis = begrenze([_aufgabe_kurz(a) for a in gefiltert], weitere, maximum=limit)
            ergebnis["gesamt"] = len(gefiltert)
            await self._ergaenze_anhaenge(ergebnis["eintraege"])
            return ergebnis
        ergebnis = {
            "gesamt": len(gefiltert),
            "angezeigt": min(len(gefiltert), limit),
            "spalten": "gid | fällig | projekt | name",
            "aufgaben": [_aufgabe_zeile(a) for a in gefiltert[:limit]],
        }
        if bis:
            ergebnis["stichtag"] = f"fällig am oder vor dem {bis:%d.%m.%Y}"
        if weitere:
            ergebnis["hinweis"] = (
                f"Es wurden nicht alle Aufgaben geladen (Obergrenze {MAX_SAMMEL}); die Zahl in "
                "„gesamt“ ist deshalb eine Untergrenze. Bitte enger filtern."
            )
        elif len(gefiltert) > limit:
            ergebnis["hinweis"] = f"Angezeigt werden die ersten {limit}."
        return ergebnis

    async def _ergaenze_anhaenge(self, aufgaben: list[dict]) -> None:
        schranke = asyncio.Semaphore(GLEICHZEITIGE_ZAEHLUNGEN)

        async def zaehle(aufgabe: dict) -> None:
            async with schranke:
                anzahl = await self._zaehle_anhaenge(self.asana, aufgabe["gid"])
            if anzahl is not None:
                aufgabe["anhaenge"] = anzahl

        async with self.asana:
            await asyncio.gather(*(zaehle(aufgabe) for aufgabe in aufgaben))

    async def _lade(self, abruf, felder: tuple[str, ...], details: bool):
        """Die Wiederholungsregel wird nur für die ausführliche Ansicht mit angefragt."""
        return await self._mit_wiederholung(abruf, felder) if details else await abruf(felder)

    async def _ueber_suche(
        self, asana: AsanaClient, f: "_Filter", felder: tuple[str, ...], details: bool
    ) -> tuple[list[dict], bool]:
        """Die Suche kennt keine Seiten. Weitergeblättert wird über das Anlagedatum: Jede
        Abfrage holt die 100 jüngsten Aufgaben, die älter sind als die letzte der vorigen."""
        params = {
            "text": f.text or None,
            "projects.any": f.projekt or None,
            "sections.any": f.abschnitt or None,
            "assignee.any": f.zustaendig or None,
            # Einen Tag weiter gefasst, damit die Grenztage sicher enthalten sind.
            "due_on.after": (f.von - timedelta(days=1)).isoformat() if f.von else None,
            "due_on.before": (f.bis + timedelta(days=1)).isoformat() if f.bis else None,
            "completed": "false" if f.offen else None,
            "sort_by": "created_at",
            "sort_ascending": "false",
            "limit": SUCH_SEITE,
        }
        pfad = f"/workspaces/{await asana.workspace_gid()}/tasks/search"
        gefunden: dict[str, dict] = {}
        while True:
            seite = await self._lade(
                lambda fl, p=dict(params): asana.get(pfad, p, felder=(*fl, "created_at")),
                felder,
                details,
            )
            neue = [a for a in seite if a["gid"] not in gefunden]
            gefunden.update((a["gid"], a) for a in neue)
            if len(gefunden) > MAX_SAMMEL:
                return list(gefunden.values())[:MAX_SAMMEL], True
            aelteste = seite[-1].get("created_at") if seite else None
            if len(seite) < SUCH_SEITE or not neue or not aelteste:
                return list(gefunden.values()), False
            params["created_at.before"] = aelteste

    async def _ueber_listen(
        self, asana: AsanaClient, f: "_Filter", felder: tuple[str, ...], details: bool
    ) -> tuple[list[dict], bool]:
        """Ohne die Suche (nicht im Tarif): Listen je Abschnitt, Projekt oder Zuständigem; ohne
        solche Eingrenzung alle aktiven Projekte der Reihe nach."""
        offen = {"completed_since": "now"} if f.offen else {}

        async def liste(pfad: str, params: dict) -> tuple[list[dict], bool]:
            return await self._lade(
                lambda fl: asana.liste(pfad, params, felder=fl, max_eintraege=MAX_GELADEN),
                felder,
                details,
            )

        if f.abschnitt:
            aufgaben, weitere = await liste(f"/sections/{f.abschnitt}/tasks", dict(offen))
        elif f.projekt:
            aufgaben, weitere = await liste("/tasks", {"project": f.projekt, **offen})
        elif f.zustaendig:
            aufgaben, weitere = await liste(
                "/tasks",
                {"assignee": f.zustaendig, "workspace": await asana.workspace_gid(), **offen},
            )
            return aufgaben, weitere
        elif not f.offen:
            raise ToolFehler(
                "Die Suche über den ganzen Workspace ist im aktuellen Asana-Tarif nicht "
                "enthalten. Erledigte Aufgaben lassen sich deshalb nur mit projekt, abschnitt "
                "oder zustaendig durchsuchen."
            )
        else:
            projekte, weitere = await asana.liste(
                "/projects",
                {"workspace": await asana.workspace_gid(), "archived": "false"},
                max_eintraege=MAX_PROJEKTE_JE_SUCHE,
            )
            gefunden: dict[str, dict] = {}
            for projekt in projekte:
                teil, mehr = await liste("/tasks", {"project": projekt["gid"], **offen})
                weitere = weitere or mehr
                gefunden.update((a["gid"], a) for a in teil)
            aufgaben = list(gefunden.values())
        if f.zustaendig:
            ich = (await asana.get("/users/me"))["gid"] if f.zustaendig == ICH else f.zustaendig
            aufgaben = [a for a in aufgaben if (a.get("assignee") or {}).get("gid") == ich]
        return aufgaben, weitere


class AsanaAufgabeDetails(AsanaLeseTool):
    name = "asana_aufgabe_details"
    beschreibung = (
        "Liefert alle Angaben zu einer Asana-Aufgabe: Felder, benutzerdefinierte Felder, "
        "Unteraufgaben, Tags, Abhängigkeiten, Anzahl der Anhänge, Wiederholungsregel und die "
        "letzten 10 Kommentare (mit GID). Nutze es vor jeder Änderung an einer Aufgabe, um den "
        "aktuellen Stand zu kennen."
    )
    parameter_schema = {
        "type": "object",
        "properties": {"aufgabe_gid": {"type": "string", "description": "GID der Aufgabe"}},
        "required": ["aufgabe_gid"],
    }

    async def ausfuehren(self, aufgabe_gid: str) -> dict:
        gid = pruefe_gid(aufgabe_gid, "aufgabe_gid")
        async with self.asana as asana:
            aufgabe = await self._mit_wiederholung(
                lambda felder: asana.get(f"/tasks/{gid}", felder=felder),
                (
                    *_AUFGABEN_FELDER,
                    "custom_fields.resource_subtype",
                    "notes",
                    "start_on",
                    "start_at",
                    "resource_subtype",
                    "approval_status",
                    "completed_at",
                    "created_at",
                    "modified_at",
                    "parent.name",
                    "tags.name",
                    "followers.name",
                    "permalink_url",
                ),
            )
            anhaenge = await self._zaehle_anhaenge(asana, gid)
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
            "startzeit": aufgabe.get("start_at"),
            "meilenstein": aufgabe.get("resource_subtype") == "milestone",
            "aufgabentyp": aufgabe.get("resource_subtype"),
            "genehmigung": aufgabe.get("approval_status"),
            "anhaenge": anhaenge,
            "benutzerfelder": [
                {
                    "gid": f["gid"],
                    "name": f.get("name", ""),
                    "typ": f.get("resource_subtype"),
                    "wert": f.get("display_value"),
                }
                for f in aufgabe.get("custom_fields") or []
            ],
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
                    "gid": k.get("gid"),
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


def _abschnitt_gids(aufgabe: dict) -> set[str]:
    return {(z.get("section") or {}).get("gid", "") for z in aufgabe.get("memberships") or []}


def _aufgabe_zeile(aufgabe: dict) -> str:
    """Eine Aufgabe als kompakte Zeile „gid | fällig | projekt | name“."""
    projekte = [_name(z.get("project")) for z in aufgabe.get("memberships") or []]
    projekt = kuerze_text(next((p for p in projekte if p), ""), MAX_PROJEKTNAME_ZEICHEN) or "-"
    name = kuerze_text(aufgabe.get("name"), MAX_NAME_ZEICHEN).replace("\n", " ")
    return f"{aufgabe['gid']} | {aufgabe.get('due_on') or '-'} | {projekt} | {name}"


def _aufgabe_kurz(aufgabe: dict) -> dict:
    zugehoerigkeiten = aufgabe.get("memberships") or []
    kurz = _aufgabe_grunddaten(aufgabe, zugehoerigkeiten)
    # Nur gefüllte Felder, damit die Liste kurz bleibt.
    felder = {
        f.get("name", ""): f["display_value"]
        for f in aufgabe.get("custom_fields") or []
        if f.get("display_value") not in (None, "")
    }
    if felder:
        kurz["felder"] = felder
    if aufgabe.get(WIEDERHOLUNG_FELD):
        kurz["wiederholung"] = aufgabe[WIEDERHOLUNG_FELD]
    return kurz


def _aufgabe_grunddaten(aufgabe: dict, zugehoerigkeiten: list[dict]) -> dict:
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
