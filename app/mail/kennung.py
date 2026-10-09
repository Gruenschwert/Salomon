"""Kennung einer Mail: Ordner, UIDVALIDITY und UID, vom Server erzeugt und versiegelt.

Das Siegel entsteht mit dem Schlüssel der Person und bindet die Kennung an genau ihr
Postfach. Eine Kennung aus einem fremden Postfach wird deshalb abgewiesen, bevor überhaupt
eine Verbindung aufgebaut wird; ein Zufallstreffer auf eine eigene Mail ist ausgeschlossen.
"""

import base64
import binascii
import hmac
from dataclasses import dataclass

from app.auth.tresor import Tresor
from app.mail.konten import Postfach
from app.tools.base import ToolFehler

PRAEFIX = "m1"
SIEGEL_BYTES = 10
ZWECK = "mail-kennung"
FREMD_TEXT = (
    "Diese Kennung gehört nicht zu diesem Postfach oder ist ungültig. Nimm eine Kennung aus "
    "mail_suchen für dasselbe Postfach."
)


@dataclass(frozen=True)
class Kennung:
    ordner: str
    uidvalidity: int
    uid: int


def _siegel(tresor: Tresor, nutzer_id: int, postfach: Postfach, kennung: Kennung) -> str:
    daten = "\0".join(
        (
            postfach.label,
            postfach.adresse.lower(),
            kennung.ordner,
            str(kennung.uidvalidity),
            str(kennung.uid),
        )
    )
    return tresor.siegel(nutzer_id, ZWECK, daten.encode("utf-8"))[:SIEGEL_BYTES].hex()


def erzeuge(tresor: Tresor, nutzer_id: int, postfach: Postfach, kennung: Kennung) -> str:
    ordner = base64.urlsafe_b64encode(kennung.ordner.encode("utf-8")).decode().rstrip("=")
    siegel = _siegel(tresor, nutzer_id, postfach, kennung)
    return f"{PRAEFIX}.{ordner}.{kennung.uidvalidity}.{kennung.uid}.{siegel}"


def pruefe(tresor: Tresor, nutzer_id: int, postfach: Postfach, text: object) -> Kennung:
    """Löst eine Kennung gegen das Postfach der anfragenden Person auf oder weist sie ab."""
    try:
        praefix, ordner, uidvalidity, uid, siegel = str(text).strip().split(".")
        if praefix != PRAEFIX:
            raise ValueError
        roh = base64.urlsafe_b64decode(ordner + "=" * (-len(ordner) % 4))
        kennung = Kennung(roh.decode("utf-8"), int(uidvalidity), int(uid))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ToolFehler(FREMD_TEXT) from None
    if not hmac.compare_digest(siegel, _siegel(tresor, nutzer_id, postfach, kennung)):
        raise ToolFehler(FREMD_TEXT)
    return kennung
