"""Mail, Schritt 6: Mailinhalt im Verlauf nur verschlüsselt und befristet, Bereinigung, Audit."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.agent.gedaechtnis import bereinige_mailinhalt, vergiss_alles
from app.agent.history import (
    MailAblage,
    lade_verlauf,
    lade_verlauf_mit_mail,
    speichere_austausch,
)
from app.agent.loop import Agent
from app.auth.approvals import Freigaben
from app.auth.tresor import Tresor, TresorFehler
from app.auth.zugaenge import Zugaenge
from app.channels.base import EingehendeNachricht
from app.channels.befehle import Befehle
from app.db.models import (
    STATUS_ABGELAUFEN,
    STATUS_ABGELEHNT,
    STATUS_OFFEN,
    Approval,
    AuditLog,
    Message,
    jetzt,
)
from app.db.session import db_sitzung
from app.mail.schutz import (
    ANFANG,
    ENTFERNT,
    KONTEXT_KOPF,
    PLATZHALTER_ENTFERNT,
    PLATZHALTER_VERSCHLUESSELT,
    VERSIEGELT,
)
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block
from tests.mail_fake import baue_mail
from tests.mail_hilfen import ADRESSE_A, PASSWORT_A, Werkzeuge, frage_an, verbinde
from tests.test_zugaenge import _alles_in_der_datenbank

CHAT = 5
INHALTE = ("Rechnung 4711", "Max Lieferant", "lieferant.example", "Freitag zahlen", "1.234,56")


@pytest.fixture
async def aufbau(mkontext, mail_server, user, settings, session_fabrik, kosten):
    mkontext.settings.mail_max_anmeldungen_pro_minute = 1000
    postfach = await verbinde(mkontext, mail_server, user, "lea", ADRESSE_A, PASSWORT_A)
    postfach.lege_ab(
        "INBOX",
        baue_mail(
            "Max Lieferant <max@lieferant.example>",
            ADRESSE_A,
            "Rechnung 4711",
            "Guten Tag,\n\nbitte bis Freitag zahlen: 1.234,56 Euro.\n\nDritter Absatz: Skonto 2 %.",
        ),
        eingang=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    )
    werkzeuge = Werkzeuge(mkontext)
    freigaben = Freigaben(mkontext, werkzeuge.registry)

    def baue(client) -> Agent:
        return Agent(
            settings,
            session_fabrik,
            client,
            werkzeuge.registry,
            freigaben,
            kosten,
            kontext=mkontext,
        )

    kennung = (await werkzeuge.daten("mail_suchen", user, konto="lea"))["treffer"][0]["kennung"]
    return baue, werkzeuge, freigaben, kennung


def _nachricht(text: str) -> EingehendeNachricht:
    return EingehendeNachricht(CHAT, ERLAUBT_ID, "Lea", text)


async def _lies_mail(baue, user, kennung) -> None:
    client = FakeAnthropic(
        claude_antwort(tool_use_block("mail_lesen", {"konto": "lea", "kennung": kennung})),
        claude_antwort(
            text_block("Max Lieferant schickt die Rechnung 4711: 1.234,56 Euro bis Freitag zahlen.")
        ),
    )
    await baue(client).beantworte(_nachricht("Was steht in der Mail vom Lieferanten?"), user)


async def _zeilen(session_fabrik) -> list[Message]:
    async with session_fabrik() as session:
        return list(await session.scalars(select(Message).order_by(Message.id)))


async def test_mailinhalt_liegt_im_verlauf_nur_verschluesselt_und_dient_rueckfragen(
    aufbau, user, admin, session_fabrik, laufzeit_fabrik, settings
):
    baue, _, _, kennung = aufbau
    await _lies_mail(baue, user, kennung)

    frage, kontext, antwort = await _zeilen(session_fabrik)
    # Die eigene Frage der Person steht normal da; Tool-Ergebnis und Antwort nie im Klartext.
    assert (frage.rolle, frage.inhalt) == ("user", "Was steht in der Mail vom Lieferanten?")
    assert frage.inhalt_verschluesselt is None
    for zeile, rolle in ((kontext, "user"), (antwort, "assistant")):
        assert (zeile.rolle, zeile.inhalt) == (rolle, PLATZHALTER_VERSCHLUESSELT)
        assert zeile.inhalt_verschluesselt
    alles = await _alles_in_der_datenbank(session_fabrik)
    for inhalt in INHALTE:
        assert inhalt not in alles, inhalt

    # Rückfrage: Das Modell bekommt den Inhalt entschlüsselt, weiter als Daten von außen umrandet.
    client = FakeAnthropic(claude_antwort(text_block("Im dritten Absatz steht: Skonto 2 %.")))
    await baue(client).beantworte(_nachricht("Und was schreibt er im dritten Absatz?"), user)
    verlauf = client.aufrufe[0]["messages"]
    assert [m["role"] for m in verlauf] == ["user", "assistant", "user"]
    erste = verlauf[0]["content"]
    assert erste.startswith(f"Was steht in der Mail vom Lieferanten?\n\n{KONTEXT_KOPF}\n{ANFANG}")
    assert "Dritter Absatz: Skonto 2 %." in erste
    assert verlauf[1]["content"].startswith("Max Lieferant schickt die Rechnung 4711")
    # Auch die Antwort auf die Rückfrage enthält Mailinhalt und liegt nur verschlüsselt vor.
    zeilen = await _zeilen(session_fabrik)
    assert [z.inhalt for z in zeilen[3:]] == [
        "Und was schreibt er im dritten Absatz?",
        PLATZHALTER_VERSCHLUESSELT,
    ]
    assert "Skonto" not in await _alles_in_der_datenbank(session_fabrik)

    # Niemand sonst kommt an den Inhalt: Für B gibt es die Zeilen nicht, und B's Schlüssel
    # öffnet den Geheimtext von A nicht.
    assert await lade_verlauf(laufzeit_fabrik, admin, CHAT, 20) == []
    async with db_sitzung(laufzeit_fabrik, admin) as session:
        assert list(await session.scalars(select(Message))) == []
        assert list(await session.scalars(select(AuditLog))) == []
    tresor = Tresor.aus_settings(settings)
    with pytest.raises(TresorFehler):
        tresor.entsiegle(admin.id, "verlauf", kontext.inhalt_verschluesselt)
    # Ohne Schlüssel (reines Laden) erscheint nur der Platzhalter.
    ohne = await lade_verlauf(session_fabrik, user, CHAT, 20)
    assert ohne[0]["content"].endswith(PLATZHALTER_ENTFERNT)


async def test_nach_der_frist_ist_der_mailinhalt_ersetzt(
    aufbau, user, session_fabrik, laufzeit_fabrik, settings
):
    baue, _, _, kennung = aufbau
    await _lies_mail(baue, user, kennung)
    tresor = Tresor.aus_settings(settings)
    assert settings.mail_kontext_ttl_stunden == 24

    # Innerhalb der Frist lesbar, eine Minute danach nicht mehr, auch wenn die tägliche
    # Bereinigung noch nicht gelaufen ist.
    kurz_davor = jetzt() + timedelta(hours=23, minutes=59)
    lesbar = await lade_verlauf_mit_mail(session_fabrik, user, CHAT, 20, tresor, 24, kurz_davor)
    assert lesbar.hat_mail and "Freitag zahlen" in lesbar.nachrichten[0]["content"]
    danach = jetzt() + timedelta(hours=24, minutes=1)
    abgelaufen = await lade_verlauf_mit_mail(session_fabrik, user, CHAT, 20, tresor, 24, danach)
    assert not abgelaufen.hat_mail
    assert [m["content"] for m in abgelaufen.nachrichten] == [
        f"Was steht in der Mail vom Lieferanten?\n\n{PLATZHALTER_ENTFERNT}",
        PLATZHALTER_ENTFERNT,
    ]
    assert abgelaufen.nutzer_text == "Was steht in der Mail vom Lieferanten?"

    # Die Bereinigung ersetzt abgelaufenen Inhalt endgültig; frischer bleibt.
    assert await bereinige_mailinhalt(laufzeit_fabrik, 24) == 0
    async with session_fabrik() as session:
        await session.execute(update(Message).values(zeit=jetzt() - timedelta(hours=25)))
        await session.commit()
    await _lies_mail(baue, user, kennung)
    assert await bereinige_mailinhalt(laufzeit_fabrik, 24) == 2
    assert await bereinige_mailinhalt(laufzeit_fabrik, 24) == 0
    zeilen = await _zeilen(session_fabrik)
    assert [(z.inhalt, z.inhalt_verschluesselt is None) for z in zeilen[:3]] == [
        ("Was steht in der Mail vom Lieferanten?", True),
        (PLATZHALTER_ENTFERNT, True),
        (PLATZHALTER_ENTFERNT, True),
    ]
    assert [z.inhalt_verschluesselt is not None for z in zeilen[3:]] == [False, True, True]
    # Das Modell sieht danach nur noch den Platzhalter.
    async with session_fabrik() as session:
        await session.execute(update(Message).values(zeit=jetzt() - timedelta(hours=25)))
        await session.commit()
    assert await bereinige_mailinhalt(laufzeit_fabrik, 24) == 2
    client = FakeAnthropic(claude_antwort(text_block("Das weiß ich nicht mehr.")))
    await baue(client).beantworte(_nachricht("Wie hoch war die Rechnung?"), user)
    frueher = "\n".join(str(m["content"]) for m in client.aufrufe[0]["messages"][:-1])
    assert PLATZHALTER_ENTFERNT in frueher
    for inhalt in INHALTE:
        assert inhalt not in frueher
    # Ohne lesbaren Mailinhalt im Verlauf wird die neue Antwort wieder normal gespeichert.
    assert (await _zeilen(session_fabrik))[-1].inhalt == "Das weiß ich nicht mehr."


async def test_vergessen_entfernt_mailinhalt_und_vorbereitete_mails_sofort(
    aufbau, user, admin, session_fabrik, mkontext
):
    baue, werkzeuge, freigaben, kennung = aufbau
    await _lies_mail(baue, user, kennung)
    anfrage = await frage_an(
        freigaben, werkzeuge, user, "mail_senden", an=["x@y.example"], betreff="B", text="T"
    )
    await speichere_austausch(session_fabrik, CHAT, admin.id, "Frage von B", "Antwort an B")

    befehle = Befehle(session_fabrik, Zugaenge(mkontext))
    antwort = await befehle.fuehre_aus("vergessen", user, [], CHAT, True)
    assert await befehle.bestaetige(antwort.aktion, user, True) is not None

    zeilen = await _zeilen(session_fabrik)
    assert [z.inhalt for z in zeilen] == ["Frage von B", "Antwort an B"]
    async with session_fabrik() as session:
        freigabe = await session.get(Approval, anfrage.approval_id)
    assert (freigabe.parameter, freigabe.status) == (ENTFERNT, STATUS_ABGELEHNT)
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, ERLAUBT_ID, genehmigt=True)
    assert entscheidung.text == "Diese Freigabe wurde bereits entschieden."
    assert await vergiss_alles(session_fabrik, user) == (0, 0)


async def test_abgelaufene_mail_freigaben_werden_geleert(
    aufbau, user, session_fabrik, laufzeit_fabrik, mail_server
):
    _, werkzeuge, freigaben, _ = aufbau
    alt = await frage_an(
        freigaben, werkzeuge, user, "mail_senden", an=["x@y.example"], betreff="B", text="T"
    )
    async with session_fabrik() as session:
        await session.execute(update(Approval).values(erstellt_am=jetzt() - timedelta(minutes=16)))
        await session.commit()
    frisch = await frage_an(
        freigaben, werkzeuge, user, "mail_senden", an=["x@y.example"], betreff="B", text="T"
    )
    await bereinige_mailinhalt(laufzeit_fabrik, 24)
    async with session_fabrik() as session:
        alte = await session.get(Approval, alt.approval_id)
        neue = await session.get(Approval, frisch.approval_id)
    assert (alte.parameter, alte.status) == (ENTFERNT, STATUS_ABGELAUFEN)
    assert VERSIEGELT in neue.parameter and neue.status == STATUS_OFFEN
    assert mail_server.gesendet == []


async def test_auch_eine_vorbereitete_mail_macht_die_antwort_vertraulich(
    aufbau, user, session_fabrik
):
    baue, _, _, _ = aufbau
    client = FakeAnthropic(
        claude_antwort(
            tool_use_block(
                "mail_entwurf_speichern",
                {
                    "konto": "lea",
                    "an": ["kunde@firma.example"],
                    "betreff": "Angebot",
                    "text": "Hallo",
                },
            )
        ),
        claude_antwort(text_block("Der Entwurf „Angebot“ an kunde@firma.example ist vorbereitet.")),
    )
    await baue(client).beantworte(_nachricht("Leg bitte den Entwurf ab"), user)
    frage, antwort = await _zeilen(session_fabrik)
    assert frage.inhalt == "Leg bitte den Entwurf ab"
    assert antwort.inhalt == PLATZHALTER_VERSCHLUESSELT and antwort.inhalt_verschluesselt
    alles = await _alles_in_der_datenbank(session_fabrik)
    assert "kunde@firma.example" not in alles and "Angebot" not in alles


async def test_runden_ohne_mail_bleiben_wie_bisher(aufbau, user, session_fabrik):
    baue, _, _, _ = aufbau
    client = FakeAnthropic(claude_antwort(text_block("Hallo Lea.")))
    await baue(client).beantworte(_nachricht("Hallo"), user)
    assert [(z.inhalt, z.inhalt_verschluesselt) for z in await _zeilen(session_fabrik)] == [
        ("Hallo", None),
        ("Hallo Lea.", None),
    ]


async def test_ohne_schluessel_wird_mailinhalt_gar_nicht_abgelegt(session_fabrik, user):
    await speichere_austausch(
        session_fabrik, CHAT, user.id, "Frage", "Antwort mit Betreff", MailAblage(None, "Mailtext")
    )
    zeilen = await _zeilen(session_fabrik)
    assert [(z.inhalt, z.inhalt_verschluesselt) for z in zeilen] == [
        ("Frage", None),
        (PLATZHALTER_ENTFERNT, None),
        (PLATZHALTER_ENTFERNT, None),
    ]


async def test_der_hintergrundjob_des_kanals_bereinigt_mailinhalt(
    aufbau, user, session_fabrik, freigaben, kosten, alarme, mkontext, caplog
):
    from app.channels.telegram import (
        BEREINIGUNG_ABSTAND_SEKUNDEN,
        MAIL_BEREINIGUNG_ABSTAND_SEKUNDEN,
        TelegramKanal,
    )

    baue, _, _, kennung = aufbau
    await _lies_mail(baue, user, kennung)
    async with session_fabrik() as session:
        await session.execute(update(Message).values(zeit=jetzt() - timedelta(hours=25)))
        await session.commit()

    async def handler(nachricht, nutzer):
        raise AssertionError

    kanal = TelegramKanal(
        mkontext.settings,
        session_fabrik,
        handler=handler,
        freigaben=freigaben,
        kosten=kosten,
        alarme=alarme,
    )
    with caplog.at_level("INFO"):
        await kanal.bereinige(mit_nachrichten=False)
    assert "Mailinhalt im Verlauf: 2 abgelaufene Einträge entfernt" in caplog.text
    assert [z.inhalt_verschluesselt for z in await _zeilen(session_fabrik)] == [None] * 3
    # Mailinhalt stündlich, alte Nachrichten weiter einmal am Tag.
    assert (MAIL_BEREINIGUNG_ABSTAND_SEKUNDEN, BEREINIGUNG_ABSTAND_SEKUNDEN) == (3600, 86400)
    for inhalt in INHALTE:
        assert inhalt not in caplog.text


async def test_audit_log_enthaelt_von_mails_nur_metadaten(aufbau, user, session_fabrik):
    baue, _, _, kennung = aufbau
    await _lies_mail(baue, user, kennung)
    async with session_fabrik() as session:
        audit = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert [(a.tool_name, a.parameter, a.ergebnis_kurz) for a in audit] == [
        ("mail_suchen", {"aktion": "durchsucht", "konto": "lea", "anzahl": 1}, "durchsucht"),
        ("mail_lesen", {"aktion": "gelesen", "konto": "lea", "anzahl": 1}, "gelesen"),
    ]
    assert all(a.zeit and a.user_id == user.id for a in audit)
