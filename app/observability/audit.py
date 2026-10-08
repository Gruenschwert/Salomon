"""Audit-Log."""

import re
from typing import Any

from sqlalchemy import insert

from app.db.models import AuditLog, jetzt
from app.db.session import SessionFabrik, db_sitzung

EREIGNIS_UNBEKANNT = "unbekannt"

MAX_WERT_ZEICHEN = 300
MAX_ERGEBNIS_ZEICHEN = 200
MASKIERT = "***"
_GEHEIM_SCHLUESSEL = re.compile(r"token|passwor|secret|api_?key|authorization", re.IGNORECASE)


def bereinige(wert: Any, schluessel: str = "") -> Any:
    """Maskiert Secrets und kürzt lange Texte, damit das Log nur das Nötige enthält."""
    if schluessel and _GEHEIM_SCHLUESSEL.search(schluessel):
        return MASKIERT
    if isinstance(wert, dict):
        return {str(k): bereinige(v, str(k)) for k, v in wert.items()}
    if isinstance(wert, list):
        return [bereinige(v) for v in wert]
    if isinstance(wert, str) and len(wert) > MAX_WERT_ZEICHEN:
        return wert[:MAX_WERT_ZEICHEN] + "…"
    return wert


async def protokolliere(
    session_fabrik: SessionFabrik,
    *,
    user_id: int | None,
    tool_name: str,
    parameter: dict[str, Any],
    ergebnis_kurz: str = "",
    dauer_ms: int = 0,
    fehler: str | None = None,
) -> None:
    # Einträge ohne Person (unbekannte Telegram-Nutzer) lassen sich nur schreiben, nie lesen.
    sitzung = db_sitzung(session_fabrik, user_id) if user_id is not None else session_fabrik()
    async with sitzung as session:
        # Bewusst ohne RETURNING: Einen Eintrag ohne Person darf die Laufzeitrolle schreiben,
        # aber nicht lesen, und RETURNING zählt für die Datenbank als Lesen.
        await session.execute(
            insert(AuditLog)
            .inline()
            .values(
                zeit=jetzt(),
                user_id=user_id,
                tool_name=tool_name,
                parameter=bereinige(parameter),
                ergebnis_kurz=ergebnis_kurz[:MAX_ERGEBNIS_ZEICHEN],
                dauer_ms=dauer_ms,
                fehler=fehler,
            )
        )
        await session.commit()
