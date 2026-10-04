"""Agent-Schleife."""

import logging

import anthropic

from app.agent.history import lade_verlauf, speichere_austausch
from app.agent.prompts import SYSTEM_PROMPT
from app.channels.base import Antwort, EingehendeNachricht
from app.config import Settings
from app.db.models import User
from app.db.session import SessionFabrik

log = logging.getLogger(__name__)

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
    ) -> None:
        self._settings = settings
        self._session_fabrik = session_fabrik
        self._client = client

    async def beantworte(self, nachricht: EingehendeNachricht, user: User) -> Antwort:
        verlauf = await lade_verlauf(
            self._session_fabrik, nachricht.chat_id, self._settings.history_max_messages
        )
        messages = [*verlauf, {"role": "user", "content": nachricht.text}]
        try:
            text = await self._schleife(messages)
        except anthropic.APIError:
            log.exception("Claude-Aufruf fehlgeschlagen")
            return Antwort(text=DIENST_FEHLER_TEXT)
        await speichere_austausch(
            self._session_fabrik, nachricht.chat_id, user.id, nachricht.text, text
        )
        return Antwort(text=text)

    async def _schleife(self, messages: list[dict]) -> str:
        response = await self._client.messages.create(
            model=self._settings.model_default,
            max_tokens=self._settings.max_output_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        return _antworttext(response)


def _antworttext(response: anthropic.types.Message) -> str:
    if response.stop_reason == "refusal":
        return ABLEHNUNG_TEXT
    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        return LEERE_ANTWORT_TEXT
    if response.stop_reason == "max_tokens":
        text += GEKUERZT_HINWEIS
    return text
