"""Findet und registriert Tools automatisch und führt sie mit Audit-Log aus."""

import base64
import importlib
import inspect
import json
import logging
import pkgutil
import time
from dataclasses import dataclass
from types import ModuleType

import app.tools
from app.db.models import User
from app.medien import PDF
from app.observability.audit import protokolliere
from app.tools.base import (
    ANSICHT_SCHLUESSEL,
    Ansicht,
    BasisTool,
    Tool,
    ToolFehler,
    ToolKontext,
    aktueller_nutzer,
)

log = logging.getLogger(__name__)

MAX_ERGEBNIS_ZEICHEN = 8000
GEKUERZT_MARKE = "… [gekürzt]"


@dataclass(frozen=True)
class ToolErgebnis:
    text: str
    fehler: bool = False
    # Das ungekürzte Ergebnis des Tools; None bei Fehlern
    daten: dict | None = None
    # Zusätzliche Inhaltsblöcke für Claude (Bild oder PDF), nie Teil von Log oder Verlauf
    bloecke: tuple[dict, ...] = ()


class Registry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in sorted(tools, key=lambda t: t.name)}
        if len(self._tools) != len(tools):
            raise ValueError("Tool-Namen müssen eindeutig sein")

    def alle(self) -> list[Tool]:
        return list(self._tools.values())

    def hole(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def api_definitionen(self, rolle: str) -> list[dict]:
        """Tool-Liste für Claude; enthält nur, was die Rolle nutzen darf."""
        return [
            {
                "name": tool.name,
                "description": tool.beschreibung,
                "input_schema": tool.parameter_schema,
            }
            for tool in self._tools.values()
            if rolle in tool.erlaubte_rollen
        ]


def lade_registry(kontext: ToolKontext, paket: ModuleType = app.tools) -> Registry:
    """Importiert alle Module des Pakets und instanziiert jede dort definierte Tool-Klasse."""
    tools: list[Tool] = []
    for modul_info in pkgutil.iter_modules(paket.__path__):
        modul = importlib.import_module(f"{paket.__name__}.{modul_info.name}")
        for _, klasse in inspect.getmembers(modul, inspect.isclass):
            if (
                issubclass(klasse, BasisTool)
                and klasse is not BasisTool
                and klasse.__module__ == modul.__name__
                # Gemeinsame Basisklassen ohne eigenen Namen sind keine Tools.
                and getattr(klasse, "name", None)
            ):
                tools.append(klasse(kontext))
    return Registry(tools)


def _ansicht_block(ansicht: Ansicht) -> dict:
    return {
        "type": "document" if ansicht.medientyp == PDF else "image",
        "source": {
            "type": "base64",
            "media_type": ansicht.medientyp,
            "data": base64.standard_b64encode(ansicht.daten).decode("ascii"),
        },
    }


def kuerze(text: str, max_zeichen: int = MAX_ERGEBNIS_ZEICHEN) -> str:
    if len(text) <= max_zeichen:
        return text
    return text[: max_zeichen - len(GEKUERZT_MARKE)] + GEKUERZT_MARKE


async def fuehre_tool_aus(
    tool: Tool, params: dict, user: User, kontext: ToolKontext
) -> ToolErgebnis:
    """Führt das Tool aus. Fehler werden zu einem Fehlertext, nie zu einer Ausnahme."""
    start = time.monotonic()
    marke = aktueller_nutzer.set(user)
    try:
        daten = await tool.ausfuehren(**params)
        # Ein Tool kann Claude eine Datei zum Ansehen mitgeben. Sie geht als eigener Block an
        # Claude und taucht weder im Text noch im Audit-Log auf.
        ansicht = daten.pop(ANSICHT_SCHLUESSEL, None) if isinstance(daten, dict) else None
        ergebnis = ToolErgebnis(
            kuerze(
                json.dumps(daten, ensure_ascii=False, default=str),
                getattr(tool, "max_ergebnis_zeichen", None) or MAX_ERGEBNIS_ZEICHEN,
            ),
            daten=daten,
            bloecke=(_ansicht_block(ansicht),) if ansicht else (),
        )
        fehler = None
    except ToolFehler as exc:
        ergebnis = ToolErgebnis(f"Fehler: {exc}", fehler=True)
        fehler = str(exc)
    except Exception as exc:
        log.exception("Unerwarteter Fehler im Tool %s", tool.name)
        ergebnis = ToolErgebnis(f"Interner Fehler im Tool {tool.name}.", fehler=True)
        fehler = type(exc).__name__
    finally:
        aktueller_nutzer.reset(marke)
    await protokolliere(
        kontext.session_fabrik,
        user_id=user.id,
        tool_name=tool.name,
        parameter=params,
        ergebnis_kurz="" if fehler else ergebnis.text,
        dauer_ms=int((time.monotonic() - start) * 1000),
        fehler=fehler,
    )
    return ergebnis
