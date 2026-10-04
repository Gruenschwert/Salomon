"""Agent-Schleife."""

import logging

import anthropic

from app.agent.history import lade_verlauf, speichere_austausch
from app.agent.prompts import SYSTEM_PROMPT
from app.auth.approvals import Freigaben
from app.channels.base import Antwort, EingehendeNachricht, FreigabeAnfrage
from app.config import Settings
from app.db.models import User
from app.db.session import SessionFabrik
from app.observability.audit import protokolliere
from app.observability.costs import Kosten
from app.tools.base import ToolFehler, ToolKontext
from app.tools.registry import Registry, ToolErgebnis, fuehre_tool_aus

log = logging.getLogger(__name__)

MAX_ITERATIONEN_TEXT = (
    "Ich habe die maximale Anzahl an Tool-Schritten erreicht und breche hier ab. "
    "Bitte stelle die Frage enger gefasst noch einmal."
)
WARTET_AUF_FREIGABE_TEXT = (
    "Wartet auf Freigabe des Nutzers. Das Tool wurde NICHT ausgeführt. Der Nutzer sieht jetzt "
    "eine Vorschau mit den Buttons ✅ / ❌. Beende die Runde mit einem kurzen Hinweis darauf."
)
TAGESLIMIT_TEXT = (
    "Das Tageslimit für KI-Kosten ist erreicht. Neue Anfragen sind ab morgen wieder möglich."
)
DIENST_FEHLER_TEXT = "Der KI-Dienst ist gerade nicht erreichbar. Bitte versuche es später erneut."
ABLEHNUNG_TEXT = "Diese Anfrage kann ich nicht beantworten."
LEERE_ANTWORT_TEXT = "Dazu habe ich keine Antwort erhalten. Bitte formuliere die Frage neu."
GEKUERZT_HINWEIS = "\n\n(Antwort wurde wegen der Längenbegrenzung gekürzt.)"


class Agent:
    def __init__(
        self,
        settings: Settings,
        session_fabrik: SessionFabrik,
        client: anthropic.AsyncAnthropic,
        registry: Registry,
        freigaben: Freigaben,
        kosten: Kosten,
    ) -> None:
        self._settings = settings
        self._session_fabrik = session_fabrik
        self._client = client
        self._registry = registry
        self._freigaben = freigaben
        self._kosten = kosten
        self._kontext = ToolKontext(settings=settings, session_fabrik=session_fabrik)

    async def beantworte(self, nachricht: EingehendeNachricht, user: User) -> Antwort:
        if await self._kosten.limit_erreicht():
            return Antwort(text=TAGESLIMIT_TEXT)
        verlauf = await lade_verlauf(
            self._session_fabrik, nachricht.chat_id, self._settings.history_max_messages
        )
        messages = [*verlauf, {"role": "user", "content": nachricht.text}]
        # Freigaben, die in dieser Runde angelegt wurden; der Kanal zeigt sie mit Buttons an.
        anfragen: list[FreigabeAnfrage] = []
        try:
            text = await self._schleife(messages, user, anfragen)
        except anthropic.APIError:
            log.exception("Claude-Aufruf fehlgeschlagen")
            return Antwort(text=DIENST_FEHLER_TEXT, freigaben=tuple(anfragen))
        await speichere_austausch(
            self._session_fabrik, nachricht.chat_id, user.id, nachricht.text, text
        )
        return Antwort(text=text, freigaben=tuple(anfragen))

    async def _schleife(
        self, messages: list[dict], user: User, anfragen: list[FreigabeAnfrage]
    ) -> str:
        anfrage = {
            "model": self._settings.model_default,
            "max_tokens": self._settings.max_output_tokens,
            "system": SYSTEM_PROMPT,
        }
        if tools := self._registry.api_definitionen(user.rolle):
            anfrage["tools"] = tools
        for _ in range(self._settings.max_tool_iterations):
            response = await self._client.messages.create(**anfrage, messages=messages)
            await self._kosten.verbuche(user.id, *_tokens(response.usage))
            if response.stop_reason != "tool_use":
                return _antworttext(response)
            messages.append({"role": "assistant", "content": response.content})
            ergebnisse = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                ergebnis = await self._bearbeite_tool_anfrage(
                    block.name, block.input, user, anfragen
                )
                ergebnisse.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": ergebnis.text,
                        "is_error": ergebnis.fehler,
                    }
                )
            messages.append({"role": "user", "content": ergebnisse})
        return MAX_ITERATIONEN_TEXT

    async def _bearbeite_tool_anfrage(
        self, name: str, params: dict, user: User, anfragen: list[FreigabeAnfrage]
    ) -> ToolErgebnis:
        tool = self._registry.hole(name)
        if tool is None or user.rolle not in tool.erlaubte_rollen:
            await protokolliere(
                self._session_fabrik,
                user_id=user.id,
                tool_name=name,
                parameter=params,
                fehler="nicht verfügbar",
            )
            return ToolErgebnis(f"Das Tool {name} ist nicht verfügbar.", fehler=True)
        if not tool.schreibend:
            return await fuehre_tool_aus(tool, params, user, self._kontext)
        # Schreibend: NICHT ausführen, sondern Freigabe anlegen.
        try:
            anfragen.append(await self._freigaben.anfragen(user, tool, params))
        except ToolFehler as exc:
            return ToolErgebnis(f"Fehler: {exc}", fehler=True)
        except Exception:
            log.exception("Freigabe für Tool %s konnte nicht angelegt werden", name)
            return ToolErgebnis(f"Interner Fehler im Tool {name}.", fehler=True)
        return ToolErgebnis(WARTET_AUF_FREIGABE_TEXT)


def _tokens(usage: anthropic.types.Usage) -> tuple[int, int]:
    """Input- und Output-Tokens; Cache-Tokens zählen vorsichtshalber voll als Input."""
    eingabe = (
        usage.input_tokens
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    )
    return eingabe, usage.output_tokens


def _antworttext(response: anthropic.types.Message) -> str:
    if response.stop_reason == "refusal":
        return ABLEHNUNG_TEXT
    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        return LEERE_ANTWORT_TEXT
    if response.stop_reason == "max_tokens":
        text += GEKUERZT_HINWEIS
    return text
