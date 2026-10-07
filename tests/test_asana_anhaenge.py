"""Anhänge: Upload als multipart, externer Link, Löschen, Telegram-Eingang für Dateien."""

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.agent.history import lade_verlauf
from app.agent.loop import Agent
from app.auth.approvals import Freigaben
from app.channels.base import Antwort, DateiHinweis, EingehendeNachricht
from app.channels.telegram import TelegramKanal
from app.db.models import AuditLog, Message, TelegramDatei
from app.tools.base import ToolFehler
from app.tools.registry import lade_registry
from tests.asana_fake import ASANA_TOKEN, FakeAsana, asana_kontext
from tests.conftest import ERLAUBT_ID, FREMD_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block
from tests.test_asana_schreiben import aufgabe
from tests.test_fotos import JPEG, FakeFoto

NAME = "asana_aenderungen_ausfuehren"
CHAT_ID = 5
PDF = b"%PDF-1.7\n" + b"vertragsinhalt" * 40


@pytest.fixture
def fake() -> FakeAsana:
    fake = FakeAsana()
    fake.route("GET", "/tasks/7", aufgabe())
    fake.route("GET", "/projects/100", {"gid": "100", "name": "Launch"})
    fake.route("POST", "/attachments", {"gid": "880", "name": "vertrag.pdf"})
    return fake


@pytest.fixture
def akontext(kontext, fake):
    akontext = asana_kontext(kontext, fake)

    async def lader(file_id: str) -> bytes:
        akontext.geladen.append(file_id)
        return PDF

    object.__setattr__(akontext, "geladen", [])
    akontext.dateien.verbinde(lader)
    return akontext


@pytest.fixture
def werkzeug(akontext):
    registry = lade_registry(akontext)
    return registry.hole(NAME), Freigaben(akontext, registry), registry


async def _datei(session_fabrik, user, groesse: int = len(PDF), name: str = "vertrag.pdf") -> str:
    async with session_fabrik() as session:
        eintrag = TelegramDatei(
            user_id=user.id,
            chat_id=CHAT_ID,
            file_id="tg-file-1",
            name=name,
            medientyp="application/pdf",
            groesse=groesse,
        )
        session.add(eintrag)
        await session.commit()
    return f"datei:{eintrag.id}"


async def _freigeben(werkzeug, nutzer, operationen, bestaetigt: bool = False):
    tool, freigaben, _ = werkzeug
    anfrage = await freigaben.anfragen(nutzer, tool, {"operationen": operationen})
    entscheidung = await freigaben.entscheiden(anfrage.approval_id, nutzer.telegram_id, True)
    if bestaetigt:
        assert entscheidung.rueckfrage
        entscheidung = await freigaben.entscheiden(
            anfrage.approval_id, nutzer.telegram_id, True, bestaetigt=True
        )
    return anfrage, entscheidung


def _formular(anfrage: httpx.Request) -> tuple[str, bytes]:
    return anfrage.headers["content-type"], anfrage.content


# ---------------------------------------------------------------- Operationen


async def test_datei_upload_ist_multipart_mit_parent_file_und_name(
    werkzeug, akontext, user, fake, session_fabrik
):
    verweis = await _datei(session_fabrik, user)
    anfrage, entscheidung = await _freigeben(
        werkzeug, user, [{"operation": "anhang_hinzufuegen", "aufgabe_gid": "7", "datei": verweis}]
    )

    assert anfrage.vorschau_text.splitlines()[1] == (
        "1. Anhängen: Datei „vertrag.pdf“ (1 KB) an Aufgabe „Etiketten“"
    )
    assert entscheidung.text.startswith("✅ Asana-Änderungssatz ausgeführt: 1 angelegt.")
    assert akontext.geladen == ["tg-file-1"]
    (upload,) = [a for a in fake.anfragen if a.method == "POST"]
    typ, koerper = _formular(upload)
    assert typ.startswith("multipart/form-data; boundary=")
    assert b'name="parent"\r\n\r\n7\r\n' in koerper
    assert b'name="name"\r\n\r\nvertrag.pdf\r\n' in koerper
    assert b'name="file"; filename="vertrag.pdf"\r\nContent-Type: application/pdf' in koerper
    assert PDF in koerper
    assert b'"data"' not in koerper
    assert upload.headers["Authorization"] == f"Bearer {ASANA_TOKEN}"


