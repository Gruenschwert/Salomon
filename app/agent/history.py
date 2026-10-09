"""Gesprächsverlauf pro Chat.

Gespeichert wird je Runde nur der Text von Nutzer und Assistent, keine Tool-Blöcke. So bleibt
der Verlauf auch nach dem Kürzen auf `HISTORY_MAX_MESSAGES` immer gültig. Jede Person hat ihren
eigenen Verlauf; gelesen und geschrieben wird nur über `db_sitzung`.

Mailinhalt ist die Ausnahme: Er wird nie im Klartext abgelegt. Hat eine Runde Mails gelesen
oder eine Mail vorbereitet, liegen die Tool-Ergebnisse und die Antwort mit dem Schlüssel der
Person verschlüsselt in `inhalt_verschluesselt` und gelten nur `MAIL_KONTEXT_TTL_STUNDEN`.
Danach steht dort nur noch ein Platzhalter.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from app.auth.tresor import Tresor, TresorFehler
from app.db.models import Message, jetzt
from app.db.session import SessionFabrik, db_sitzung
from app.mail.schutz import KONTEXT_KOPF, PLATZHALTER_ENTFERNT, PLATZHALTER_VERSCHLUESSELT

log = logging.getLogger(__name__)

ROLLE_NUTZER = "user"
ROLLE_ASSISTENT = "assistant"
MAX_HINWEIS_ZEICHEN = 3000
ZWECK_VERLAUF = "verlauf"


@dataclass(frozen=True)
class MailAblage:
    """Was von einer Runde mit Mailinhalt verschlüsselt in den Verlauf kommt."""

    # None: Es gibt keinen Schlüssel; dann wird nur der Platzhalter abgelegt.
    tresor: Tresor | None
    # Die Tool-Ergebnisse mit Mailinhalt, bereits als nicht vertrauenswürdig umrandet
    kontext: str = ""


@dataclass(frozen=True)
class Verlauf:
    nachrichten: list[dict]
    # True, wenn im Verlauf noch lesbarer Mailinhalt aus einer früheren Runde liegt
    hat_mail: bool = False
    # Was die Person in diesen Nachrichten selbst geschrieben hat (ohne Mailinhalt)
    nutzer_text: str = ""


def _entschluessle(
    nachricht: Message, nutzer_id: int, tresor: Tresor | None, grenze: datetime | None
) -> str | None:
    """Der verschlüsselte Inhalt einer Zeile; None, wenn er abgelaufen oder nicht lesbar ist."""
    if tresor is None or (grenze is not None and nachricht.zeit < grenze):
        return None
    try:
        return tresor.entsiegle(nutzer_id, ZWECK_VERLAUF, nachricht.inhalt_verschluesselt)
    except TresorFehler:
        log.warning("Verschlüsselter Mailinhalt im Verlauf ist nicht lesbar")
        return None


async def lade_verlauf_mit_mail(
    session_fabrik: SessionFabrik,
    nutzer: object,
    chat_id: int,
    max_nachrichten: int,
    tresor: Tresor | None = None,
    ttl_stunden: int | None = None,
    zeitpunkt: datetime | None = None,
) -> Verlauf:
    """Die letzten eigenen Nachrichten dieser Person in diesem Chat im Format der
    Messages-API, älteste zuerst. Fremde Nachrichten lässt schon die Datenbank nicht durch.

    Verschlüsselter Mailinhalt wird nur mit `tresor` und nur innerhalb der Frist lesbar;
    sonst steht an seiner Stelle der Platzhalter.
    """
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
    while nachrichten and (
        nachrichten[0].rolle != ROLLE_NUTZER or nachrichten[0].inhalt_verschluesselt is not None
    ):
        nachrichten.pop(0)
    grenze = None
    if ttl_stunden is not None:
        grenze = (zeitpunkt or jetzt()) - timedelta(hours=ttl_stunden)
    ergebnis: list[dict] = []
    hat_mail = False
    eigene: list[str] = []
    for nachricht in nachrichten:
        inhalt = nachricht.inhalt
        if nachricht.inhalt_verschluesselt is not None:
            klartext = _entschluessle(nachricht, nutzer_id, tresor, grenze)
            if klartext is None:
                inhalt = PLATZHALTER_ENTFERNT
            else:
                hat_mail = True
                inhalt = (
                    f"{KONTEXT_KOPF}\n{klartext}" if nachricht.rolle == ROLLE_NUTZER else klartext
                )
        elif nachricht.rolle == ROLLE_NUTZER and isinstance(inhalt, str):
            eigene.append(inhalt)
        # Aufeinanderfolgende Zeilen derselben Rolle (Frage plus Mailinhalt) werden eine.
        if ergebnis and ergebnis[-1]["role"] == nachricht.rolle and isinstance(inhalt, str):
            ergebnis[-1]["content"] = f"{ergebnis[-1]['content']}\n\n{inhalt}"
        else:
            ergebnis.append({"role": nachricht.rolle, "content": inhalt})
    return Verlauf(ergebnis, hat_mail, " ".join(eigene))


async def lade_verlauf(
    session_fabrik: SessionFabrik, nutzer: object, chat_id: int, max_nachrichten: int
) -> list[dict]:
    """Der Verlauf ohne Schlüssel: Verschlüsselter Mailinhalt erscheint nur als Platzhalter."""
    verlauf = await lade_verlauf_mit_mail(session_fabrik, nutzer, chat_id, max_nachrichten)
    return verlauf.nachrichten


def _versiegelt(mail: MailAblage, user_id: int, text: str) -> dict:
    """Spaltenwerte für eine Zeile mit Mailinhalt: nie Klartext."""
    if mail.tresor is None:
        return {"inhalt": PLATZHALTER_ENTFERNT}
    return {
        "inhalt": PLATZHALTER_VERSCHLUESSELT,
        "inhalt_verschluesselt": mail.tresor.versiegle(user_id, ZWECK_VERLAUF, text),
    }


async def speichere_austausch(
    session_fabrik: SessionFabrik,
    chat_id: int,
    user_id: int,
    frage: str,
    antwort: str,
    mail: MailAblage | None = None,
) -> None:
    """Hält Frage und Antwort fest. Mit `mail` enthält die Runde Mailinhalt: Dann werden die
    Tool-Ergebnisse und die Antwort nur verschlüsselt abgelegt."""
    async with db_sitzung(session_fabrik, user_id) as session:
        session.add(Message(chat_id=chat_id, user_id=user_id, rolle=ROLLE_NUTZER, inhalt=frage))
        await session.flush()
        if mail is not None and mail.kontext:
            session.add(
                Message(
                    chat_id=chat_id,
                    user_id=user_id,
                    rolle=ROLLE_NUTZER,
                    **_versiegelt(mail, user_id, mail.kontext),
                )
            )
            await session.flush()
        werte = {"inhalt": antwort} if mail is None else _versiegelt(mail, user_id, antwort)
        session.add(Message(chat_id=chat_id, user_id=user_id, rolle=ROLLE_ASSISTENT, **werte))
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
