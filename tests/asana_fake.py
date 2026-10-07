"""Attrappe für die Asana-API. In Tests findet kein echter Asana-Aufruf statt."""

import json
from collections.abc import Callable
from dataclasses import replace

import httpx
from pydantic import SecretStr

from app.tools.base import ToolKontext

ASANA_TOKEN = "asana-geheimer-token-123"

Antwort = httpx.Response | dict | list | Callable[[httpx.Request], httpx.Response]


class FakeAsana:
    """Beantwortet Anfragen über Routen `(METHODE, Pfad)` und merkt sich alle Aufrufe."""

    def __init__(self) -> None:
        self.anfragen: list[httpx.Request] = []
        self._routen: dict[tuple[str, str], list[Antwort]] = {}

    def route(self, methode: str, pfad: str, *antworten: Antwort) -> None:
        """Mehrere Antworten werden der Reihe nach geliefert, die letzte beliebig oft."""
        self._routen[(methode, pfad)] = list(antworten)

    def aufrufe(self, methode: str | None = None) -> list[tuple[str, str]]:
        return [
            (a.method, a.url.path.removeprefix("/api/1.0"))
            for a in self.anfragen
            if methode is None or a.method == methode
        ]

    def koerper(self, methode: str, pfad: str) -> list[dict]:
        return [
            json.loads(a.content)["data"]
            for a in self.anfragen
            if a.method == methode and a.url.path.removeprefix("/api/1.0") == pfad
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        pfad = request.url.path.removeprefix("/api/1.0")
        antworten = self._routen.get((request.method, pfad))
        if antworten is None:
            return httpx.Response(404, json={"errors": [{"message": f"Unbekannt: {pfad}"}]})
        antwort = antworten.pop(0) if len(antworten) > 1 else antworten[0]
        if isinstance(antwort, Exception):
            raise antwort
        if callable(antwort):
            return antwort(request)
        if isinstance(antwort, httpx.Response):
            return antwort
        return httpx.Response(200, json={"data": antwort})


def asana_kontext(kontext: ToolKontext, fake: FakeAsana, **settings_werte) -> ToolKontext:
    settings = kontext.settings.model_copy(
        update={
            "asana_token": SecretStr(ASANA_TOKEN),
            "asana_workspace_gid": "ws1",
            **settings_werte,
        }
    )
    return replace(kontext, settings=settings, http_transport=httpx.MockTransport(fake))
