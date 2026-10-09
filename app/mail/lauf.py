"""Zustand eines Laufs (eine Nachricht der Person bis zur Antwort), soweit er Mails betrifft.

Der Agent legt ihn je Anfrage an. Er hält die offenen IMAP-Verbindungen, damit eine Anfrage
sich nur einmal je Postfach anmeldet, und merkt sich, welche Mails von außen in diesem Lauf
gelesen wurden. Daraus entsteht in jeder Freigabevorschau der Hinweis, woher eine Aktion kommt.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Obergrenze des Mailinhalts, der verschlüsselt im Gesprächsverlauf abgelegt wird
MAX_KONTEXT_ZEICHEN = 30000


@dataclass
class MailLauf:
    # Was die Person in dieser Nachricht selbst geschrieben hat
    nutzer_text: str = ""
    # Was sie in den letzten eigenen Nachrichten geschrieben hat (ohne Mailinhalt)
    frueherer_nutzer_text: str = ""
    # True, wenn im geladenen Verlauf noch Mailinhalt aus einem früheren Lauf liegt
    verlauf_hat_mail: bool = False
    # Absender der in diesem Lauf gelesenen Mails, in Lesereihenfolge
    gelesene_absender: list[str] = field(default_factory=list)
    # True, wenn in diesem Lauf Trefferlisten (Absender, Betreffs) gelesen wurden
    listen_gelesen: bool = False
    # Adressen, an die eine Antwort auf die gelesenen Mails üblicherweise geht
    antwortweg: set[str] = field(default_factory=set)
    # Tool-Ergebnisse mit Mailinhalt, für den verschlüsselten Verlauf
    kontext_texte: list[str] = field(default_factory=list)
    # True, sobald in diesem Lauf Mailinhalt gelesen oder eine Mail vorbereitet wurde
    vertraulich: bool = False
    # Label -> offene IMAP-Verbindung dieses Laufs
    verbindungen: dict[str, object] = field(default_factory=dict)

    def merke_gelesen(self, absender: str, antwortweg: set[str]) -> None:
        self.vertraulich = True
        if absender and absender not in self.gelesene_absender:
            self.gelesene_absender.append(absender)
        self.antwortweg |= {adresse.lower() for adresse in antwortweg if adresse}

    def merke_kontext(self, text: str) -> None:
        self.vertraulich = True
        belegt = sum(len(teil) for teil in self.kontext_texte)
        if belegt < MAX_KONTEXT_ZEICHEN:
            self.kontext_texte.append(text[: MAX_KONTEXT_ZEICHEN - belegt])

    @property
    def fremdinhalt(self) -> bool:
        """Ob in diesem Lauf oder im geladenen Verlauf Mailinhalt von außen vorliegt."""
        return bool(self.gelesene_absender) or self.listen_gelesen or self.verlauf_hat_mail

    def hat_selbst_genannt(self, adresse: str) -> bool:
        """Ob die Person diese Adresse selbst geschrieben hat."""
        gesucht = adresse.lower()
        return gesucht in self.nutzer_text.lower() or gesucht in self.frueherer_nutzer_text.lower()


aktueller_mail_lauf: ContextVar[MailLauf | None] = ContextVar("aktueller_mail_lauf", default=None)
