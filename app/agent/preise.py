"""Preise der Claude-Modelle in USD je 1 Mio. Tokens.

Quelle: offizielle Preisseite von Anthropic, Abschnitt „Model pricing“,
https://platform.claude.com/docs/en/about-claude/pricing (erreichbar über docs.claude.com),
abgerufen am 08.10.2026. Bei einem Modellwechsel oder einer Preisänderung hier anpassen.

Cache schreiben = „5m cache writes“ (der Bot nutzt den 5-Minuten-Cache),
Cache lesen = „Cache hits and refreshes“.
"""

from dataclasses import dataclass
from decimal import Decimal

MIO = Decimal(1_000_000)


@dataclass(frozen=True)
class Preis:
    eingabe: Decimal
    ausgabe: Decimal
    cache_lesen: Decimal
    cache_schreiben: Decimal


def _preis(eingabe: str, ausgabe: str, cache_lesen: str, cache_schreiben: str) -> Preis:
    return Preis(Decimal(eingabe), Decimal(ausgabe), Decimal(cache_lesen), Decimal(cache_schreiben))


# Anfang der Modell-ID -> Preis. Längere Anfänge gewinnen (claude-sonnet-5-5 vor
# claude-sonnet-5). Die Tabelle der Preisseite nennt Modellnamen; die IDs folgen dem Schema
# claude-<familie>-<version>.
PREISE: dict[str, Preis] = {
    #                              Eingabe  Ausgabe  Cache lesen  Cache schreiben (5 min)
    "claude-fable-5-1": _preis("10", "50", "0.25", "12.50"),
    "claude-fable-5": _preis("10", "50", "1", "12.50"),
    "claude-opus-5-5": _preis("4", "20", "0.20", "5"),
    "claude-opus-5": _preis("5", "25", "0.50", "6.25"),
    "claude-opus-4-8": _preis("5", "25", "0.50", "6.25"),
    "claude-opus-4-7": _preis("5", "25", "0.50", "6.25"),
    "claude-opus-4-6": _preis("5", "25", "0.50", "6.25"),
    "claude-opus-4-5": _preis("5", "25", "0.50", "6.25"),
    "claude-sonnet-5-5": _preis("2", "10", "0.10", "2.50"),
    "claude-sonnet-5": _preis("2", "10", "0.20", "2.50"),
    "claude-sonnet-4-6": _preis("3", "15", "0.30", "3.75"),
    "claude-sonnet-4-5": _preis("3", "15", "0.30", "3.75"),
    # Haiku 5.5: Preis für Prompts bis 100.000 Tokens; darüber gilt das Fünffache.
    "claude-haiku-5-5": _preis("0.10", "0.50", "0.01", "0.125"),
    "claude-haiku-4-5": _preis("1", "5", "0.10", "1.25"),
}
# Laut Preisseite: Cache schreiben (5 min) = 1,25 × Eingabe, Cache lesen = 0,1 × Eingabe.
CACHE_SCHREIBEN_FAKTOR = Decimal("1.25")
CACHE_LESEN_FAKTOR = Decimal("0.1")


def preis_fuer(modell: str, eingabe_ersatz: Decimal, ausgabe_ersatz: Decimal) -> tuple[Preis, bool]:
    """Preis des Modells und ob er aus der Tabelle stammt.

    Für ein unbekanntes Modell gelten die Werte aus PRICE_INPUT_USD_PER_MTOK und
    PRICE_OUTPUT_USD_PER_MTOK mit den üblichen Cache-Faktoren.
    """
    for anfang in sorted(PREISE, key=len, reverse=True):
        if modell.startswith(anfang):
            return PREISE[anfang], True
    return (
        Preis(
            eingabe_ersatz,
            ausgabe_ersatz,
            eingabe_ersatz * CACHE_LESEN_FAKTOR,
            eingabe_ersatz * CACHE_SCHREIBEN_FAKTOR,
        ),
        False,
    )


def kosten_usd(
    preis: Preis, eingabe: int, ausgabe: int, cache_lesen: int = 0, cache_schreiben: int = 0
) -> Decimal:
    return (
        eingabe * preis.eingabe
        + ausgabe * preis.ausgabe
        + cache_lesen * preis.cache_lesen
        + cache_schreiben * preis.cache_schreiben
    ) / MIO
