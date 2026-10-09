"""Tageslimit für versendete Mails je Person. Gezählt wird im Audit-Log (nur Metadaten)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.db.models import AuditLog
from app.db.session import db_sitzung
from app.tools.base import ToolFehler, ToolKontext

AKTION_GESENDET = "gesendet"
SENDE_TOOLS = ("mail_senden", "mail_antworten", "mail_weiterleiten")


async def gesendet_heute(
    kontext: ToolKontext, nutzer: object, jetzt: datetime | None = None
) -> int:
    """So viele Mails hat die Person heute (Ortszeit) über den Bot versendet."""
    zone = ZoneInfo(kontext.settings.tz)
    jetzt = jetzt or datetime.now(zone)
    tagesbeginn = jetzt.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(kontext.session_fabrik, nutzer_id) as session:
        return await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.user_id == nutzer_id,
                AuditLog.tool_name.in_(SENDE_TOOLS),
                AuditLog.ergebnis_kurz == AKTION_GESENDET,
                AuditLog.fehler.is_(None),
                AuditLog.zeit >= tagesbeginn,
            )
        )


async def pruefe_tageslimit(kontext: ToolKontext, nutzer: object) -> None:
    maximum = kontext.settings.mail_max_senden_pro_tag
    if await gesendet_heute(kontext, nutzer) >= maximum:
        raise ToolFehler(
            f"Das Tageslimit ist erreicht: Du hast heute schon {maximum} Mails über den Bot "
            "versendet. Morgen geht es weiter. Für Massenmails ist Klaviyo da."
        )
