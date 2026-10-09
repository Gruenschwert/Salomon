"""Die Postfächer einer Person: verschlüsselte Einträge in `user_secrets` mit `dienst='mail'`.

Ein Postfach gehört genau einer Person. Gelesen wird ausschließlich über `db_sitzung`, also nur
innerhalb der eigenen Zeilen; das Passwort wird erst unmittelbar vor dem Verbindungsaufbau
entschlüsselt und taucht in keiner Meldung auf.
"""

import json
import re
from dataclasses import asdict, dataclass, field, replace

from app.auth.tresor import (
    Tresor,
    TresorFehler,
    labels_von,
    lade_geheimnis,
    loesche_geheimnis,
    speichere_geheimnis,
)
from app.config import Settings
from app.db.models import STANDARD_LABEL
from app.db.session import SessionFabrik
from app.tools.base import ToolFehler, ToolKontext

DIENST = "mail"
MAX_LABEL_ZEICHEN = 30
MAX_SIGNATUR_ZEICHEN = 1000
_LABEL = re.compile(rf"^[a-z0-9][a-z0-9._-]{{0,{MAX_LABEL_ZEICHEN - 1}}}$")
_ADRESSE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;.]+$")
_SERVER = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

NICHT_VERBUNDEN_TEXT = "Du hast noch kein Postfach verbunden. Das geht mit /verbinden mail."
KEIN_SCHLUESSEL_TEXT = (
    "Postfächer sind auf dem Server noch nicht eingerichtet (SECRETS_MASTER_KEY fehlt). Bitte "
    "sprich den Admin an."
)


@dataclass(frozen=True)
class Postfach:
    label: str
    adresse: str
    # Nie in repr, Logs oder Meldungen
    passwort: str = field(repr=False)
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    signatur: str = ""


def ist_adresse(text: str) -> bool:
    return bool(_ADRESSE.match(text)) and len(text) <= 254


def label_aus(adresse: str) -> str:
    """Vorschlag für das Label: der Teil vor dem @, auf erlaubte Zeichen reduziert."""
    roh = adresse.split("@", 1)[0].lower()
    label = re.sub(r"[^a-z0-9._-]", "", roh).lstrip("._-")[:MAX_LABEL_ZEICHEN]
    return label if label and label != STANDARD_LABEL else "postfach"


def pruefe_label(label: str) -> str:
    label = label.strip().lower()
    if not _LABEL.match(label) or label == STANDARD_LABEL:
        raise ToolFehler(
            f"Ein Name für ein Postfach hat höchstens {MAX_LABEL_ZEICHEN} Zeichen: Buchstaben "
            "ohne Umlaute, Ziffern, Punkt, Bindestrich, Unterstrich."
        )
    return label


def lies_server(angabe: str, standard_port: int) -> tuple[str, int]:
    """`host` oder `host:port` aus dem Befehl /verbinden mail."""
    host, _, port = angabe.strip().partition(":")
    if not _SERVER.match(host) or (port and not port.isdigit()):
        raise ToolFehler("Ein Server wird als name oder name:port angegeben, z. B. imap.x.de:993.")
    nummer = int(port) if port else standard_port
    if not 1 <= nummer <= 65535:
        raise ToolFehler("Der Port muss zwischen 1 und 65535 liegen.")
    return host.lower(), nummer


def tresor_fuer(settings: Settings) -> Tresor:
    try:
        tresor = Tresor.aus_settings(settings)
    except TresorFehler:
        raise ToolFehler(KEIN_SCHLUESSEL_TEXT) from None
    if not tresor.verfuegbar:
        raise ToolFehler(KEIN_SCHLUESSEL_TEXT)
    return tresor


async def labels(session_fabrik: SessionFabrik, nutzer: object) -> list[str]:
    """Die Labels der eigenen Postfächer; dafür wird nichts entschlüsselt."""
    return await labels_von(session_fabrik, nutzer, DIENST)


async def speichere_postfach(
    session_fabrik: SessionFabrik, tresor: Tresor, nutzer: object, postfach: Postfach
) -> None:
    daten = asdict(postfach)
    del daten["label"]
    await speichere_geheimnis(
        session_fabrik,
        tresor,
        nutzer,
        DIENST,
        json.dumps(daten, ensure_ascii=False),
        postfach.label,
    )


