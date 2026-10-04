"""Audit-Log."""

from typing import Any

from app.db.models import AuditLog
from app.db.session import SessionFabrik

EREIGNIS_UNBEKANNT = "unbekannt"


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
                parameter=parameter,
                ergebnis_kurz=ergebnis_kurz,
                dauer_ms=dauer_ms,
                fehler=fehler,
            )
        )
        await session.commit()
