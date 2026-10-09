"""Persönliches Gedächtnis: Notizen einer Person, Löschen des eigenen Verlaufs, Aufbewahrung.

Alles hier läuft über `db_sitzung` und betrifft immer nur die Daten der Person selbst.
"""

import logging
from datetime import timedelta

from sqlalchemy import delete, select, update

from app.auth.users import alle_nutzer_ids
from app.db.models import (
    STATUS_ABGELAUFEN,
    STATUS_ABGELEHNT,
    STATUS_BESTAETIGUNG,
    STATUS_OFFEN,
    Approval,
    Message,
    TelegramDatei,
    UserMemory,
    jetzt,
)
from app.db.session import SessionFabrik, db_sitzung
from app.mail.schutz import ENTFERNT, PLATZHALTER_ENTFERNT, VERSIEGELT

log = logging.getLogger(__name__)

MAX_NOTIZ_ZEICHEN = 500
MAX_NOTIZEN = 50


class GedaechtnisFehler(Exception):
    """Erwartbarer Fehler; die Meldung geht an die Person."""


async def merke(session_fabrik: SessionFabrik, nutzer: object, text: str) -> int:
    """Speichert eine persönliche Notiz. Liefert die Anzahl der Notizen danach."""
    text = text.strip()
    if not text:
        raise GedaechtnisFehler("So geht es: /merken <was ich mir merken soll>")
    if len(text) > MAX_NOTIZ_ZEICHEN:
        raise GedaechtnisFehler(f"Eine Notiz darf höchstens {MAX_NOTIZ_ZEICHEN} Zeichen haben.")
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        vorhandene = list(await session.scalars(select(UserMemory.id)))
        if len(vorhandene) >= MAX_NOTIZEN:
            raise GedaechtnisFehler(
                f"Du hast schon {MAX_NOTIZEN} Notizen. Mit /vergessen räumst du auf."
            )
        session.add(UserMemory(user_id=nutzer_id, inhalt=text))
        await session.commit()
    return len(vorhandene) + 1


async def lade_notizen(session_fabrik: SessionFabrik, nutzer: object) -> list[str]:
    """Die eigenen Notizen, älteste zuerst. Fremde lässt schon die Datenbank nicht durch."""
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        return list(
            await session.scalars(
                select(UserMemory.inhalt)
                .where(UserMemory.user_id == nutzer_id)
                .order_by(UserMemory.id)
            )
        )


async def vergiss_alles(session_fabrik: SessionFabrik, nutzer: object) -> tuple[int, int]:
    """Löscht den eigenen Gesprächsverlauf und alle eigenen Notizen. Liefert die Anzahlen."""
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        nachrichten = await session.execute(delete(Message).where(Message.user_id == nutzer_id))
        notizen = await session.execute(delete(UserMemory).where(UserMemory.user_id == nutzer_id))
        # Auch vorbereitete Mails, die noch auf eine Freigabe warten, verschwinden sofort.
        await _leere_versiegelte_freigaben(session, nutzer_id, STATUS_ABGELEHNT)
        await session.commit()
    return nachrichten.rowcount, notizen.rowcount


async def _leere_versiegelte_freigaben(
    session, nutzer_id: int, status: str, aelter_als=None
) -> int:
    """Entfernt den versiegelten Inhalt offener Freigaben (Mail) und schließt sie ab."""
    bedingungen = [
        Approval.user_id == nutzer_id,
        Approval.status.in_([STATUS_OFFEN, STATUS_BESTAETIGUNG]),
    ]
    if aelter_als is not None:
        bedingungen.append(Approval.erstellt_am < aelter_als)
    offene = await session.scalars(select(Approval).where(*bedingungen))
    betroffen = [
        f.id for f in offene if isinstance(f.parameter, dict) and VERSIEGELT in f.parameter
    ]
    if betroffen:
        await session.execute(
            update(Approval)
            .where(Approval.id.in_(betroffen))
            .values(parameter=ENTFERNT, status=status, entschieden_am=jetzt())
        )
    return len(betroffen)


async def bereinige_mailinhalt(
    session_fabrik: SessionFabrik, ttl_stunden: int, freigabe_minuten: int = 15
) -> int:
    """Ersetzt Mailinhalt im Verlauf, der älter ist als die Frist, durch den Platzhalter und
    leert abgelaufene, nie entschiedene Mail-Freigaben. Liefert die Zahl der ersetzten Zeilen.

    Geht Person für Person vor und fasst jeweils nur deren eigene Zeilen an. Gelesen wird
    dabei nichts; ins Log kommt nur die Anzahl.
    """
    grenze = jetzt() - timedelta(hours=max(ttl_stunden, 0))
    freigabe_grenze = jetzt() - timedelta(minutes=freigabe_minuten)
    ersetzt = 0
    for nutzer_id in await alle_nutzer_ids(session_fabrik):
        async with db_sitzung(session_fabrik, nutzer_id) as session:
            ergebnis = await session.execute(
                update(Message)
                .where(
                    Message.user_id == nutzer_id,
                    Message.inhalt_verschluesselt.is_not(None),
                    Message.zeit < grenze,
                )
                .values(inhalt=PLATZHALTER_ENTFERNT, inhalt_verschluesselt=None)
            )
            await _leere_versiegelte_freigaben(
                session, nutzer_id, STATUS_ABGELAUFEN, aelter_als=freigabe_grenze
            )
            await session.commit()
        ersetzt += ergebnis.rowcount
    return ersetzt


async def bereinige_alte_nachrichten(session_fabrik: SessionFabrik, tage: int) -> int:
    """Löscht Nachrichten und Dateiverweise, die älter sind als die Aufbewahrungsfrist.

    Geht Person für Person vor und löscht jeweils nur deren eigene abgelaufene Zeilen. Dabei
    wird nichts gelesen, nur gelöscht; ins Log kommt nur die Anzahl.
    """
    if tage <= 0:
        return 0
    grenze = jetzt() - timedelta(days=tage)
    geloescht = 0
    for nutzer_id in await alle_nutzer_ids(session_fabrik):
        async with db_sitzung(session_fabrik, nutzer_id) as session:
            ergebnis = await session.execute(
                delete(Message).where(Message.user_id == nutzer_id, Message.zeit < grenze)
            )
            await session.execute(
                delete(TelegramDatei).where(
                    TelegramDatei.user_id == nutzer_id, TelegramDatei.erstellt_am < grenze
                )
            )
            await session.commit()
        geloescht += ergebnis.rowcount
    return geloescht
