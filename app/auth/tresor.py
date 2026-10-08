"""Verschlüsselte Zugangsdaten je Person (AES-256-GCM, Schlüssel je Person per HKDF).

Der Hauptschlüssel kommt aus SECRETS_MASTER_KEY und liegt nie in der Datenbank. Aus ihm und der
user_id wird je Person ein eigener Schlüssel abgeleitet. Der Schutz gilt gegenüber dem Bot,
seinen Tools und allen Bot-Rollen einschließlich Admin. Wer Root-Zugriff auf den Server hat und
den Hauptschlüssel kennt, kann technisch entschlüsseln.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, select

from app.config import Settings
from app.db.models import UserSecret, jetzt
from app.db.session import SessionFabrik, db_sitzung

SCHLUESSEL_BYTES = 32
NONCE_BYTES = 12
SCHLUESSEL_ERZEUGEN = (
    'python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
)


class TresorFehler(Exception):
    """Fehler beim Ver- oder Entschlüsseln. Die Meldung enthält nie Schlüssel oder Klartext."""


def _lies_schluessel(wert: str, name: str) -> bytes:
    try:
        schluessel = base64.b64decode(wert.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise TresorFehler(f"{name} ist kein gültiges Base64.") from None
    if len(schluessel) != SCHLUESSEL_BYTES:
        raise TresorFehler(f"{name} muss genau {SCHLUESSEL_BYTES} Byte lang sein (Base64).")
    return schluessel


class Tresor:
    """Ver- und entschlüsselt Zugangsdaten. Ohne Hauptschlüssel ist er nicht verfügbar."""

    def __init__(self, schluessel: dict[int, bytes], aktuelle_version: int = 1) -> None:
        self._schluessel = dict(schluessel)
        self.aktuelle_version = aktuelle_version

    @classmethod
    def aus_settings(cls, settings: Settings) -> "Tresor":
        schluessel: dict[int, bytes] = {}
        version = settings.secrets_master_key_version
        if aktuell := settings.secrets_master_key.get_secret_value().strip():
            schluessel[version] = _lies_schluessel(aktuell, "SECRETS_MASTER_KEY")
        # Der vorherige Schlüssel wird nur während einer Rotation gebraucht.
        if alt := settings.secrets_master_key_alt.get_secret_value().strip():
            schluessel[version - 1] = _lies_schluessel(alt, "SECRETS_MASTER_KEY_ALT")
        return cls(schluessel, version)

    @property
    def verfuegbar(self) -> bool:
        return self.aktuelle_version in self._schluessel

    def _schluessel_fuer(self, user_id: int, version: int) -> AESGCM:
        if version not in self._schluessel:
            raise TresorFehler(f"Der Hauptschlüssel der Version {version} ist nicht hinterlegt.")
        abgeleitet = HKDF(
            algorithm=hashes.SHA256(),
            length=SCHLUESSEL_BYTES,
            salt=None,
            info=f"gs-assistant:user:{user_id}:v{version}".encode(),
        ).derive(self._schluessel[version])
        return AESGCM(abgeleitet)

    @staticmethod
    def _bindung(user_id: int, dienst: str) -> bytes:
        """Bindet den Geheimtext an Person und Dienst; an anderer Stelle ist er wertlos."""
        return f"{user_id}:{dienst}".encode()

    def verschluessle(self, user_id: int, dienst: str, klartext: str) -> tuple[bytes, bytes, int]:
        """Liefert (Geheimtext, Nonce, Schlüsselversion)."""
        nonce = os.urandom(NONCE_BYTES)
        geheimtext = self._schluessel_fuer(user_id, self.aktuelle_version).encrypt(
            nonce, klartext.encode("utf-8"), self._bindung(user_id, dienst)
        )
        return geheimtext, nonce, self.aktuelle_version

    def entschluessle(
        self, user_id: int, dienst: str, geheimtext: bytes, nonce: bytes, version: int
    ) -> str:
        try:
            klartext = self._schluessel_fuer(user_id, version).decrypt(
                nonce, geheimtext, self._bindung(user_id, dienst)
            )
        except InvalidTag:
            raise TresorFehler(
                "Die Zugangsdaten lassen sich nicht entschlüsseln (falscher Schlüssel oder "
                "veränderte Daten)."
            ) from None
        return klartext.decode("utf-8")


async def speichere_geheimnis(
    session_fabrik: SessionFabrik, tresor: Tresor, nutzer: object, dienst: str, klartext: str
) -> None:
    """Legt die Zugangsdaten der Person zum Dienst verschlüsselt ab oder ersetzt sie."""
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    geheimtext, nonce, version = tresor.verschluessle(nutzer_id, dienst, klartext)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        eintrag = await session.scalar(
            select(UserSecret).where(UserSecret.user_id == nutzer_id, UserSecret.dienst == dienst)
        )
        if eintrag is None:
            eintrag = UserSecret(user_id=nutzer_id, dienst=dienst)
            session.add(eintrag)
        eintrag.ciphertext = geheimtext
        eintrag.nonce = nonce
        eintrag.schluessel_version = version
        eintrag.aktualisiert_am = jetzt()
        await session.commit()


async def lade_geheimnis(
    session_fabrik: SessionFabrik, tresor: Tresor, nutzer: object, dienst: str
) -> str | None:
    """Entschlüsselt die eigenen Zugangsdaten zum Dienst; None, wenn nichts verbunden ist.

    Der Klartext lebt nur beim Aufrufer und nur für die Dauer des Tool-Aufrufs.
    """
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        eintrag = await session.scalar(
            select(UserSecret).where(UserSecret.user_id == nutzer_id, UserSecret.dienst == dienst)
        )
    if eintrag is None:
        return None
    return tresor.entschluessle(
        nutzer_id, dienst, eintrag.ciphertext, eintrag.nonce, eintrag.schluessel_version
    )


async def loesche_geheimnis(session_fabrik: SessionFabrik, nutzer: object, dienst: str) -> bool:
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        ergebnis = await session.execute(
            delete(UserSecret).where(UserSecret.user_id == nutzer_id, UserSecret.dienst == dienst)
        )
        await session.commit()
    return ergebnis.rowcount > 0


async def verbundene_dienste(session_fabrik: SessionFabrik, nutzer: object) -> list[str]:
    """Nur die Namen der verbundenen Dienste, nie Werte."""
    nutzer_id = getattr(nutzer, "nutzer_id", nutzer)
    async with db_sitzung(session_fabrik, nutzer_id) as session:
        return sorted(
            await session.scalars(select(UserSecret.dienst).where(UserSecret.user_id == nutzer_id))
        )


async def rotiere(session_fabrik: SessionFabrik, tresor: Tresor, nutzer_ids: list[int]) -> int:
    """Verschlüsselt alle Einträge mit älterer Schlüsselversion neu. Liefert deren Anzahl.

    Gebraucht werden dafür der neue Schlüssel (SECRETS_MASTER_KEY) und der bisherige
    (SECRETS_MASTER_KEY_ALT).
    """
    rotiert = 0
    for nutzer_id in nutzer_ids:
        async with db_sitzung(session_fabrik, nutzer_id) as session:
            alte = await session.scalars(
                select(UserSecret).where(
                    UserSecret.user_id == nutzer_id,
                    UserSecret.schluessel_version != tresor.aktuelle_version,
                )
            )
            for eintrag in alte:
                klartext = tresor.entschluessle(
                    nutzer_id,
                    eintrag.dienst,
                    eintrag.ciphertext,
                    eintrag.nonce,
                    eintrag.schluessel_version,
                )
                eintrag.ciphertext, eintrag.nonce, eintrag.schluessel_version = (
                    tresor.verschluessle(nutzer_id, eintrag.dienst, klartext)
                )
                eintrag.aktualisiert_am = jetzt()
                rotiert += 1
            await session.commit()
    return rotiert
