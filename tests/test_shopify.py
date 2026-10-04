import json
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from app.tools.base import ToolFehler
from app.tools.registry import fuehre_tool_aus, lade_registry
from app.tools.shopify import ShopifyLagerbestand, ShopifyOffeneBestellungen

TOKEN = "shpat_geheim"

LAGER_ANTWORT = {
    "data": {
        "products": {
            "nodes": [
                {
                    "title": "Grinder Classic",
                    "variants": {
                        "nodes": [
                            {
                                "title": "Schwarz",
                                "sku": "GR-001",
                                "inventoryItem": {
                                    "inventoryLevels": {
                                        "nodes": [
                                            {
                                                "location": {"name": "Hauptlager"},
                                                "quantities": [
                                                    {"name": "available", "quantity": 42}
                                                ],
                                            },
                                            {
                                                "location": {"name": "Außenlager"},
                                                "quantities": [
                                                    {"name": "available", "quantity": 3}
                                                ],
                                            },
                                        ]
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        }
    }
}


def _bestellung(nummer: str, tracking: str | None, versendet: bool = True) -> dict:
    versand = [{"trackingInfo": [{"number": tracking, "url": None}] if tracking else []}]
    return {
        "name": nummer,
        "createdAt": "2026-10-01T08:00:00Z",
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "FULFILLED" if versendet else "UNFULFILLED",
        "fulfillments": versand if versendet else [],
    }


def _bestell_antwort(bestellungen: list[dict], weitere: bool = False) -> dict:
    return {"data": {"orders": {"pageInfo": {"hasNextPage": weitere}, "nodes": bestellungen}}}


@pytest.fixture
def shopify_kontext(kontext):
    """Liefert eine Fabrik: Kontext mit konfiguriertem Kiffkraut-Shop und Attrappen-Transport."""

    def _baue(antwort: dict | None = None, status: int = 200):
        anfragen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            anfragen.append(request)
            return httpx.Response(status, json=antwort or {})

        settings = kontext.settings.model_copy(
            update={
                "shopify_kiffkraut_domain": "https://kiffkraut.myshopify.com/",
                "shopify_kiffkraut_token": SecretStr(TOKEN),
                "shopify_api_version": "2026-07",
            }
        )
        neuer = replace(kontext, settings=settings, http_transport=httpx.MockTransport(handler))
        return neuer, anfragen

    return _baue


def test_echte_registry_findet_alle_tools(kontext):
    assert [tool.name for tool in lade_registry(kontext).alle()] == [
        "demo_notiz",
        "shopify_lagerbestand",
        "shopify_offene_bestellungen",
    ]


def test_shopify_tools_sind_lesend(kontext):
    assert not ShopifyLagerbestand(kontext).schreibend
    assert not ShopifyOffeneBestellungen(kontext).schreibend


async def test_lagerbestand(shopify_kontext):
    kontext, anfragen = shopify_kontext(LAGER_ANTWORT)
    ergebnis = await ShopifyLagerbestand(kontext).ausfuehren(shop="kiffkraut", suche="Grinder")

    assert ergebnis["varianten"] == [
        {
            "produkt": "Grinder Classic",
            "variante": "Schwarz",
            "sku": "GR-001",
            "bestand": [
                {"lagerort": "Hauptlager", "verfuegbar": 42},
                {"lagerort": "Außenlager", "verfuegbar": 3},
            ],
        }
    ]
    (anfrage,) = anfragen
    assert str(anfrage.url) == "https://kiffkraut.myshopify.com/admin/api/2026-07/graphql.json"
    assert anfrage.headers["X-Shopify-Access-Token"] == TOKEN
    koerper = json.loads(anfrage.content)
    assert koerper["variables"] == {"suche": "Grinder"}
    assert "mutation" not in koerper["query"]


async def test_offene_bestellungen_ohne_tracking(shopify_kontext):
    antwort = _bestell_antwort(
        [
            _bestellung("#1003", None, versendet=False),
            _bestellung("#1002", "DHL123"),
            _bestellung("#1001", None),
        ]
    )
    kontext, _ = shopify_kontext(antwort)
    ergebnis = await ShopifyOffeneBestellungen(kontext).ausfuehren(
        shop="kiffkraut", ohne_tracking=True
    )
    assert [b["bestellnummer"] for b in ergebnis["bestellungen"]] == ["#1003", "#1001"]
    assert ergebnis["bestellungen"][0] == {
        "bestellnummer": "#1003",
        "datum": "2026-10-01T08:00:00Z",
        "versandstatus": "UNFULFILLED",
        "zahlungsstatus": "PAID",
        "tracking_vorhanden": False,
    }
    assert "hinweis" not in ergebnis


async def test_offene_bestellungen_limit_hoechstens_20(shopify_kontext):
    antwort = _bestell_antwort([_bestellung(f"#{n}", None) for n in range(30)], weitere=True)
    kontext, _ = shopify_kontext(antwort)
    ergebnis = await ShopifyOffeneBestellungen(kontext).ausfuehren(shop="kiffkraut", limit=99)
    assert len(ergebnis["bestellungen"]) == 20
    assert ergebnis["treffer_gesamt"] == 30
    assert "hinweis" in ergebnis


async def test_nicht_konfigurierter_shop(shopify_kontext):
    kontext, anfragen = shopify_kontext(LAGER_ANTWORT)
    with pytest.raises(ToolFehler, match="nicht konfiguriert"):
        await ShopifyLagerbestand(kontext).ausfuehren(shop="canasups", suche="x")
    assert anfragen == []


async def test_unbekannter_shop(shopify_kontext):
    kontext, _ = shopify_kontext(LAGER_ANTWORT)
    with pytest.raises(ToolFehler, match="Unbekannter Shop"):
        await ShopifyLagerbestand(kontext).ausfuehren(shop="anderer", suche="x")


async def test_shopify_fehler_verraten_kein_token(shopify_kontext, user):
    kontext, _ = shopify_kontext(status=401)
    ergebnis = await fuehre_tool_aus(
        ShopifyLagerbestand(kontext), {"shop": "kiffkraut", "suche": "x"}, user, kontext
    )
    assert ergebnis.fehler
    assert "abgelehnt" in ergebnis.text
    assert TOKEN not in ergebnis.text


async def test_graphql_fehler_wird_zu_toolfehler(shopify_kontext):
    kontext, _ = shopify_kontext({"errors": [{"message": "Field does not exist"}]})
    with pytest.raises(ToolFehler, match="abgelehnt"):
        await ShopifyLagerbestand(kontext).ausfuehren(shop="kiffkraut", suche="x")