async def lade_postfach(
    session_fabrik: SessionFabrik, tresor: Tresor, nutzer: object, label: str
) -> Postfach | None:
    """Entschlüsselt ein eigenes Postfach; None, wenn die Person keines mit diesem Label hat."""
    try:
        klartext = await lade_geheimnis(session_fabrik, tresor, nutzer, DIENST, label)
    except TresorFehler as exc:
        raise ToolFehler(str(exc)) from None
    if klartext is None:
        return None
    return Postfach(label=label, **json.loads(klartext))


async def lade_postfaecher(
    session_fabrik: SessionFabrik, tresor: Tresor, nutzer: object
) -> list[Postfach]:
    eigene = [
        await lade_postfach(session_fabrik, tresor, nutzer, label)
        for label in await labels(session_fabrik, nutzer)
    ]
    return [postfach for postfach in eigene if postfach is not None]


async def trenne_postfach(session_fabrik: SessionFabrik, nutzer: object, label: str) -> bool:
    return await loesche_geheimnis(session_fabrik, nutzer, DIENST, label)


async def benenne_um(
    session_fabrik: SessionFabrik, tresor: Tresor, nutzer: object, alt: str, neu: str
) -> Postfach:
    """Gibt einem eigenen Postfach einen anderen Namen. Der Geheimtext ist an das Label
    gebunden und wird deshalb neu verschlüsselt."""
    neu = pruefe_label(neu)
    if neu == alt:
        raise ToolFehler(f"Das Postfach heißt schon „{alt}“.")
    if neu in await labels(session_fabrik, nutzer):
        raise ToolFehler(f"Du hast schon ein Postfach mit dem Namen „{neu}“.")
    postfach = await lade_postfach(session_fabrik, tresor, nutzer, alt)
    if postfach is None:
        raise ToolFehler(f"Ein Postfach „{alt}“ hast du nicht verbunden.")
    umbenannt = replace(postfach, label=neu)
    await speichere_postfach(session_fabrik, tresor, nutzer, umbenannt)
    await trenne_postfach(session_fabrik, nutzer, alt)
    return umbenannt


async def setze_signatur(
    session_fabrik: SessionFabrik, tresor: Tresor, nutzer: object, label: str, signatur: str
) -> None:
    signatur = signatur.strip()
    if len(signatur) > MAX_SIGNATUR_ZEICHEN:
        raise ToolFehler(f"Eine Signatur darf höchstens {MAX_SIGNATUR_ZEICHEN} Zeichen haben.")
    postfach = await lade_postfach(session_fabrik, tresor, nutzer, label)
    if postfach is None:
        raise ToolFehler(f"Ein Postfach „{label}“ hast du nicht verbunden.")
    await speichere_postfach(session_fabrik, tresor, nutzer, replace(postfach, signatur=signatur))


def _auswahl_text(eigene: list[str]) -> str:
    return ", ".join(eigene)


async def loese_konto(kontext: ToolKontext, nutzer: object, konto: object) -> Postfach:
    """Löst den Tool-Parameter `konto` auf, und zwar ausschließlich innerhalb der eigenen
    Postfächer der anfragenden Person. Fremde Labels gibt es an dieser Stelle nicht."""
    eigene = await labels(kontext.session_fabrik, nutzer)
    if not eigene:
        raise ToolFehler(NICHT_VERBUNDEN_TEXT)
    if konto is None or konto == "":
        if len(eigene) > 1:
            raise ToolFehler(
                f"Du hast mehrere Postfächer. Gib mit konto an, welches: {_auswahl_text(eigene)}."
            )
        konto = eigene[0]
    if not isinstance(konto, str) or konto not in eigene:
        raise ToolFehler(
            f"Dieses Postfach gibt es bei dir nicht. Deine Postfächer: {_auswahl_text(eigene)}."
        )
    postfach = await lade_postfach(
        kontext.session_fabrik, tresor_fuer(kontext.settings), nutzer, konto
    )
    if postfach is None:
        raise ToolFehler(NICHT_VERBUNDEN_TEXT)
    return postfach