async def test_externer_link_wird_als_external_gesendet(werkzeug, user, fake):
    anfrage, _ = await _freigeben(
        werkzeug,
        user,
        [
            {
                "operation": "anhang_hinzufuegen",
                "projekt": "100",
                "url": "https://example.com/briefing",
                "name": "Briefing",
            }
        ],
    )
    assert anfrage.vorschau_text.splitlines()[1] == (
        "1. Anhängen: Link „Briefing“ (https://example.com/briefing) an Projekt „Launch“"
    )
    (upload,) = [a for a in fake.anfragen if a.method == "POST"]
    typ, koerper = _formular(upload)
    assert typ.startswith("multipart/form-data")
    for teil in (
        b'name="parent"\r\n\r\n100\r\n',
        b'name="resource_subtype"\r\n\r\nexternal\r\n',
        b'name="url"\r\n\r\nhttps://example.com/briefing\r\n',
        b'name="name"\r\n\r\nBriefing\r\n',
    ):
        assert teil in koerper
    assert b'name="file"' not in koerper


async def test_anhang_an_neue_aufgabe_des_satzes(werkzeug, user, fake, session_fabrik):
    verweis = await _datei(session_fabrik, user)
    fake.route("POST", "/tasks", {"gid": "930"})
    fake.route("GET", "/tasks/930", aufgabe("930", "Vertrag prüfen"))
    anfrage, _ = await _freigeben(
        werkzeug,
        user,
        [
            {"operation": "aufgabe_anlegen", "name": "Vertrag prüfen", "platzhalter": "$a1"},
            {
                "operation": "anhang_hinzufuegen",
                "aufgabe_gid": "$a1",
                "datei": verweis,
                "name": "v.pdf",
            },
        ],
    )
    assert "Datei „v.pdf“ (1 KB) an Aufgabe „Vertrag prüfen“ (neu)" in anfrage.vorschau_text
    upload = [a for a in fake.anfragen if a.url.path.endswith("/attachments")][0]
    assert b'name="parent"\r\n\r\n930\r\n' in upload.content
    assert b'filename="v.pdf"' in upload.content


@pytest.mark.parametrize(
    ("op", "meldung"),
    [
        ({"aufgabe_gid": "7"}, "genau eines angeben: „datei“ oder „url“"),
        (
            {"aufgabe_gid": "7", "datei": "datei:1", "url": "https://x.de", "name": "x"},
            "genau eines",
        ),
        ({"datei": "datei:1"}, "„aufgabe_gid“ oder „projekt“"),
        ({"aufgabe_gid": "7", "projekt": "100", "url": "https://x.de", "name": "x"}, "genau eines"),
        ({"aufgabe_gid": "7", "url": "https://x.de"}, "braucht einen „name“"),
        ({"aufgabe_gid": "7", "url": "javascript:alert(1)", "name": "x"}, "https://"),
        ({"aufgabe_gid": "7", "datei": "datei:999"}, "gibt es nicht"),
        ({"aufgabe_gid": "7", "datei": "/etc/passwd"}, "gibt es nicht"),
    ],
)
async def test_ungueltige_anhaenge_fallen_in_der_vorschau_auf(werkzeug, user, fake, op, meldung):
    tool, freigaben, _ = werkzeug
    with pytest.raises(ToolFehler, match=meldung):
        await freigaben.anfragen(
            user, tool, {"operationen": [{"operation": "anhang_hinzufuegen", **op}]}
        )
    assert fake.aufrufe("POST") == []


async def test_datei_eines_anderen_nutzers_ist_tabu(werkzeug, user, admin, fake, session_fabrik):
    verweis = await _datei(session_fabrik, admin)
    tool, freigaben, _ = werkzeug
    op = {"operation": "anhang_hinzufuegen", "aufgabe_gid": "7", "datei": verweis}
    with pytest.raises(ToolFehler, match="gibt es nicht"):
        await freigaben.anfragen(user, tool, {"operationen": [op]})
    await freigaben.anfragen(admin, tool, {"operationen": [op]})


async def test_datei_ueber_20_mb_wird_mit_klarer_meldung_abgelehnt(
    werkzeug, user, fake, session_fabrik
):
    verweis = await _datei(session_fabrik, user, groesse=21 * 1024 * 1024)
    tool, freigaben, _ = werkzeug
    with pytest.raises(ToolFehler, match="größer als 20 MB.*direkt in Asana hochladen"):
        await freigaben.anfragen(
            user,
            tool,
            {
                "operationen": [
                    {"operation": "anhang_hinzufuegen", "aufgabe_gid": "7", "datei": verweis}
                ]
            },
        )


