"""Shopify-Lese-Tools (Admin GraphQL API, ausschließlich Abfragen)."""

import logging

import httpx

from app.auth.rechte import SHOPIFY_LESEN
from app.tools.base import BasisTool, ToolFehler, ToolKontext

log = logging.getLogger(__name__)

SHOPS = ("canasups", "kiffkraut")
TIMEOUT_SEKUNDEN = 20.0
MAX_VARIANTEN = 20
MAX_BESTELLUNGEN = 20
# Für den Filter „ohne Tracking“ werden mehr Bestellungen geladen, als am Ende ausgegeben werden.
BESTELLUNGEN_JE_ABFRAGE = 50

_SHOP_SCHEMA = {"type": "string", "enum": list(SHOPS), "description": "Welcher Shop"}

LAGERBESTAND_QUERY = """
query Lagerbestand($suche: String!) {
  products(first: 5, query: $suche) {
    nodes {
      title
      variants(first: 10) {
        nodes {
          title
          sku
          inventoryItem {
            inventoryLevels(first: 5) {
              nodes {
                location { name }
                quantities(names: ["available"]) { name quantity }
              }
            }
          }
        }
      }
    }
  }
}
"""

OFFENE_BESTELLUNGEN_QUERY = """
query OffeneBestellungen($anzahl: Int!) {
  orders(first: $anzahl, query: "status:open", sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage }
    nodes {
      name
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      fulfillments(first: 10) {
        trackingInfo(first: 1) { number url }
      }
    }
  }
}
"""


async def shopify_abfrage(kontext: ToolKontext, shop: str, query: str, variablen: dict) -> dict:
    """Schickt eine GraphQL-Abfrage an den Shop und liefert den `data`-Teil der Antwort."""
    if shop not in SHOPS:
        raise ToolFehler(f"Unbekannter Shop. Erlaubt sind: {', '.join(SHOPS)}.")
    if "mutation" in query:
        raise ToolFehler("Schreibende Shopify-Abfragen sind nicht erlaubt.")
    settings = kontext.settings
    domain = getattr(settings, f"shopify_{shop}_domain").strip()
    token = getattr(settings, f"shopify_{shop}_token").get_secret_value()
    version = settings.shopify_api_version.strip()
    if not (domain and token and version):
        raise ToolFehler(f"Der Shop {shop} ist nicht konfiguriert.")
    domain = domain.removeprefix("https://").removeprefix("http://").rstrip("/")

    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SEKUNDEN, transport=kontext.http_transport
        ) as client:
            antwort = await client.post(
                f"https://{domain}/admin/api/{version}/graphql.json",
                headers={"X-Shopify-Access-Token": token},
                json={"query": query, "variables": variablen},
            )
    except httpx.HTTPError as exc:
        log.warning("Shopify (%s) nicht erreichbar: %s", shop, type(exc).__name__)
        raise ToolFehler("Shopify ist gerade nicht erreichbar.") from None

    if antwort.status_code in (401, 403):
        raise ToolFehler("Shopify hat den Zugriff abgelehnt. Token und Leserechte prüfen.")
    if antwort.status_code == 429:
        raise ToolFehler("Das Shopify-Abfragelimit ist erreicht. Bitte später erneut versuchen.")
    if antwort.status_code != 200:
        raise ToolFehler(f"Shopify antwortet mit Status {antwort.status_code}.")
    inhalt = antwort.json()
    if inhalt.get("errors") or "data" not in inhalt:
        log.warning("Shopify (%s) meldet Fehler: %s", shop, inhalt.get("errors"))
        raise ToolFehler("Shopify hat die Abfrage abgelehnt.")
    return inhalt["data"]


