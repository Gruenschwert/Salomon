"""Gesprächsverlauf pro Chat.

Gespeichert wird je Runde nur der Text von Nutzer und Assistent, keine Tool-Blöcke. So bleibt
der Verlauf auch nach dem Kürzen auf `HISTORY_MAX_MESSAGES` immer gültig. Jede Person hat ihren
eigenen Verlauf; gelesen und geschrieben wird nur über `db_sitzung`.
"""

from sqlalchemy import select

from app.db.models import Message
from app.db.session import SessionFabrik, db_sitzung

ROLLE_NUTZER = "user"
ROLLE_ASSISTENT = "assistant"
MAX_HINWEIS_ZEICHEN = 3000


async def lade_verlauf(
    session_fabrik: SessionFabrik, nutzer: object, chat_id: int, max_nachrichten: int
) -> list[dict]:
    """Liefert die letzten eigenen Nachrichten dieser Person in diesem Chat im Format der
    Messages-API, älteste zuerst. Fremde Nachrichten lässt schon die Datenbank nicht durch."""
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        neueste = await session.scalars(
            select(Message)
            .where(Message.chat_id == chat_id, Message.user_id == nutzer_id)
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
    async with db_sitzung(session_fabrik, user_id) as session:
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
    async with db_sitzung(session_fabrik, user_id) as session:
        session.add(
            Message(
                chat_id=chat_id,
                user_id=user_id,
                rolle=ROLLE_ASSISTENT,
                inhalt=f"[Ergebnis der Freigabe]\n{text[:MAX_HINWEIS_ZEICHEN]}",
            )
        )
        await session.commit()
