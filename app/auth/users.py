"""Whitelist und Rollen."""

from sqlalchemy import select

from app.config import Settings
from app.db.models import ROLLE_ADMIN, ROLLE_USER, User
from app.db.session import SessionFabrik


async def synchronisiere_whitelist(session_fabrik: SessionFabrik, settings: Settings) -> None:
    """Gleicht `users` mit der Whitelist aus `.env` ab. Entfernte IDs werden deaktiviert."""
    async with session_fabrik() as session:
        vorhandene = {user.telegram_id: user for user in (await session.scalars(select(User)))}
        for telegram_id in settings.telegram_allowed_user_ids:
            rolle = ROLLE_ADMIN if telegram_id in settings.telegram_admin_user_ids else ROLLE_USER
            user = vorhandene.get(telegram_id)
            if user is None:
                session.add(User(telegram_id=telegram_id, rolle=rolle, aktiv=True))
            else:
                user.rolle = rolle
                user.aktiv = True
        for telegram_id, user in vorhandene.items():
            if telegram_id not in settings.telegram_allowed_user_ids:
                user.aktiv = False
        await session.commit()


async def finde_erlaubten_nutzer(
    session_fabrik: SessionFabrik, telegram_id: int, name: str = ""
) -> User | None:
    """Liefert den aktiven Nutzer zur Telegram-ID oder None, wenn er nicht bedient wird."""
    async with session_fabrik() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id, User.aktiv.is_(True))
        )
        if user is not None and name and user.name != name:
            user.name = name
            await session.commit()
        return user


async def aktive_admins(session_fabrik: SessionFabrik) -> list[User]:
    async with session_fabrik() as session:
        ergebnis = await session.scalars(
            select(User).where(User.rolle == ROLLE_ADMIN, User.aktiv.is_(True))
        )
        return list(ergebnis)
