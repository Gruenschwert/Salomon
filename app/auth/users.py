"""Nutzer und Rollen. Die Datenbank ist die Wahrheit; die .env dient nur der Übernahme."""

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from sqlalchemy import func, select

from app.auth.kontext import NutzerKontext
from app.auth.rechte import rechte_von
from app.config import Settings
from app.db.models import (
    ROLLE_ADMIN,
    ROLLE_MITARBEITER,
    ROLLEN,
    SystemEinstellung,
    User,
    UserRole,
    jetzt,
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
) -> NutzerKontext | None:
    """Baut den NutzerKontext zur Telegram-ID oder liefert None, wenn die Person nicht bedient
    wird (unbekannt, inaktiv oder gesperrt). Die Identität kommt allein aus der Datenbank."""
    async with session_fabrik() as session:
        user = await session.scalar(
            select(User).where(
                User.telegram_id == telegram_id, User.aktiv.is_(True), User.gesperrt_am.is_(None)
            )
        )
        if user is None:
            return None
        # Der Name aus Telegram ist nur der Startwert; danach gilt, was im Profil steht.
        if name and not user.anzeigename:
            user.anzeigename = name[:200]
            await session.commit()
        rollen = frozenset(
            await session.scalars(select(UserRole.role_name).where(UserRole.user_id == user.id))
        )
    return NutzerKontext(
        nutzer_id=user.id,
        telegram_id=user.telegram_id,
        anzeigename=user.anzeigename,
        rollen=rollen,
        rechte=rechte_von(rollen),
        einstellungen=MappingProxyType(
            {"ton": user.ton, "zeitzone": user.zeitzone, "tageslimit_eur": user.tageslimit_eur}
        ),
    )


async def aktive_mit_rolle(session_fabrik: SessionFabrik, rolle: str) -> list[User]:
    """Aktive, nicht gesperrte Personen mit dieser Rolle."""
    async with session_fabrik() as session:
        ergebnis = await session.scalars(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .where(
                UserRole.role_name == rolle,
                User.aktiv.is_(True),
                User.gesperrt_am.is_(None),
            )
            .order_by(User.id)
        )
        return list(ergebnis)


async def aktive_admins(session_fabrik: SessionFabrik) -> list[User]:
    return await aktive_mit_rolle(session_fabrik, ROLLE_ADMIN)


# --------------------------------------------------------------------------------------
# Verwaltung durch Admins. Gelesen und geändert werden nur Metadaten: Name, Telegram-ID,
# Rollen, Sperre. Inhalte anderer Personen sind hier nicht erreichbar.
# --------------------------------------------------------------------------------------


class NutzerFehler(Exception):
    """Erwartbarer Fehler in der Nutzerverwaltung; die Meldung geht an den Admin."""


@dataclass(frozen=True)
class NutzerZeile:
    id: int
    telegram_id: int
    anzeigename: str
    rollen: tuple[str, ...]
    aktiv: bool
    gesperrt: bool
    tageslimit_eur: Decimal | None


async def liste_nutzer(session_fabrik: SessionFabrik) -> list[NutzerZeile]:
    async with session_fabrik() as session:
        nutzer = list(await session.scalars(select(User).order_by(User.id)))
        rollen: dict[int, list[str]] = {}
        for user_id, rolle in await session.execute(select(UserRole.user_id, UserRole.role_name)):
            rollen.setdefault(user_id, []).append(rolle)
    return [
        NutzerZeile(
            id=u.id,
            telegram_id=u.telegram_id,
            anzeigename=u.anzeigename,
            rollen=tuple(sorted(rollen.get(u.id, []))),
            aktiv=u.aktiv,
            gesperrt=u.gesperrt_am is not None,
            tageslimit_eur=u.tageslimit_eur,
        )
        for u in nutzer
    ]


async def finde_nutzer(session_fabrik: SessionFabrik, angabe: str) -> NutzerZeile:
    """Findet eine Person über Telegram-ID oder Namen. Rät nicht bei mehreren Treffern."""
    angabe = angabe.strip()
    alle = await liste_nutzer(session_fabrik)
    treffer = [n for n in alle if str(n.telegram_id) == angabe]
    if not treffer:
        treffer = [n for n in alle if n.anzeigename.casefold() == angabe.casefold()]
    if not treffer and angabe:
        treffer = [n for n in alle if angabe.casefold() in n.anzeigename.casefold()]
    if not treffer:
        raise NutzerFehler(f"Eine Person „{angabe}“ gibt es nicht. /nutzer zeigt alle.")
    if len(treffer) > 1:
        auswahl = ", ".join(f"{n.anzeigename or '(ohne Namen)'} ({n.telegram_id})" for n in treffer)
        raise NutzerFehler(f"„{angabe}“ ist nicht eindeutig: {auswahl}. Nimm die Telegram-ID.")
    return treffer[0]


async def lege_nutzer_an(
    session_fabrik: SessionFabrik, telegram_id: int, name: str, vergeben_von: int
) -> None:
    async with session_fabrik() as session:
        if await session.scalar(select(User).where(User.telegram_id == telegram_id)):
            raise NutzerFehler(f"Die Telegram-ID {telegram_id} ist schon angelegt.")
        user = User(telegram_id=telegram_id, anzeigename=name[:200])
        session.add(user)
        await session.flush()
        session.add(
            UserRole(user_id=user.id, role_name=ROLLE_MITARBEITER, vergeben_von=vergeben_von)
        )
        await session.commit()


async def aendere_rolle(
    session_fabrik: SessionFabrik, user_id: int, rolle: str, hinzufuegen: bool, vergeben_von: int
) -> None:
    if rolle not in ROLLEN:
        raise NutzerFehler(f"Die Rolle „{rolle}“ gibt es nicht. Möglich: {', '.join(ROLLEN)}.")
    async with session_fabrik() as session:
        vorhanden = await session.get(UserRole, (user_id, rolle))
        if hinzufuegen:
            if vorhanden is not None:
                raise NutzerFehler("Die Person hat diese Rolle schon.")
            session.add(UserRole(user_id=user_id, role_name=rolle, vergeben_von=vergeben_von))
        else:
            if vorhanden is None:
                raise NutzerFehler("Die Person hat diese Rolle nicht.")
            if rolle == ROLLE_ADMIN:
                admins = await session.scalar(
                    select(func.count())
                    .select_from(UserRole)
                    .join(User, User.id == UserRole.user_id)
                    .where(
                        UserRole.role_name == ROLLE_ADMIN,
                        User.aktiv.is_(True),
                        User.gesperrt_am.is_(None),
                    )
                )
                if admins <= 1:
                    raise NutzerFehler("Das ist der letzte Admin; die Rolle bleibt.")
            await session.delete(vorhanden)
        await session.commit()


async def setze_sperre(session_fabrik: SessionFabrik, user_id: int, gesperrt: bool) -> None:
    """Eine gesperrte Person wird ab der nächsten Nachricht nicht mehr bedient."""
    async with session_fabrik() as session:
        user = await session.get(User, user_id)
        user.gesperrt_am = jetzt() if gesperrt else None
        if not gesperrt:
            user.aktiv = True
        await session.commit()


async def merker_gesetzt(session_fabrik: SessionFabrik, schluessel: str) -> bool:
    async with session_fabrik() as session:
        return await session.get(SystemEinstellung, schluessel) is not None


async def setze_merker(session_fabrik: SessionFabrik, schluessel: str, wert: str = "ja") -> None:
    async with session_fabrik() as session:
        if await session.get(SystemEinstellung, schluessel) is None:
            session.add(SystemEinstellung(schluessel=schluessel, wert=wert))
            await session.commit()
