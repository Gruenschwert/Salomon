import anthropic
import httpx2
import pytest

from app.agent.history import lade_verlauf
from app.agent.loop import DIENST_FEHLER_TEXT, Agent
from app.agent.prompts import SYSTEM_PROMPT
from app.auth.users import finde_erlaubten_nutzer, synchronisiere_whitelist
from app.channels.base import EingehendeNachricht
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block

CHAT_ID = 7


def nachricht(text: str) -> EingehendeNachricht:
    return EingehendeNachricht(
        chat_id=CHAT_ID, absender_id=ERLAUBT_ID, absender_name="X", text=text
    )


@pytest.fixture
async def user(settings, session_fabrik):
    await synchronisiere_whitelist(session_fabrik, settings)
    return await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)


async def test_antwort_und_verlauf(settings, session_fabrik, user):
    client = FakeAnthropic(claude_antwort(text_block("Hallo!")))
    agent = Agent(settings, session_fabrik, client)

    antwort = await agent.beantworte(nachricht("Hi"), user)

    assert antwort.text == "Hallo!"
    aufruf = client.aufrufe[0]
    assert aufruf["model"] == settings.model_default
    assert aufruf["max_tokens"] == settings.max_output_tokens
    assert aufruf["system"] == SYSTEM_PROMPT
    assert aufruf["messages"] == [{"role": "user", "content": "Hi"}]
    assert await lade_verlauf(session_fabrik, CHAT_ID, 20) == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hallo!"},
    ]


async def test_verlauf_wird_mitgeschickt(settings, session_fabrik, user):
    client = FakeAnthropic(claude_antwort(text_block("Antwort")))
    agent = Agent(settings, session_fabrik, client)
    await agent.beantworte(nachricht("Erste"), user)
    await agent.beantworte(nachricht("Zweite"), user)
    assert [m["content"] for m in client.aufrufe[1]["messages"]] == ["Erste", "Antwort", "Zweite"]


async def test_verlauf_ist_begrenzt_und_beginnt_mit_nutzer(settings, session_fabrik, user):
    client = FakeAnthropic(claude_antwort(text_block("A")))
    agent = Agent(settings, session_fabrik, client)
    for i in range(5):
        await agent.beantworte(nachricht(f"F{i}"), user)
    verlauf = await lade_verlauf(session_fabrik, CHAT_ID, 3)
    assert [m["content"] for m in verlauf] == ["F4", "A"]


async def test_api_fehler_fuehrt_zu_hoeflicher_meldung(settings, session_fabrik, user):
    fehler = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid"))
    agent = Agent(settings, session_fabrik, FakeAnthropic(fehler))
    antwort = await agent.beantworte(nachricht("Hi"), user)
    assert antwort.text == DIENST_FEHLER_TEXT
    assert await lade_verlauf(session_fabrik, CHAT_ID, 20) == []
