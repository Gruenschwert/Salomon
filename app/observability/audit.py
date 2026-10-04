"""Audit-Log."""

import re
from typing import Any

from app.db.models import AuditLog
from app.db.session import SessionFabrik

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
    async with session_fabrik() as session:
        session.add(
            AuditLog(
                user_id=user_id,
                tool_name=tool_name,
                parameter=bereinige(parameter),
                ergebnis_kurz=ergebnis_kurz[:MAX_ERGEBNIS_ZEICHEN],
                dauer_ms=dauer_ms,
                fehler=fehler,
            )
        )
        await session.commit()
