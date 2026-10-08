"""Rechte und Rollen. Bewusst als Konstanten im Code und nicht in der Datenbank: Jede Änderung
ist damit im Code-Review sichtbar.

Es gibt kein Recht, mit dem jemand fremde Nachrichten, Entwürfe, Mails oder Zugangsdaten lesen
kann – auch nicht für Admins. `mail.eigene` gilt immer nur für die eigenen Mails.
"""

from collections.abc import Iterable

from app.db.models import (
    ROLLE_ADMIN,
    ROLLE_APOTHEKEN_UPDATES,
    ROLLE_BUCHHALTUNG,
    ROLLE_MITARBEITER,
)

ASANA_LESEN = "asana.lesen"
ASANA_SCHREIBEN = "asana.schreiben"
ASANA_LOESCHEN = "asana.loeschen"
ASANA_API_AUFRUF = "asana.api_aufruf"
ASANA_TEAMS = "asana.teams"
SHOPIFY_LESEN = "shopify.lesen"
SHOPIFY_SCHREIBEN = "shopify.schreiben"
BESTAND_LESEN = "bestand.lesen"
BESTAND_SCHREIBEN = "bestand.schreiben"
APOTHEKEN_LESEN = "apotheken.lesen"
APOTHEKEN_SCHREIBEN = "apotheken.schreiben"
KLAVIYO_ENTWURF = "klaviyo.entwurf"
KLAVIYO_PLANEN = "klaviyo.planen"
BUCHHALTUNG_LESEN = "buchhaltung.lesen"
BUCHHALTUNG_SCHREIBEN = "buchhaltung.schreiben"
MAIL_EIGENE = "mail.eigene"
ADMIN_NUTZER = "admin.nutzer"
ADMIN_KOSTEN_ALLE = "admin.kosten_alle"

# Recht -> Bedeutung, wie sie auch der Systemprompt nennt
RECHTE: dict[str, str] = {
    ASANA_LESEN: "in Asana lesen",
    ASANA_SCHREIBEN: "in Asana anlegen und ändern (mit Freigabe)",
    ASANA_LOESCHEN: "in Asana löschen (mit zweiter Bestätigung)",
    ASANA_API_AUFRUF: "den allgemeinen Asana-API-Aufruf nutzen",
    ASANA_TEAMS: "Asana-Teams anlegen und Mitglieder verwalten",
    SHOPIFY_LESEN: "Shopify-Daten lesen",
    SHOPIFY_SCHREIBEN: "in Shopify ändern",
    BESTAND_LESEN: "die Bestandsverfolgung lesen",
    BESTAND_SCHREIBEN: "die Bestandsverfolgung ändern",
    APOTHEKEN_LESEN: "Apotheken-Scan und Sortenabgleich lesen",
    APOTHEKEN_SCHREIBEN: "Apotheken-Scan und Sortenabgleich ändern",
    KLAVIYO_ENTWURF: "E-Mail-Kampagnen entwerfen",
    KLAVIYO_PLANEN: "E-Mail-Kampagnen planen",
    BUCHHALTUNG_LESEN: "die Buchhaltung lesen",
    BUCHHALTUNG_SCHREIBEN: "in der Buchhaltung ändern",
    MAIL_EIGENE: "die eigenen Mails lesen und Entwürfe schreiben (nie fremde)",
    ADMIN_NUTZER: "Nutzer, Rollen und Sperren verwalten",
    ADMIN_KOSTEN_ALLE: "die Kosten aller Personen sehen (nur Zahlen)",
}

ROLLEN_RECHTE: dict[str, frozenset[str]] = {
    ROLLE_MITARBEITER: frozenset({ASANA_LESEN, ASANA_SCHREIBEN, MAIL_EIGENE}),
    # Alle Rechte. Zugriff auf fremde persönliche Daten ist nicht darunter, weil es dieses
    # Recht nicht gibt.
    ROLLE_ADMIN: frozenset(RECHTE),
    ROLLE_BUCHHALTUNG: frozenset(
        {BUCHHALTUNG_LESEN, BUCHHALTUNG_SCHREIBEN, MAIL_EIGENE, ASANA_LESEN}
    ),
    ROLLE_APOTHEKEN_UPDATES: frozenset(
        {APOTHEKEN_LESEN, APOTHEKEN_SCHREIBEN, BESTAND_LESEN, ASANA_LESEN}
    ),
}


def rechte_von(rollen: Iterable[str]) -> frozenset[str]:
    """Vereinigung der Rechte aller Rollen einer Person."""
    rechte: set[str] = set()
    for rolle in rollen:
        rechte |= ROLLEN_RECHTE.get(rolle, frozenset())
    return frozenset(rechte)
