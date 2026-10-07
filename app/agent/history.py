"""Gesprächsverlauf pro Chat.

Gespeichert wird je Runde nur der Text von Nutzer und Assistent, keine Tool-Blöcke. So bleibt
der Verlauf auch nach dem Kürzen auf `HISTORY_MAX_MESSAGES` immer gültig.
"""

from sqlalchemy import select

from app.db.models import Message
from app.db.session import SessionFabrik

ROLLE_NUTZER = "user"
ROLLE_ASSISTENT = "assistant"
MAX_HINWEIS_ZEICHEN = 3000


async def lade_verlauf(
    session_fabrik: SessionFabrik, chat_id: int, max_nachrichten: int
) -> list[dict]:
    """Liefert die letzten Nachrichten des Chats im Format der Messages-API, älteste zuerst."""
    async with session_fabrik() as session:
        neueste = await session.scalars(
            select(Message)
            .where(Message.chat_id == chat_id)
            .order_by(Message.id.desc())
            .limit(max_nachrichten)
        )
        nachrichten = list(reversed(list(neueste)))
    # Die API verlangt, dass der Verlauf mit einer Nutzer-Nachricht beginnt.
    while nachrichten and nachrichten[0].rolle != ROLLE_NUTZER:
        nachrichten.pop(0)
    return [{"role": n.rolle, "content": n.inhalt} for n in nachrichten]


async def speichere_austausch(
    session_fabrik: SessionFabrik, chat_id: int, user_id: int, frage: str, antwort: str
) -> None:
    async with session_fabrik() as session:
        session.add(Message(chat_id=chat_id, user_id=user_id, rolle=ROLLE_NUTZER, inhalt=frage))
        await session.flush()
        session.add(
            Message(chat_id=chat_id, user_id=user_id, rolle=ROLLE_ASSISTENT, inhalt=antwort)
        )
        await session.commit()


async def speichere_hinweis(
    session_fabrik: SessionFabrik, chat_id: int, user_id: int, text: str
) -> None:
    """Hält fest, was nach einer Freigabe passiert ist, damit Claude es beim nächsten Mal weiß."""
    async with session_fabrik() as session:
        session.add(
            Message(
                chat_id=chat_id,
                user_id=user_id,
                rolle=ROLLE_ASSISTENT,
                inhalt=f"[Ergebnis der Freigabe]\n{text[:MAX_HINWEIS_ZEICHEN]}",
            )
        )
        await session.commit()
