from datetime import timedelta

from sqlalchemy import select

from app.agent.loop import WARTET_AUF_FREIGABE_TEXT
from app.auth.approvals import (
    STATUS_BEREITS_ENTSCHIEDEN,
    STATUS_FREMDER_NUTZER,
    STATUS_NICHT_GEFUNDEN,
    Freigaben,
)
from app.channels.base import EingehendeNachricht
from app.db.models import Approval, AuditLog, Notiz, jetzt
from app.tools.registry import lade_registry
from tests.beispiel_tools.schreibend import BeispielSchreiben
from tests.conftest import ADMIN_ID, ERLAUBT_ID, FREMD_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block


async def _approval(session_fabrik, approval_id: int) -> Approval:
    async with session_fabrik() as session:
        return await session.get(Approval, approval_id)


async def _anfrage(freigaben, registry, user, text="Hallo"):
    return await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": text})


async def test_schleife_legt_freigabe_an_statt_auszufuehren(baue_agent, session_fabrik, user):
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_schreiben", {"text": "eilig, sofort!"})),
        claude_antwort(text_block("Bitte bestätige die Freigabe.")),
    )
    nachricht = EingehendeNachricht(
        chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="Schreib das"
    )

    antwort = await baue_agent(client).beantworte(nachricht, user)

    assert BeispielSchreiben.ausgefuehrt == []
    (anfrage,) = antwort.freigaben
    assert anfrage.vorschau_text == "Schreiben: eilig, sofort!"
    approval = await _approval(session_fabrik, anfrage.approval_id)
    assert approval.status == "offen"
    assert approval.user_id == user.id
    (ergebnis,) = client.aufrufe[1]["messages"][-1]["content"]
    assert ergebnis["content"] == WARTET_AUF_FREIGABE_TEXT
    async with session_fabrik() as session:
        (eintrag,) = list(await session.scalars(select(AuditLog)))
    assert eintrag.ergebnis_kurz == f"Freigabe #{approval.id} angefragt"


async def test_genehmigung_fuehrt_aus(freigaben, registry, user, session_fabrik):
    anfrage = await _anfrage(freigaben, registry, user)
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert entscheidung.text.startswith("✅")
    assert BeispielSchreiben.ausgefuehrt == ["Hallo"]
    approval = await _approval(session_fabrik, anfrage.approval_id)
    assert approval.status == "genehmigt"
    assert approval.entschieden_am is not None
    async with session_fabrik() as session:
        eintraege = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert [e.tool_name for e in eintraege] == ["beispiel_schreiben", "beispiel_schreiben"]
    assert "gespeichert" in eintraege[1].ergebnis_kurz


async def test_ablehnung_verwirft(freigaben, registry, user, session_fabrik):
    anfrage = await _anfrage(freigaben, registry, user)
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=False)
    assert entscheidung.text.startswith("❌")
    assert BeispielSchreiben.ausgefuehrt == []
    assert (await _approval(session_fabrik, anfrage.approval_id)).status == "abgelehnt"


async def test_anderer_nutzer_kann_nicht_freigeben(freigaben, registry, user, session_fabrik):
    anfrage = await _anfrage(freigaben, registry, user)
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=True)
    assert entscheidung.status == STATUS_FREMDER_NUTZER
    assert not entscheidung.abgeschlossen
    assert BeispielSchreiben.ausgefuehrt == []
    assert (await _approval(session_fabrik, anfrage.approval_id)).status == "offen"


async def test_unbekannter_nutzer_wird_ignoriert(freigaben, registry, user, session_fabrik):
    anfrage = await _anfrage(freigaben, registry, user)
    assert await freigaben.entscheiden(anfrage.approval_id, FREMD_ID, genehmigt=True) is None
    assert BeispielSchreiben.ausgefuehrt == []
    assert (await _approval(session_fabrik, anfrage.approval_id)).status == "offen"


async def test_abgelaufene_freigabe_wird_nicht_ausgefuehrt(
    freigaben, registry, user, session_fabrik
):
    anfrage = await _anfrage(freigaben, registry, user)
    spaeter = jetzt() + timedelta(minutes=16)
    entscheidung = await freigaben.entscheiden(
        anfrage.approval_id, ERLAUBT_ID, genehmigt=True, zeitpunkt=spaeter
    )
    assert entscheidung.status == "abgelaufen"
    assert BeispielSchreiben.ausgefuehrt == []
    assert (await _approval(session_fabrik, anfrage.approval_id)).status == "abgelaufen"


async def test_kurz_vor_ablauf_gilt_noch(freigaben, registry, user):
    anfrage = await _anfrage(freigaben, registry, user)
    gleich = jetzt() + timedelta(minutes=14)
    await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True, zeitpunkt=gleich)
    assert BeispielSchreiben.ausgefuehrt == ["Hallo"]


async def test_doppelklick_fuehrt_nur_einmal_aus(freigaben, registry, user):
    anfrage = await _anfrage(freigaben, registry, user)
    await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    zweite = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert zweite.status == STATUS_BEREITS_ENTSCHIEDEN
    assert BeispielSchreiben.ausgefuehrt == ["Hallo"]


async def test_unbekannte_freigabe(freigaben, user):
    entscheidung = await freigaben.entscheiden(4711, ERLAUBT_ID, genehmigt=True)
    assert entscheidung.status == STATUS_NICHT_GEFUNDEN


async def test_anzahl_offen_ignoriert_abgelaufene(freigaben, registry, user):
    await _anfrage(freigaben, registry, user)
    assert await freigaben.anzahl_offen() == 1
    assert await freigaben.anzahl_offen(zeitpunkt=jetzt() + timedelta(minutes=16)) == 0


async def test_demo_notiz_speichert_erst_nach_freigabe(kontext, user, session_fabrik):
    registry = lade_registry(kontext)
    freigaben = Freigaben(kontext, registry)
    tool = registry.hole("demo_notiz")
    assert tool.schreibend

    anfrage = await freigaben.anfragen(user, tool, {"text": "Muster bestellen"})
    assert "Muster bestellen" in anfrage.vorschau_text
    async with session_fabrik() as session:
        assert list(await session.scalars(select(Notiz))) == []

    await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    async with session_fabrik() as session:
        (notiz,) = list(await session.scalars(select(Notiz)))
    assert notiz.text == "Muster bestellen"
    assert notiz.user_id == user.id


async def test_demo_notiz_verworfen_speichert_nichts(kontext, user, session_fabrik):
    registry = lade_registry(kontext)
    freigaben = Freigaben(kontext, registry)
    anfrage = await freigaben.anfragen(user, registry.hole("demo_notiz"), {"text": "Nein"})
    await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=False)
    async with session_fabrik() as session:
        assert list(await session.scalars(select(Notiz))) == []
