"""Schutz gegen Anweisungen in Mails: Mailinhalt ist Daten, nie Anweisung.

Jedes Tool-Ergebnis mit Mailinhalt geht nur in dieser Umrandung an das Modell. Der
Systemprompt erklärt sie; technisch erzwungen wird der Schutz durch die Freigabe, ohne die
nichts mit Wirkung nach außen passiert.
"""

import re

ANFANG = '<mail_inhalt untrusted="true">'
ENDE = "</mail_inhalt>"
PLATZHALTER_ENTFERNT = "[Mailinhalt aus Datenschutzgründen entfernt]"
_MARKE = re.compile(r"<\s*(/?)\s*mail_inhalt", re.IGNORECASE)


def entschaerfe(text: str) -> str:
    """Verhindert, dass Mailinhalt die Umrandung selbst schließt oder eine neue öffnet."""
    return _MARKE.sub(r"<\1mail-inhalt", text)


def umrande(text: str) -> str:
    return f"{ANFANG}\n{entschaerfe(text)}\n{ENDE}"