async def test_upload_wird_bei_netzwerkfehler_nicht_wiederholt(
    werkzeug, user, fake, session_fabrik
):
    verweis = await _datei(session_fabrik, user)
    fake.route("POST", "/attachments", httpx.ReadTimeout("zu langsam"))
    _, entscheidung = await _freigeben(
        werkzeug, user, [{"operation": "anhang_hinzufuegen", "aufgabe_gid": "7", "datei": verweis}]
    )
    assert "Ob die Änderung angekommen ist, ist unklar" in entscheidung.text
    assert fake.aufrufe("POST") == [("POST", "/attachments")]


async def test_anhang_loeschen_zaehlt_als_loeschung(werkzeug, user, admin, fake, session_fabrik):
    fake.route(
        "GET", "/attachments/55", {"gid": "55", "name": "alt.pdf", "parent": {"name": "Etiketten"}}
    )
    fake.route("DELETE", "/attachments/55", {})
    op = {"operation": "anhang_loeschen", "anhang_gid": "55"}
    tool, freigaben, _ = werkzeug
    with pytest.raises(ToolFehler, match="darf in Asana nichts löschen"):
        await freigaben.anfragen(user, tool, {"operationen": [op]})

    anfrage, entscheidung = await _freigeben(werkzeug, admin, [op], bestaetigt=True)
    assert anfrage.vorschau_text.splitlines() == [
        "Asana-Änderungssatz: 1 🗑 löschen",
        "1. 🗑 Löschen: Anhang „alt.pdf“ von „Etiketten“",
    ]
    assert entscheidung.text.startswith("✅ Asana-Änderungssatz ausgeführt: 1 🗑 gelöscht.")
    assert fake.aufrufe("DELETE") == [("DELETE", "/attachments/55")]


# ---------------------------------------------------------------- Telegram-Eingang


def _dokument(name: str = "vertrag.pdf", groesse: int = len(PDF)) -> SimpleNamespace:
    return SimpleNamespace(
        file_id="tg-doc-1", file_name=name, mime_type="application/pdf", file_size=groesse
    )


def _update(dokument=None, fotos=(), telegram_id=ERLAUBT_ID, text=None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name="Test Nutzer"),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_message=SimpleNamespace(
            photo=tuple(fotos), document=dokument, caption=text, media_group_id=None, message_id=1
        ),
    )


@pytest.fixture
def empfangen() -> list[EingehendeNachricht]:
    return []


@pytest.fixture
def gesendet() -> list[str]:
    return []


@pytest.fixture
def baue_kanal(akontext, session_fabrik, kosten, alarme, user, empfangen, gesendet, werkzeug):
    def _baue(handler=None) -> TelegramKanal:
        async def standard(nachricht, user):
            empfangen.append(nachricht)
            return Antwort(text="ok")

        kanal = TelegramKanal(
            akontext.settings,
            session_fabrik,
            handler=handler or standard,
            freigaben=werkzeug[1],
            kosten=kosten,
            alarme=alarme,
        )

        async def sende_antwort(chat_id: int, text: str) -> None:
            gesendet.append(text)

        async def sende_freigabe_anfrage(chat_id: int, anfrage) -> None:
            gesendet.append(f"FREIGABE {anfrage.approval_id}: {anfrage.vorschau_text}")

        kanal.sende_antwort = sende_antwort
        kanal.sende_freigabe_anfrage = sende_freigabe_anfrage
        return kanal

    return _baue


async def test_dokument_wird_als_verweis_uebergeben_und_nicht_heruntergeladen(
    baue_kanal, empfangen, akontext, session_fabrik, user
):
    await baue_kanal()._bei_foto(_update(_dokument(), text="Häng das an Aufgabe Etiketten"), None)

    (nachricht,) = empfangen
    assert nachricht.text == "Häng das an Aufgabe Etiketten"
    assert nachricht.bilder == ()
    (hinweis,) = nachricht.dateien
    assert hinweis == DateiHinweis(hinweis.verweis, "vertrag.pdf", "application/pdf", len(PDF))
    assert akontext.geladen == []
    async with session_fabrik() as session:
        (eintrag,) = list(await session.scalars(select(TelegramDatei)))
    assert hinweis.verweis == f"datei:{eintrag.id}"
    assert (eintrag.user_id, eintrag.chat_id, eintrag.file_id) == (user.id, CHAT_ID, "tg-doc-1")


