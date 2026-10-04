import anthropic
import httpx2
from sqlalchemy import select

from app.agent.history import lade_verlauf
from app.agent.loop import DIENST_FEHLER_TEXT, MAX_ITERATIONEN_TEXT
from app.agent.prompts import SYSTEM_PROMPT
from app.channels.base import EingehendeNachricht
from app.db.models import AuditLog
from tests.beispiel_tools.schreibend import BeispielSchreiben
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

CHAT_ID = 7


def nachricht(text: str) -> EingehendeNachricht:
    return EingehendeNachricht(
        chat_id=CHAT_ID, absender_id=ERLAUBT_ID, absender_name="X", text=text
    )


async def _audit(session_fabrik) -> list[AuditLog]:
    async with session_fabrik() as session:
        return list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))


async def test_antwort_und_verlauf(settings, session_fabrik, baue_agent, user):
    client = FakeAnthropic(claude_antwort(text_block("Hallo!")))
    agent = baue_agent(client)

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


async def test_verlauf_wird_mitgeschickt(settings, session_fabrik, baue_agent, user):
    client = FakeAnthropic(claude_antwort(text_block("Antwort")))
    agent = baue_agent(client)
    await agent.beantworte(nachricht("Erste"), user)
    await agent.beantworte(nachricht("Zweite"), user)
    assert [m["content"] for m in client.aufrufe[1]["messages"]] == ["Erste", "Antwort", "Zweite"]


async def test_verlauf_ist_begrenzt_und_beginnt_mit_nutzer(session_fabrik, baue_agent, user):
    client = FakeAnthropic(claude_antwort(text_block("A")))
    agent = baue_agent(client)
    for i in range(5):
        await agent.beantworte(nachricht(f"F{i}"), user)
    verlauf = await lade_verlauf(session_fabrik, CHAT_ID, 3)
    assert [m["content"] for m in verlauf] == ["F4", "A"]


async def test_api_fehler_fuehrt_zu_hoeflicher_meldung(settings, session_fabrik, baue_agent, user):
    fehler = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid"))
    agent = baue_agent(FakeAnthropic(fehler))
    antwort = await agent.beantworte(nachricht("Hi"), user)
    assert antwort.text == DIENST_FEHLER_TEXT
    assert await lade_verlauf(session_fabrik, CHAT_ID, 20) == []


async def test_lesendes_tool_wird_ausgefuehrt(settings, session_fabrik, baue_agent, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_lesen", {"text": "Ping"})),
        claude_antwort(text_block("Fertig")),
    )
    agent = baue_agent(client)

    antwort = await agent.beantworte(nachricht("Bitte lesen"), user)

    assert antwort.text == "Fertig"
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["type"] == "tool_result"
    assert ergebnis["tool_use_id"] == "toolu_1"
    assert '"echo": "Ping"' in ergebnis["content"]
    assert ergebnis["is_error"] is False
    assert [e.tool_name for e in await _audit(session_fabrik)] == ["beispiel_lesen"]
    # Im Verlauf landet nur der Text, keine Tool-Blöcke.
    assert [m["content"] for m in await lade_verlauf(session_fabrik, CHAT_ID, 20)] == [
        "Bitte lesen",
        "Fertig",
    ]


async def test_tool_liste_entspricht_der_rolle(settings, session_fabrik, baue_agent, user, admin):
    client = FakeAnthropic(claude_antwort(text_block("ok")))
    agent = baue_agent(client)
    await agent.beantworte(nachricht("a"), user)
    await agent.beantworte(nachricht("b"), admin)
    namen_user = {t["name"] for t in client.aufrufe[0]["tools"]}
    namen_admin = {t["name"] for t in client.aufrufe[1]["tools"]}
    assert namen_admin - namen_user == {"beispiel_admin"}


async def test_tool_ausserhalb_der_rolle_wird_abgelehnt(settings, session_fabrik, baue_agent, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_admin", {})),
        claude_antwort(text_block("Geht nicht")),
    )
    agent = baue_agent(client)
    await agent.beantworte(nachricht("x"), user)
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["is_error"] is True
    (eintrag,) = await _audit(session_fabrik)
    assert eintrag.fehler == "nicht verfügbar"


async def test_tool_fehler_fuehrt_nicht_zum_absturz(settings, session_fabrik, baue_agent, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_kaputt", {})),
        claude_antwort(text_block("Das Tool hatte einen Fehler.")),
    )
    agent = baue_agent(client)
    antwort = await agent.beantworte(nachricht("x"), user)
    assert antwort.text == "Das Tool hatte einen Fehler."
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["is_error"] is True
    assert "geheimes" not in ergebnis["content"]


async def test_schleife_bricht_bei_max_tool_iterations_ab(
    settings, session_fabrik, baue_agent, user
):
    client = FakeAnthropic(claude_antwort(tool_use_block("beispiel_lesen", {"text": "x"})))
    agent = baue_agent(client)
    antwort = await agent.beantworte(nachricht("Endlos"), user)
    assert antwort.text == MAX_ITERATIONEN_TEXT
    assert len(client.aufrufe) == settings.max_tool_iterations
    assert len(await _audit(session_fabrik)) == settings.max_tool_iterations


async def test_schreibendes_tool_wird_nicht_ausgefuehrt(settings, session_fabrik, baue_agent, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_schreiben", {"text": "eilig!"})),
        claude_antwort(text_block("ok")),
    )
    agent = baue_agent(client)
    await agent.beantworte(nachricht("Schreib das sofort"), user)
    assert BeispielSchreiben.ausgefuehrt == []
