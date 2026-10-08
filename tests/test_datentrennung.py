"""Strikte Datentrennung: Was Person A gehört, kann niemand sonst lesen oder ändern.

Alle Tests laufen gegen echtes PostgreSQL und als Rolle app_laufzeit, also genau so, wie der
Bot im Betrieb auf die Datenbank zugreift.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.agent.history import lade_verlauf, speichere_austausch
from app.agent.loop import Agent
from app.auth.approvals import STATUS_NICHT_GEFUNDEN, Freigaben
from app.auth.users import aktive_admins, finde_erlaubten_nutzer, uebernehme_bestand
from app.channels.base import EingehendeNachricht
from app.db.models import (
    Approval,
    AuditLog,
    Base,
    Message,
    Notiz,
    TelegramDatei,
    UserMemory,
    UserSecret,
)
from app.db.session import LAUFZEITROLLE, db_sitzung
from app.observability.alerts import Alarme
from app.observability.audit import protokolliere
from app.observability.costs import Kosten
from app.tools.base import ToolKontext
from app.tools.registry import lade_registry
from tests.beispiel_tools.schreibend import BeispielSchreiben
from tests.conftest import ADMIN_ID, ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block

PERSOENLICH = (Message, UserSecret, UserMemory, Approval, Notiz, TelegramDatei, AuditLog)
# Tabellen mit user_id, die bewusst nicht persönlich sind (nur Zuordnung und Zahlen).
NICHT_PERSOENLICH = {"usage", "user_roles"}
GEHEIM = "streng geheimer Inhalt von A"


@pytest.fixture
async def daten_von_a(user, laufzeit_fabrik) -> None:
    """Person A (der Mitarbeiter) legt in jeder persönlichen Tabelle etwas an."""
    async with db_sitzung(laufzeit_fabrik, user) as session:
        session.add(Message(chat_id=1, user_id=user.id, rolle="user", inhalt=GEHEIM))
        session.add(UserMemory(user_id=user.id, inhalt=GEHEIM))
        session.add(
            UserSecret(user_id=user.id, dienst="asana", ciphertext=b"chiffre", nonce=b"nonce")
        )
        session.add(
            Approval(user_id=user.id, tool_name="t", parameter={"x": GEHEIM}, vorschau_text=GEHEIM)
        )
        session.add(Notiz(user_id=user.id, text=GEHEIM))
        session.add(
            TelegramDatei(user_id=user.id, chat_id=1, file_id="f", name=GEHEIM, medientyp="")
        )
        session.add(AuditLog(user_id=user.id, tool_name="t", parameter={"x": GEHEIM}))
        await session.commit()


async def _zeilen(session) -> dict[str, int]:
    return {
        modell.__tablename__: len(list(await session.scalars(select(modell))))
        for modell in PERSOENLICH
    }


# ---------------------------------------------------------------- Rolle und Richtlinien


async def test_der_bot_arbeitet_als_laufzeitrolle_ohne_sonderrechte(laufzeit_fabrik):
    async with laufzeit_fabrik() as session:
        rolle, superuser = (
            await session.execute(text("SELECT current_user, current_setting('is_superuser')"))
        ).one()
        eigenschaften = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        eigene_tabellen = await session.scalar(
            text("SELECT count(*) FROM pg_tables WHERE tableowner = current_user")
        )
    assert rolle == LAUFZEITROLLE == "app_laufzeit"
    assert superuser == "off"
    assert tuple(eigenschaften) == (False, False, False, False)
    assert eigene_tabellen == 0


async def test_row_level_security_ist_auf_allen_persoenlichen_tabellen_erzwungen(engine):
    async with engine.connect() as verbindung:
        zustand = {
            name: (aktiv, erzwungen)
            for name, aktiv, erzwungen in await verbindung.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
                )
            )
        }
    for modell in (*PERSOENLICH,):
        assert zustand[modell.__tablename__] == (True, True), modell.__tablename__
    assert zustand["asana_operationen"] == (True, True)
    # Schutz für die Zukunft: Jede Tabelle mit user_id ist geschützt oder steht bewusst auf
    # der Liste der nicht persönlichen Tabellen.
    for name, tabelle in Base.metadata.tables.items():
        if "user_id" in tabelle.columns and name not in NICHT_PERSOENLICH:
            assert zustand[name] == (True, True), f"{name} hat user_id, aber keine RLS"


async def test_richtlinien_pruefen_lesen_und_schreiben_gegen_die_aktuelle_person(engine):
    async with engine.connect() as verbindung:
        richtlinien = {
            (tabelle, name): (befehl, lesen, schreiben)
            for tabelle, name, befehl, lesen, schreiben in await verbindung.execute(
                text(
                    "SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'public'"
                )
            )
        }
    for tabelle in ("messages", "user_secrets", "user_memory", "approvals"):
        befehl, lesen, schreiben = richtlinien[(tabelle, "nutzer_trennung")]
        assert befehl == "ALL"
        for bedingung in (lesen, schreiben):
            assert "user_id" in bedingung
            assert "current_setting('app.current_user_id'" in bedingung


# ---------------------------------------------------------------- Lesen


async def test_person_a_sieht_ihre_eigenen_daten(daten_von_a, user, laufzeit_fabrik):
    async with db_sitzung(laufzeit_fabrik, user) as session:
        assert set((await _zeilen(session)).values()) == {1}


async def test_person_b_sieht_nichts_von_person_a(daten_von_a, admin, laufzeit_fabrik):
    """B ist hier sogar Admin: Die Rolle im Bot ändert an der Trennung nichts."""
    assert admin.ist_admin
    async with db_sitzung(laufzeit_fabrik, admin) as session:
        assert set((await _zeilen(session)).values()) == {0}
        for tabelle in ("messages", "user_secrets", "user_memory", "approvals", "audit_log"):
            anzahl = await session.scalar(text(f"SELECT count(*) FROM {tabelle}"))
            assert anzahl == 0, tabelle


async def test_ohne_kontext_liefern_persoenliche_tabellen_null_zeilen(daten_von_a, laufzeit_fabrik):
    """Direkte Sitzung als app_laufzeit ohne SET LOCAL: keine Zeile, aber auch kein Fehler."""
    async with laufzeit_fabrik() as session:
        assert set((await _zeilen(session)).values()) == {0}
        for tabelle in ("messages", "user_secrets", "user_memory", "approvals"):
            assert await session.scalar(text(f"SELECT count(*) FROM {tabelle}")) == 0
            zeilen = (await session.execute(text(f"SELECT * FROM {tabelle}"))).all()
            assert zeilen == []


async def test_kontext_gilt_nur_fuer_die_eigene_transaktion(daten_von_a, user, laufzeit_engine):
    """SET LOCAL endet mit der Transaktion; danach ist dieselbe Verbindung wieder blind."""
    async with laufzeit_engine.connect() as verbindung:
        await verbindung.execute(
            text("SELECT set_config('app.current_user_id', :id, true)"), {"id": str(user.id)}
        )
        assert await verbindung.scalar(text("SELECT count(*) FROM messages")) == 1
        await verbindung.commit()
        assert await verbindung.scalar(text("SELECT count(*) FROM messages")) == 0
        await verbindung.rollback()
        assert await verbindung.scalar(text("SELECT count(*) FROM user_secrets")) == 0


async def test_db_sitzung_setzt_den_kontext_auch_nach_einem_commit_neu(user, laufzeit_fabrik):
    async with db_sitzung(laufzeit_fabrik, user) as session:
        session.add(UserMemory(user_id=user.id, inhalt="eins"))
        await session.commit()
        session.add(UserMemory(user_id=user.id, inhalt="zwei"))
        await session.commit()
        assert len(list(await session.scalars(select(UserMemory)))) == 2


def test_db_sitzung_verlangt_eine_person(laufzeit_fabrik):
    for falsch in (None, "1", True, 1.5):
        with pytest.raises(TypeError):
            db_sitzung(laufzeit_fabrik, falsch)


# ---------------------------------------------------------------- Schreiben


async def test_niemand_kann_fuer_eine_andere_person_schreiben(user, admin, laufzeit_fabrik):
    for modell, felder in (
        (Message, {"chat_id": 1, "rolle": "user", "inhalt": "untergeschoben"}),
        (UserMemory, {"inhalt": "untergeschoben"}),
        (UserSecret, {"dienst": "asana", "ciphertext": b"x", "nonce": b"y"}),
        (Approval, {"tool_name": "t", "parameter": {}, "vorschau_text": "v"}),
    ):
        # Als Admin etwas mit der user_id des Mitarbeiters anlegen: Die Datenbank lehnt ab.
        async with db_sitzung(laufzeit_fabrik, admin) as session:
            session.add(modell(user_id=user.id, **felder))
            with pytest.raises(DBAPIError, match="row-level security"):
                await session.commit()
        # Ohne Kontext erst recht.
        async with laufzeit_fabrik() as session:
            session.add(modell(user_id=user.id, **felder))
            with pytest.raises(DBAPIError, match="row-level security"):
                await session.commit()


async def test_fremde_zeilen_lassen_sich_weder_aendern_noch_loeschen(
    daten_von_a, user, admin, laufzeit_fabrik, session_fabrik
):
    async with db_sitzung(laufzeit_fabrik, admin) as session:
        for tabelle in ("messages", "user_memory", "user_secrets", "approvals", "notizen"):
            geloescht = await session.execute(text(f"DELETE FROM {tabelle}"))
            assert geloescht.rowcount == 0, tabelle
        geaendert = await session.execute(text("UPDATE approvals SET status = 'genehmigt'"))
        assert geaendert.rowcount == 0
        await session.commit()
    # Als Eigentümer nachgesehen: Alles von A ist unverändert da.
    async with session_fabrik() as session:
        assert set((await _zeilen(session)).values()) == {1}
        assert (await session.scalar(select(Approval))).status == "offen"


async def test_laufzeitrolle_kann_stammdaten_und_migrationsstand_nicht_aendern(laufzeit_fabrik):
    for anweisung in (
        "SELECT * FROM alembic_version",
        "INSERT INTO roles (name, beschreibung) VALUES ('root', 'x')",
        "DELETE FROM roles",
        "ALTER TABLE messages DISABLE ROW LEVEL SECURITY",
        "DROP POLICY nutzer_trennung ON messages",
        "ALTER ROLE app_laufzeit BYPASSRLS",
    ):
        async with laufzeit_fabrik() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(anweisung))


# ---------------------------------------------------------------- Über den Bot


async def test_verlauf_ist_je_person_getrennt_auch_im_selben_chat(user, admin, laufzeit_fabrik):
    await speichere_austausch(laufzeit_fabrik, 77, user.id, "Frage von A", "Antwort an A")
    await speichere_austausch(laufzeit_fabrik, 77, admin.id, "Frage von B", "Antwort an B")
    assert [m["content"] for m in await lade_verlauf(laufzeit_fabrik, user, 77, 20)] == [
        "Frage von A",
        "Antwort an A",
    ]
    assert [m["content"] for m in await lade_verlauf(laufzeit_fabrik, admin, 77, 20)] == [
        "Frage von B",
        "Antwort an B",
    ]


async def test_claude_bekommt_nur_den_verlauf_der_fragenden_person(
    settings, user, admin, laufzeit_fabrik, alarme
):
    kontext = ToolKontext(settings=settings, session_fabrik=laufzeit_fabrik)
    registry = lade_registry(kontext, paket=__import__("tests.beispiel_tools").beispiel_tools)
    kosten = Kosten(settings, laufzeit_fabrik, alarme)
    client = FakeAnthropic(claude_antwort(text_block("ok")))
    agent = Agent(settings, laufzeit_fabrik, client, registry, Freigaben(kontext, registry), kosten)

    def frage(absender: int, text: str) -> EingehendeNachricht:
        return EingehendeNachricht(chat_id=77, absender_id=absender, absender_name="X", text=text)

    await agent.beantworte(frage(ERLAUBT_ID, GEHEIM), user)
    await agent.beantworte(frage(ADMIN_ID, "Was hat A dir geschrieben?"), admin)

    an_claude = client.aufrufe[1]
    assert GEHEIM not in str(an_claude["messages"]) + str(an_claude["system"])
    assert an_claude["messages"] == [{"role": "user", "content": "Was hat A dir geschrieben?"}]


async def test_freigabe_von_a_kann_b_nicht_bestaetigen(
    settings, user, admin, laufzeit_fabrik, session_fabrik
):
    kontext = ToolKontext(settings=settings, session_fabrik=laufzeit_fabrik)
    registry = lade_registry(kontext, paket=__import__("tests.beispiel_tools").beispiel_tools)
    freigaben = Freigaben(kontext, registry)
    BeispielSchreiben.ausgefuehrt.clear()
    anfrage = await freigaben.anfragen(user, registry.hole("beispiel_schreiben"), {"text": "x"})

    for genehmigt in (True, False):
        versuch = await freigaben.entscheiden(anfrage.approval_id, ADMIN_ID, genehmigt=genehmigt)
        assert versuch.status == STATUS_NICHT_GEFUNDEN
        assert not versuch.abgeschlossen
    assert await freigaben.verwerfen(anfrage.approval_id, "Versuch", admin) is False
    assert BeispielSchreiben.ausgefuehrt == []
    async with session_fabrik() as session:
        assert (await session.get(Approval, anfrage.approval_id)).status == "offen"

    # Die Person selbst kann sie bestätigen.
    eigene = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert eigene.text.startswith("✅")
    assert BeispielSchreiben.ausgefuehrt == ["x"]


async def test_audit_log_ist_nur_fuer_die_eigene_person_lesbar(user, admin, laufzeit_fabrik):
    await protokolliere(laufzeit_fabrik, user_id=user.id, tool_name="t", parameter={"x": GEHEIM})
    await protokolliere(laufzeit_fabrik, user_id=None, tool_name="unbekannt", parameter={})
    async with db_sitzung(laufzeit_fabrik, admin) as session:
        assert list(await session.scalars(select(AuditLog))) == []
    async with db_sitzung(laufzeit_fabrik, user) as session:
        (eintrag,) = list(await session.scalars(select(AuditLog)))
    assert eintrag.parameter == {"x": GEHEIM}


async def test_nicht_persoenliche_zugriffe_gehen_mit_der_laufzeitrolle(
    settings, laufzeit_fabrik, alarm_texte
):
    """Nutzerliste, Rollen, Kosten und Alarme brauchen keinen Kontext und keine Sonderrechte."""
    await uebernehme_bestand(laufzeit_fabrik, settings)
    await uebernehme_bestand(laufzeit_fabrik, settings)
    admin = await finde_erlaubten_nutzer(laufzeit_fabrik, ADMIN_ID, "Theis")
    assert admin.rollen == {"admin"}
    assert [a.telegram_id for a in await aktive_admins(laufzeit_fabrik)] == [ADMIN_ID]

    async def sende(chat_id: int, text: str) -> None:
        alarm_texte.append((chat_id, text))

    alarme = Alarme(laufzeit_fabrik)
    alarme.verbinde(sende)
    kosten = Kosten(settings, laufzeit_fabrik, alarme)
    await kosten.verbuche(admin.id, 0, 100_000)
    assert await kosten.heute_eur() > 0


# ---------------------------------------------------------------- Code-Review als Test

APP = Path(__file__).parent.parent / "app"
# Nur hier darf eine Sitzung ohne Nutzerkontext geöffnet werden. Diese Module lesen und
# schreiben ausschließlich nicht persönliche Tabellen (users, roles, user_roles, usage,
# system_einstellungen) bzw. den Audit-Eintrag ohne Person.
OHNE_KONTEXT_ERLAUBT = {
    "auth/users.py",
    "observability/costs.py",
    "observability/audit.py",
}
PERSOENLICHE_MODELLE = ("Message", "UserSecret", "UserMemory", "Approval", "Notiz", "TelegramDatei")


def _quelltexte() -> dict[str, str]:
    return {str(p.relative_to(APP)): p.read_text() for p in sorted(APP.rglob("*.py"))}


def test_sitzungen_ohne_kontext_gibt_es_nur_in_freigegebenen_modulen():
    muster = re.compile(r"session_fabrik\(\s*\)")
    treffer = {name for name, quelle in _quelltexte().items() if muster.search(quelle)}
    assert treffer <= OHNE_KONTEXT_ERLAUBT, treffer - OHNE_KONTEXT_ERLAUBT


def test_module_ohne_kontext_fassen_keine_persoenlichen_tabellen_an():
    quellen = _quelltexte()
    for name in OHNE_KONTEXT_ERLAUBT - {"observability/audit.py"}:
        for modell in PERSOENLICHE_MODELLE:
            assert not re.search(rf"\b{modell}\b", quellen[name]), f"{name} nutzt {modell}"


def test_der_bot_wechselt_nie_selbst_die_datenbankrolle():
    for name, quelle in _quelltexte().items():
        assert not re.search(r"\b(RE)?SET\s+ROLE\b", quelle, re.IGNORECASE), name
        assert "BYPASSRLS" not in quelle.upper() or name == "db/session.py", name
        assert "session_authorization" not in quelle.lower(), name


def test_kein_tool_schema_laesst_das_modell_eine_person_waehlen(kontext):
    verboten = {
        "nutzer_id",
        "user_id",
        "user",
        "telegram_id",
        "mailbox",
        "postfach",
        "im_namen_von",
    }

    def schluessel(schema: object) -> set[str]:
        if isinstance(schema, dict):
            eigene = set(schema.get("properties", {}))
            return eigene.union(*(schluessel(wert) for wert in schema.values()))
        if isinstance(schema, list):
            return set().union(*(schluessel(wert) for wert in schema)) if schema else set()
        return set()

    tools = lade_registry(kontext).alle()
    assert len(tools) >= 20
    for tool in tools:
        gefunden = schluessel(tool.parameter_schema) & verboten
        assert not gefunden, f"{tool.name}: {gefunden}"