async def test_dokument_ueber_20_mb_wird_gleich_abgelehnt(
    baue_kanal, empfangen, gesendet, session_fabrik
):
    await baue_kanal()._bei_foto(_update(_dokument("film.mov", 25 * 1024 * 1024)), None)
    assert empfangen == []
    (text,) = gesendet
    assert "„film.mov“ ist größer als 20 MB" in text
    async with session_fabrik() as session:
        assert list(await session.scalars(select(TelegramDatei))) == []


async def test_dokument_von_unbekanntem_nutzer_wird_ignoriert(
    baue_kanal, empfangen, gesendet, session_fabrik
):
    await baue_kanal()._bei_foto(_update(_dokument(), telegram_id=FREMD_ID), None)
    assert empfangen == [] and gesendet == []
    async with session_fabrik() as session:
        assert list(await session.scalars(select(TelegramDatei))) == []


async def test_foto_ist_zugleich_bild_fuer_claude_und_anhaengbare_datei(baue_kanal, empfangen):
    foto = FakeFoto(800, 600)
    foto.file_id = "tg-foto-1"
    await baue_kanal()._bei_foto(_update(fotos=[foto], text="häng das an"), None)
    (nachricht,) = empfangen
    assert len(nachricht.bilder) == 1
    (hinweis,) = nachricht.dateien
    assert (hinweis.name, hinweis.medientyp, hinweis.groesse) == (
        "foto_1.jpg",
        "image/jpeg",
        len(JPEG),
    )


async def test_vom_dokument_im_chat_bis_zum_upload_in_asana(
    baue_kanal, akontext, werkzeug, fake, session_fabrik, kosten, gesendet, user, monkeypatch
):
    """Der ganze Weg: Datei schicken, Claude schlägt den Anhang vor, ✅, Upload."""
    client = FakeAnthropic(
        claude_antwort(
            tool_use_block(
                NAME,
                {
                    "operationen": [
                        {"operation": "anhang_hinzufuegen", "aufgabe_gid": "7", "datei": "datei:1"}
                    ]
                },
            )
        ),
        claude_antwort(text_block("Ich hänge die Datei an, sobald du freigibst.")),
    )
    _, freigaben, registry = werkzeug
    agent = Agent(akontext.settings, session_fabrik, client, registry, freigaben, kosten)
    kanal = baue_kanal(handler=agent.beantworte)
    akontext.dateien.verbinde(kanal.lade_datei)

    async def get_file(self, file_id):
        async def download_as_bytearray():
            return bytearray(PDF)

        assert file_id == "tg-doc-1"
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    monkeypatch.setattr(type(kanal.application.bot), "get_file", get_file)
    await kanal._bei_foto(_update(_dokument(), text="Häng das an die Aufgabe Etiketten"), None)

    # Claude sieht den Verweis, aber nicht den Inhalt der Datei.
    frage = client.aufrufe[0]["messages"][-1]["content"]
    assert isinstance(frage, str)
    assert "Häng das an die Aufgabe Etiketten" in frage
    assert "- datei:1: „vertrag.pdf“ (application/pdf, 1 KB)" in frage
    assert "vertragsinhalt" not in frage
    assert fake.aufrufe("POST") == []
    assert gesendet[-1].startswith("FREIGABE 1: Asana-Änderungssatz: 1 anlegen")

    entscheidung = await freigaben.entscheiden(1, ERLAUBT_ID, genehmigt=True)

    assert entscheidung.text.startswith("✅")
    (upload,) = [a for a in fake.anfragen if a.method == "POST"]
    assert PDF in upload.content
    # Im Verlauf und im Audit-Log steht der Verweis, nie der Inhalt.
    verlauf = await lade_verlauf(session_fabrik, CHAT_ID, 20)
    assert "datei:1" in verlauf[0]["content"]
    async with session_fabrik() as session:
        gespeichert = [str(m.inhalt) for m in await session.scalars(select(Message))]
        gespeichert += [
            f"{a.parameter}{a.ergebnis_kurz}" for a in await session.scalars(select(AuditLog))
        ]
    assert all("vertragsinhalt" not in eintrag for eintrag in gespeichert)
