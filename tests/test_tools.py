from sqlalchemy import select

from app.db.models import AuditLog
from app.observability.audit import bereinige
from app.tools.registry import MAX_ERGEBNIS_ZEICHEN, fuehre_tool_aus, kuerze, lade_registry
from tests.conftest import ERLAUBT_ID


async def _audit(session_fabrik) -> list[AuditLog]:
    async with session_fabrik() as session:
        return list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))


def test_registry_findet_alle_tools_im_paket(registry):
    assert [tool.name for tool in registry.alle()] == [
        "beispiel_admin",
        "beispiel_fehler",
        "beispiel_kaputt",
        "beispiel_lesen",
        "beispiel_schreiben",
    ]


def test_echte_registry_laedt_ohne_fehler(kontext):
    namen = {tool.name for tool in lade_registry(kontext).alle()}
    assert "BasisTool" not in namen


def test_api_definitionen_beachten_rechte(registry, user, admin):
    fuer_user = {d["name"] for d in registry.api_definitionen(user)}
    fuer_admin = {d["name"] for d in registry.api_definitionen(admin)}
    assert "beispiel_admin" not in fuer_user
    assert "beispiel_admin" in fuer_admin
    definition = registry.api_definitionen(user)[0]
    assert set(definition) == {"name", "description", "input_schema"}


async def test_ausfuehrung_schreibt_audit_log(registry, kontext, user, session_fabrik):
    ergebnis = await fuehre_tool_aus(
        registry.hole("beispiel_lesen"), {"text": "Hallo"}, user, kontext
    )
    assert not ergebnis.fehler
    assert '"echo": "Hallo"' in ergebnis.text
    assert f'"nutzer": {ERLAUBT_ID}' in ergebnis.text
    (eintrag,) = await _audit(session_fabrik)
    assert eintrag.tool_name == "beispiel_lesen"
    assert eintrag.user_id == user.id
    assert eintrag.parameter == {"text": "Hallo"}
    assert eintrag.fehler is None


async def test_unerwarteter_fehler_wird_zu_fehlertext(registry, kontext, user, session_fabrik):
    ergebnis = await fuehre_tool_aus(registry.hole("beispiel_kaputt"), {}, user, kontext)
    assert ergebnis.fehler
    assert "geheimes" not in ergebnis.text
    (eintrag,) = await _audit(session_fabrik)
    assert eintrag.fehler == "RuntimeError"


async def test_erwartbarer_fehler_wird_weitergegeben(registry, kontext, user, session_fabrik):
    ergebnis = await fuehre_tool_aus(registry.hole("beispiel_fehler"), {}, user, kontext)
    assert ergebnis.fehler
    assert "Shop nicht konfiguriert" in ergebnis.text


async def test_falsche_parameter_fuehren_nicht_zum_absturz(registry, kontext, user):
    ergebnis = await fuehre_tool_aus(registry.hole("beispiel_lesen"), {"falsch": 1}, user, kontext)
    assert ergebnis.fehler


def test_kuerze():
    assert kuerze("kurz") == "kurz"
    lang = kuerze("x" * 20000)
    assert len(lang) == MAX_ERGEBNIS_ZEICHEN
    assert lang.endswith("[gekürzt]")


def test_bereinige_maskiert_secrets_und_kuerzt():
    sauber = bereinige({"suche": "Öl", "api_key": "abc", "tief": {"Token": "x"}, "lang": "y" * 999})
    assert sauber["suche"] == "Öl"
    assert sauber["api_key"] == "***"
    assert sauber["tief"]["Token"] == "***"
    assert len(sauber["lang"]) < 400
