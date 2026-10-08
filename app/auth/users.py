"""Nutzer und Rollen. Die Datenbank ist die Wahrheit; die .env dient nur der Übernahme."""

from sqlalchemy import select

from app.config import Settings
from app.db.models import (
    ROLLE_ADMIN,
    ROLLE_MITARBEITER,
    SystemEinstellung,
    User,
    UserRole,
)
from app.db.session import SessionFabrik

MERKER_BESTAND = "bestand_uebernommen"


async def uebernehme_bestand(session_fabrik: SessionFabrik, settings: Settings) -> None:
    """Übernimmt die Whitelist aus der .env in die Datenbank. Mehrfaches Ausführen schadet nicht.

    Beim ersten Mal werden alle erlaubten IDs angelegt (Admin-IDs als `admin`, alle anderen als
    `mitarbeiter`). Danach legt die Funktion nur noch fehlende Admins an: Bestehende Personen,
    ihre Rollen und Sperren werden nie wieder aus der .env überschrieben.
    """
    async with session_fabrik() as session:
        vorhandene = set(await session.scalars(select(User.telegram_id)))
        erstmals = await session.get(SystemEinstellung, MERKER_BESTAND) is None
        neue = {tid: ROLLE_ADMIN for tid in settings.telegram_admin_user_ids}
        if erstmals:
            for telegram_id in settings.telegram_allowed_user_ids:
                neue.setdefault(telegram_id, ROLLE_MITARBEITER)
            session.add(SystemEinstellung(schluessel=MERKER_BESTAND, wert="ja"))
        for telegram_id, rolle in sorted(neue.items()):
            if telegram_id in vorhandene:
                continue
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.flush()
            session.add(UserRole(user_id=user.id, role_name=rolle))
        await session.commit()


async def rollen_von(session_fabrik: SessionFabrik, user_id: int) -> frozenset[str]:
    async with session_fabrik() as session:
        return frozenset(
            await session.scalars(select(UserRole.role_name).where(UserRole.user_id == user_id))
        )


async def finde_erlaubten_nutzer(
    session_fabrik: SessionFabrik, telegram_id: int, name: str = ""
) -> User | None:
    """Liefert die aktive Person zur Telegram-ID oder None, wenn sie nicht bedient wird."""
    async with session_fabrik() as session:
        user = await session.scalar(
            select(User).where(
                User.telegram_id == telegram_id, User.aktiv.is_(True), User.gesperrt_am.is_(None)
            )
        )
        # Der Name aus Telegram ist nur der Startwert; danach gilt, was im Profil steht.
        if user is not None and name and not user.anzeigename:
            user.anzeigename = name[:200]
            await session.commit()
    if user is not None:
        rollen = await rollen_von(session_fabrik, user.id)
        user.rolle = "admin" if ROLLE_ADMIN in rollen else "user"
    return user


async def aktive_admins(session_fabrik: SessionFabrik) -> list[User]:
    async with session_fabrik() as session:
        ergebnis = await session.scalars(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .where(
                UserRole.role_name == ROLLE_ADMIN,
                User.aktiv.is_(True),
                User.gesperrt_am.is_(None),
            )
        )
        return list(ergebnis)
