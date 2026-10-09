"""Rollen und Rechte: Vereinigung, Tool-Auswahl, Ausführung, Admin-Befehle, Rundnachricht."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent.prompts import baue_system_prompt
from app.auth.rechte import RECHTE, ROLLEN_RECHTE, rechte_von
from app.auth.users import aendere_rolle, finde_erlaubten_nutzer, liste_nutzer
from app.channels.base import Antwort, EingehendeNachricht
from app.channels.befehle import Befehle
from app.channels.telegram import TelegramKanal
from app.db.models import ROLLEN, AuditLog
from app.tools.registry import lade_registry
from tests.conftest import ADMIN_ID, ERLAUBT_ID, FREMD_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block

CHAT_ID = 5


# ---------------------------------------------------------------- Rechte


def test_rollen_haben_genau_die_rechte_der_vorgabe():
    assert ROLLEN_RECHTE["mitarbeiter"] == {"asana.lesen", "asana.schreiben", "mail.eigene"}
    assert ROLLEN_RECHTE["buchhaltung"] == {
        "buchhaltung.lesen",
        "buchhaltung.schreiben",
        "mail.eigene",
        "asana.lesen",
    }
    assert ROLLEN_RECHTE["apotheken_updates"] == {
        "apotheken.lesen",
        "apotheken.schreiben",
        "bestand.lesen",
        "asana.lesen",
    }
    assert ROLLEN_RECHTE["admin"] == set(RECHTE)
    assert set(ROLLEN_RECHTE) == set(ROLLEN)
    for recht in (
        "asana.lesen",
        "asana.schreiben",
        "asana.loeschen",
        "asana.api_aufruf",
        "shopify.lesen",
        "shopify.schreiben",
        "bestand.lesen",
        "bestand.schreiben",
        "apotheken.lesen",
        "apotheken.schreiben",
        "klaviyo.entwurf",
        "klaviyo.planen",
        "buchhaltung.lesen",
        "buchhaltung.schreiben",
        "mail.eigene",
        "admin.nutzer",
        "admin.kosten_alle",
    ):
        assert recht in RECHTE, recht


def test_rechte_werden_aus_allen_rollen_vereinigt():
    beide = rechte_von({"buchhaltung", "apotheken_updates"})
    assert beide == ROLLEN_RECHTE["buchhaltung"] | ROLLEN_RECHTE["apotheken_updates"]
    assert "asana.schreiben" not in beide
    assert rechte_von({"mitarbeiter", "buchhaltung"}) >= {"asana.schreiben", "buchhaltung.lesen"}
    assert rechte_von(set()) == frozenset()
    assert rechte_von({"gibt_es_nicht"}) == frozenset()


def test_es_gibt_kein_recht_auf_fremde_daten():
    """Auch der Admin hat kein Recht, mit dem sich Inhalte anderer lesen ließen."""
    for recht in RECHTE:
        assert "fremde" not in recht and "alle_nachrichten" not in recht, recht
        assert recht.split(".")[0] not in ("nachrichten", "verlauf", "secrets", "notizen"), recht
    assert RECHTE["mail.eigene"].endswith("(nie fremde)")
    assert RECHTE["admin.kosten_alle"].endswith("(nur Zahlen)")


async def test_kontext_traegt_rollen_und_rechte_aus_der_datenbank(user, admin, session_fabrik):
    assert user.rollen == {"mitarbeiter"} and user.rechte == ROLLEN_RECHTE["mitarbeiter"]
    assert admin.rechte == set(RECHTE)
    await aendere_rolle(session_fabrik, user.id, "buchhaltung", True, admin.id)
    neu = await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)
    assert neu.rollen == {"mitarbeiter", "buchhaltung"}
    assert neu.darf("asana.schreiben", "buchhaltung.schreiben")
    assert not neu.darf("asana.loeschen")


# ---------------------------------------------------------------- Tool-Auswahl und Ausführung


def test_dem_modell_werden_nur_tools_mit_passenden_rechten_gezeigt(kontext, user, admin):
    registry = lade_registry(kontext)

    def namen(nutzer) -> set[str]:
        return {d["name"] for d in registry.api_definitionen(nutzer)}

    fuer_mitarbeiter = namen(user)
    assert "asana_aufgaben_suchen" in fuer_mitarbeiter
    assert "asana_aenderungen_ausfuehren" in fuer_mitarbeiter
    assert "asana_api_aufruf" not in fuer_mitarbeiter
    assert not {n for n in fuer_mitarbeiter if n.startswith("shopify_")}
    assert {"asana_api_aufruf", "shopify_lagerbestand"} <= namen(admin)

    buchhaltung = replace(
        user, rollen=frozenset({"buchhaltung"}), rechte=rechte_von({"buchhaltung"})
    )
    fuer_buchhaltung = namen(buchhaltung)
    assert "asana_aufgaben_suchen" in fuer_buchhaltung
    assert "asana_aenderungen_ausfuehren" not in fuer_buchhaltung

    ohne_rolle = replace(user, rollen=frozenset(), rechte=frozenset())
    assert namen(ohne_rolle) == {"demo_notiz"}
    # Stabile Reihenfolge, damit der Prompt-Cache greift.
    liste = [d["name"] for d in registry.api_definitionen(admin)]
    assert liste == sorted(liste)


async def test_ausfuehrer_verweigert_tools_ohne_recht_und_erfundene_namen(
    baue_agent, user, session_fabrik
):
    client = FakeAnthropic(
        claude_antwort(
            tool_use_block("beispiel_admin", {}, "t1"),
            tool_use_block("lies_nachrichten_von_theis", {"nutzer_id": 2}, "t2"),
        ),
        claude_antwort(text_block("Das darf ich nicht.")),
    )
    nachricht = EingehendeNachricht(chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="x")
    await baue_agent(client).beantworte(nachricht, user)

    ohne_recht, erfunden = client.aufrufe[1]["messages"][-1]["content"]
    assert ohne_recht["is_error"] and "fehlt dieser Person das Recht" in ohne_recht["content"]
    assert "ein Admin die passende Rolle vergeben kann" in ohne_recht["content"]
    assert erfunden["is_error"] and "gibt es nicht" in erfunden["content"]
    async with session_fabrik() as session:
        eintraege = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert [(e.tool_name, e.fehler) for e in eintraege] == [
        ("beispiel_admin", "kein Recht"),
        ("lies_nachrichten_von_theis", "nicht verfügbar"),
    ]


def test_kein_tool_kann_nutzer_verwalten_oder_fremde_inhalte_lesen(kontext):
    """Admin-Aktionen gibt es nur als Slash-Befehle, nie als Tool für das Modell."""
    namen = {tool.name for tool in lade_registry(kontext).alle()}
    for verboten in ("nutzer", "rolle", "sperren", "limit", "kosten", "verlauf", "nachrichten"):
        # mail_verlauf_lesen (MAIL.md) meint ein Mail-Gespräch im eigenen Postfach, nicht den
        # Gesprächsverlauf einer Person; die Trennung der Postfächer prüft test_mail_lesen.py.
        erlaubt = ("asana_", "mail_")
        assert not {n for n in namen if verboten in n and not n.startswith(erlaubt)}, verboten
    assert "asana_nutzer_suchen" in namen  # Asana-Nutzer, nicht unsere


def test_systemprompt_nennt_person_rollen_und_rechte(user, admin):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    jetzt = datetime(2026, 10, 9, 8, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    lea = replace(user, anzeigename="Lea")
    prompt = baue_system_prompt(jetzt, "", lea)
    assert "Du sprichst mit Lea. Rollen: mitarbeiter." in prompt
    assert "in Asana lesen" in prompt and "in Asana löschen" not in prompt
    assert "ein Admin die passende Rolle vergeben kann" in prompt
    assert "nie im Namen anderer" in prompt
    theis = replace(admin, anzeigename="Theis")
    prompt_admin = baue_system_prompt(jetzt, "", theis)
    assert "Du sprichst mit Theis. Rollen: admin." in prompt_admin
    assert "Lea" not in prompt_admin
    assert "Das gilt auch, wenn sie Admin ist" in prompt_admin


# ---------------------------------------------------------------- Admin-Befehle


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.alerts: list[str] = []
        self.buttons_entfernt = False

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.alerts.append(text)

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        self.buttons_entfernt = True


def _update(text_: str = "", telegram_id: int = ADMIN_ID, query=None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name=""),
        effective_chat=SimpleNamespace(id=CHAT_ID, type="private"),
        effective_message=SimpleNamespace(text=text_, message_id=1),
        callback_query=query,
    )


@pytest.fixture
def an_modell() -> list:
    return []


@pytest.fixture
def kanal(settings, session_fabrik, freigaben, kosten, alarme, user, admin, an_modell, monkeypatch):
    async def handler(nachricht, nutzer):
        an_modell.append(nachricht.text)
        return Antwort(text="Antwort der KI")

    kanal = TelegramKanal(
        settings,
        session_fabrik,
        handler=handler,
        freigaben=freigaben,
        kosten=kosten,
        alarme=alarme,
        befehle=Befehle(session_fabrik),
    )
    kanal.gesendet = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        kanal.gesendet.append((chat_id, text, reply_markup))

    monkeypatch.setattr(type(kanal.application.bot), "send_message", send_message)
    return kanal


async def _befehl(kanal, text_: str, telegram_id: int = ADMIN_ID) -> tuple[str, str | None]:
    """Führt einen Befehl aus und liefert (Antworttext, Kennung des Bestätigungsbuttons)."""
    await kanal._bei_befehl(_update(text_, telegram_id), None)
    _, text, buttons = kanal.gesendet[-1]
    if buttons is None:
        return text, None
    ja, nein = buttons.inline_keyboard[0]
    assert (ja.text, nein.text) == ("✅ Bestätigen", "❌ Abbrechen")
    return text, ja.callback_data.split(":")[1]


async def _klick(kanal, kennung: str, wahl: str = "ja", telegram_id: int = ADMIN_ID) -> FakeQuery:
    query = FakeQuery(f"admin:{kennung}:{wahl}")
    await kanal._bei_admin_klick(_update(telegram_id=telegram_id, query=query), None)
    return query


async def _audit(session_fabrik) -> list[tuple]:
    async with session_fabrik() as session:
        eintraege = await session.scalars(select(AuditLog).order_by(AuditLog.id))
        return [(e.tool_name, e.parameter, e.ergebnis_kurz, e.fehler) for e in eintraege]


async def test_nutzerliste_zeigt_nur_metadaten(kanal, session_fabrik):
    await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID, "Lea Beispiel")
    await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID, "Theis")
    text, kennung = await _befehl(kanal, "/nutzer")
    assert kennung is None
    assert text.splitlines() == [
        "Personen:",
        f"- Lea Beispiel, Telegram-ID {ERLAUBT_ID}, Rollen: mitarbeiter, aktiv",
        f"- Theis, Telegram-ID {ADMIN_ID}, Rollen: admin, aktiv",
    ]


async def test_rolle_vergeben_braucht_bestaetigung_und_steht_im_audit_log(kanal, session_fabrik):
    await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID, "Lea Beispiel")
    text, kennung = await _befehl(kanal, "/rolle Lea +buchhaltung")
    assert text == "Bitte bestätigen: Lea Beispiel bekommt die Rolle buchhaltung"
    # Vor dem Klick ist nichts geändert.
    assert (await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)).rollen == {"mitarbeiter"}

    query = await _klick(kanal, kennung)
    assert query.buttons_entfernt
    assert kanal.gesendet[-1][1] == "✅ Erledigt: Lea Beispiel bekommt die Rolle buchhaltung"
    lea = await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)
    assert lea.rollen == {"mitarbeiter", "buchhaltung"}
    assert await _audit(session_fabrik) == [
        (
            "/rolle",
            {"telegram_id": ERLAUBT_ID, "rolle": "buchhaltung", "hinzufuegen": True},
            "ausgeführt",
            None,
        )
    ]
    # Ein zweiter Klick auf denselben Button ändert nichts mehr.
    await _klick(kanal, kennung)
    assert kanal.gesendet[-1][1] == "Diese Aktion gibt es nicht mehr."

    text, kennung = await _befehl(kanal, f"/rolle {ERLAUBT_ID} -buchhaltung")
    await _klick(kanal, kennung)
    assert (await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)).rollen == {"mitarbeiter"}


async def test_abbrechen_aendert_nichts(kanal, session_fabrik):
    _, kennung = await _befehl(kanal, f"/rolle {ERLAUBT_ID} +buchhaltung")
    await _klick(kanal, kennung, "nein")
    assert kanal.gesendet[-1][1].startswith("Abgebrochen:")
    assert (await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)).rollen == {"mitarbeiter"}
    assert await _audit(session_fabrik) == []


async def test_admin_befehle_sind_nur_fuer_admins(kanal, session_fabrik):
    for befehl in (
        "/nutzer",
        "/nutzer_neu 333 Max",
        f"/rolle {ADMIN_ID} -admin",
        f"/rolle {ERLAUBT_ID} +admin",
        f"/sperren {ADMIN_ID}",
        f"/entsperren {ADMIN_ID}",
    ):
        text, kennung = await _befehl(kanal, befehl, telegram_id=ERLAUBT_ID)
        assert kennung is None, befehl
        assert text == "Dafür fehlt dir das Recht. Die Rolle dafür kann ein Admin vergeben."
    assert (await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)).rollen == {"mitarbeiter"}
    assert len(await liste_nutzer(session_fabrik)) == 2
    assert {eintrag[3] for eintrag in await _audit(session_fabrik)} == {"kein Recht"}


async def test_bestaetigen_kann_nur_der_admin_der_den_befehl_gab(kanal, session_fabrik):
    _, kennung = await _befehl(kanal, f"/rolle {ERLAUBT_ID} +admin")
    query = await _klick(kanal, kennung, telegram_id=ERLAUBT_ID)
    assert query.alerts == ["Das kann nur bestätigen, wer den Befehl gegeben hat."]
    assert not query.buttons_entfernt
    assert (await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID)).rollen == {"mitarbeiter"}
    # Unbekannte werden ganz ignoriert.
    fremd = await _klick(kanal, kennung, telegram_id=FREMD_ID)
    assert fremd.alerts == []


async def test_sperren_stoppt_die_person_sofort(kanal, session_fabrik, an_modell):
    await kanal._bei_nachricht(_update("Hallo", ERLAUBT_ID), None)
    assert an_modell == ["Hallo"]

    _, kennung = await _befehl(kanal, f"/sperren {ERLAUBT_ID}")
    await _klick(kanal, kennung)
    vorher = len(kanal.gesendet)

    # Nachrichten, Befehle und Klicks der gesperrten Person laufen ins Leere.
    await kanal._bei_nachricht(_update("Noch da?", ERLAUBT_ID), None)
    await kanal._bei_befehl(_update("/hilfe", ERLAUBT_ID), None)
    assert an_modell == ["Hallo"]
    assert len(kanal.gesendet) == vorher
    assert await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID) is None
    text, _ = await _befehl(kanal, "/nutzer")
    assert f"Telegram-ID {ERLAUBT_ID}, Rollen: mitarbeiter, gesperrt" in text

    _, kennung = await _befehl(kanal, f"/entsperren {ERLAUBT_ID}")
    await _klick(kanal, kennung)
    await kanal._bei_nachricht(_update("Wieder da", ERLAUBT_ID), None)
    assert an_modell == ["Hallo", "Wieder da"]


async def test_schutz_vor_aussperren(kanal, session_fabrik):
    text, kennung = await _befehl(kanal, f"/sperren {ADMIN_ID}")
    assert (text, kennung) == ("Dich selbst kannst du nicht sperren.", None)
    _, kennung = await _befehl(kanal, f"/rolle {ADMIN_ID} -admin")
    await _klick(kanal, kennung)
    assert kanal.gesendet[-1][1] == "Das ging nicht: Das ist der letzte Admin; die Rolle bleibt."
    assert (await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID)).ist_admin


async def test_neue_person_anlegen(kanal, session_fabrik, an_modell):
    text, kennung = await _befehl(kanal, "/nutzer_neu 333 Max Muster")
    assert text == "Bitte bestätigen: Max Muster (Telegram-ID 333) als Mitarbeiter anlegen"
    assert await finde_erlaubten_nutzer(session_fabrik, 333) is None
    await _klick(kanal, kennung)
    max_ = await finde_erlaubten_nutzer(session_fabrik, 333)
    assert (max_.anzeigename, max_.rollen) == ("Max Muster", {"mitarbeiter"})
    await kanal._bei_nachricht(_update("Hallo", 333), None)
    assert an_modell == ["Hallo"]
    _, kennung = await _befehl(kanal, "/nutzer_neu 333 Nochmal")
    await _klick(kanal, kennung)
    assert "schon angelegt" in kanal.gesendet[-1][1]


async def test_ungueltige_eingaben_der_admin_befehle(kanal, session_fabrik):
    await finde_erlaubten_nutzer(session_fabrik, ERLAUBT_ID, "Max Eins")
    await finde_erlaubten_nutzer(session_fabrik, ADMIN_ID, "Max Zwei")
    for befehl, teil in (
        ("/rolle Max +buchhaltung", "nicht eindeutig"),
        ("/rolle Niemand +buchhaltung", "gibt es nicht"),
        (f"/rolle {ERLAUBT_ID} +chef", "Die Rolle „chef“ gibt es nicht"),
        (f"/rolle {ERLAUBT_ID} buchhaltung", "So geht es"),
        ("/nutzer_neu Max", "So geht es"),
        ("/sperren", "So geht es"),
    ):
        text, kennung = await _befehl(kanal, befehl)
        assert teil in text and kennung is None, befehl


async def test_hilfe_zeigt_nur_was_die_person_darf(kanal):
    fuer_admin, _ = await _befehl(kanal, "/hilfe")
    fuer_mitarbeiter, _ = await _befehl(kanal, "/hilfe", telegram_id=ERLAUBT_ID)
    for befehl in ("/nutzer ", "/nutzer_neu", "/rolle", "/sperren", "/entsperren"):
        assert befehl in fuer_admin and befehl not in fuer_mitarbeiter, befehl
    for befehl in ("/hilfe", "/verbinden", "/trennen", "/verbunden"):
        assert befehl in fuer_admin and befehl in fuer_mitarbeiter
    assert "Deine Rollen: mitarbeiter" in fuer_mitarbeiter
    assert "Wer Root-Zugriff auf den Server hat" in fuer_mitarbeiter
    assert "kein Admin kann sie über den Bot lesen" in fuer_mitarbeiter
    assert "**" not in fuer_admin
    start, _ = await _befehl(kanal, "/start", telegram_id=ERLAUBT_ID)
    assert "/hilfe" in start


async def test_an_rolle_senden_erreicht_nur_aktive_mit_dieser_rolle(
    kanal, session_fabrik, admin, user
):
    await aendere_rolle(session_fabrik, user.id, "buchhaltung", True, admin.id)
    _, kennung = await _befehl(kanal, "/nutzer_neu 333 Max")
    await _klick(kanal, kennung)
    max_ = await finde_erlaubten_nutzer(session_fabrik, 333)
    await aendere_rolle(session_fabrik, max_.id, "buchhaltung", True, admin.id)
    _, kennung = await _befehl(kanal, "/sperren 333")
    await _klick(kanal, kennung)
    kanal.gesendet.clear()

    anzahl = await kanal.an_rolle_senden("buchhaltung", "Bitte Belege für September einreichen.")
    assert anzahl == 1
    assert [(chat, text) for chat, text, _ in kanal.gesendet] == [
        (ERLAUBT_ID, "Bitte Belege für September einreichen.")
    ]
    assert await kanal.an_rolle_senden("apotheken_updates", "x") == 0