class ShopifyLagerbestand(BasisTool):
    erforderliche_rechte = frozenset({SHOPIFY_LESEN})
    name = "shopify_lagerbestand"
    beschreibung = (
        "Liefert den aktuellen Lagerbestand aus Shopify. Nutze es bei Fragen wie „Wie viele X "
        "haben wir?“. Gesucht wird nach Produktname oder SKU. Rückgabe je Variante: Produkt, "
        "Variante, SKU und verfügbarer Bestand je Lagerort. Eine leere Liste bedeutet: kein "
        "passendes Produkt gefunden."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "shop": _SHOP_SCHEMA,
            "suche": {"type": "string", "description": "Produktname oder SKU"},
        },
        "required": ["shop", "suche"],
    }

    async def ausfuehren(self, shop: str, suche: str) -> dict:
        if not isinstance(suche, str) or not suche.strip():
            raise ToolFehler("Der Suchbegriff fehlt.")
        daten = await shopify_abfrage(self.kontext, shop, LAGERBESTAND_QUERY, {"suche": suche})
        varianten = []
        for produkt in daten["products"]["nodes"]:
            for variante in produkt["variants"]["nodes"]:
                lagerorte = (variante.get("inventoryItem") or {}).get("inventoryLevels") or {}
                varianten.append(
                    {
                        "produkt": produkt["title"],
                        "variante": variante["title"],
                        "sku": variante.get("sku") or "",
                        "bestand": [
                            {
                                "lagerort": ebene["location"]["name"],
                                "verfuegbar": sum(m["quantity"] for m in ebene["quantities"]),
                            }
                            for ebene in lagerorte.get("nodes", [])
                        ],
                    }
                )
        ergebnis = {"shop": shop, "varianten": varianten[:MAX_VARIANTEN]}
        if len(varianten) > MAX_VARIANTEN:
            ergebnis["hinweis"] = (
                f"Es gibt {len(varianten)} Treffer, angezeigt werden {MAX_VARIANTEN}. "
                "Suche enger fassen."
            )
        return ergebnis


class ShopifyOffeneBestellungen(BasisTool):
    erforderliche_rechte = frozenset({SHOPIFY_LESEN})
    name = "shopify_offene_bestellungen"
    beschreibung = (
        "Listet offene Bestellungen eines Shops aus Shopify, neueste zuerst. Mit "
        "ohne_tracking=true nur Bestellungen ohne Sendungsverfolgung. Rückgabe je Bestellung: "
        "Bestellnummer, Datum, Versand- und Zahlungsstatus, Tracking vorhanden ja/nein. "
        "Es werden höchstens 20 Bestellungen geliefert und keine Kundendaten."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "shop": _SHOP_SCHEMA,
            "ohne_tracking": {
                "type": "boolean",
                "description": "Nur Bestellungen ohne Tracking (Standard: false)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_BESTELLUNGEN,
                "description": "Maximale Anzahl (Standard und Höchstwert: 20)",
            },
        },
        "required": ["shop"],
    }

    async def ausfuehren(
        self, shop: str, ohne_tracking: bool = False, limit: int = MAX_BESTELLUNGEN
    ) -> dict:
        limit = max(1, min(int(limit), MAX_BESTELLUNGEN))
        daten = await shopify_abfrage(
            self.kontext, shop, OFFENE_BESTELLUNGEN_QUERY, {"anzahl": BESTELLUNGEN_JE_ABFRAGE}
        )
        bestellungen = [
            {
                "bestellnummer": bestellung["name"],
                "datum": bestellung["createdAt"],
                "versandstatus": bestellung["displayFulfillmentStatus"],
                "zahlungsstatus": bestellung["displayFinancialStatus"],
                "tracking_vorhanden": _hat_tracking(bestellung),
            }
            for bestellung in daten["orders"]["nodes"]
        ]
        if ohne_tracking:
            bestellungen = [b for b in bestellungen if not b["tracking_vorhanden"]]
        ergebnis = {
            "shop": shop,
            "bestellungen": bestellungen[:limit],
            "treffer_gesamt": len(bestellungen),
        }
        if daten["orders"]["pageInfo"]["hasNextPage"]:
            ergebnis["hinweis"] = (
                f"Geprüft wurden nur die {BESTELLUNGEN_JE_ABFRAGE} neuesten offenen Bestellungen; "
                "es gibt weitere ältere."
            )
        return ergebnis


def _hat_tracking(bestellung: dict) -> bool:
    return any(
        info.get("number") or info.get("url")
        for versand in bestellung.get("fulfillments") or []
        for info in versand.get("trackingInfo") or []
    )
