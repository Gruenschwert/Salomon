"""Agent-Schleife."""

import logging

import anthropic

from app.agent.history import lade_verlauf, speichere_austausch
from app.agent.prompts import SYSTEM_PROMPT
from app.channels.base import Antwort, EingehendeNachricht
from app.config import Settings
from app.db.models import User
from app.db.session import SessionFabrik
from app.observability.audit import protokolliere
from app.tools.base import ToolKontext
from app.tools.registry import Registry, ToolErgebnis, fuehre_tool_aus

log = logging.getLogger(__name__)

MAX_ITERATIONEN_TEXT = (
    "Ich habe die maximale Anzahl an Tool-Schritten erreicht und breche hier ab. "
    "Bitte stelle die Frage enger gefasst noch einmal."
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
    ) -> None:
        self._settings = settings
        self._session_fabrik = session_fabrik
        self._client = client
        self._registry = registry
        self._kontext = ToolKontext(settings=settings, session_fabrik=session_fabrik)

    async def beantworte(self, nachricht: EingehendeNachricht, user: User) -> Antwort:
        verlauf = await lade_verlauf(
            self._session_fabrik, nachricht.chat_id, self._settings.history_max_messages
        )
        messages = [*verlauf, {"role": "user", "content": nachricht.text}]
        try:
            text = await self._schleife(messages, user)
        except anthropic.APIError:
            log.exception("Claude-Aufruf fehlgeschlagen")
            return Antwort(text=DIENST_FEHLER_TEXT)
        await speichere_austausch(
            self._session_fabrik, nachricht.chat_id, user.id, nachricht.text, text
        )
        return Antwort(text=text)

    async def _schleife(self, messages: list[dict], user: User) -> str:
        anfrage = {
            "model": self._settings.model_default,
            "max_tokens": self._settings.max_output_tokens,
            "system": SYSTEM_PROMPT,
        }
        if tools := self._registry.api_definitionen(user.rolle):
            anfrage["tools"] = tools
        for _ in range(self._settings.max_tool_iterations):
            response = await self._client.messages.create(**anfrage, messages=messages)
            if response.stop_reason != "tool_use":
                return _antworttext(response)
            messages.append({"role": "assistant", "content": response.content})
            ergebnisse = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                ergebnis = await self._bearbeite_tool_anfrage(block.name, block.input, user)
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

    async def _bearbeite_tool_anfrage(self, name: str, params: dict, user: User) -> ToolErgebnis:
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
        if tool.schreibend:
            return ToolErgebnis("Schreibende Tools sind nicht freigeschaltet.", fehler=True)
        return await fuehre_tool_aus(tool, params, user, self._kontext)


def _antworttext(response: anthropic.types.Message) -> str:
    if response.stop_reason == "refusal":
        return ABLEHNUNG_TEXT
    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        return LEERE_ANTWORT_TEXT
    if response.stop_reason == "max_tokens":
        text += GEKUERZT_HINWEIS
    return text
