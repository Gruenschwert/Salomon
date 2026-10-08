"""Foto-Eingang: nachgebaute Telegram-Updates, keine echten Downloads."""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent.history import lade_verlauf
from app.agent.loop import FOTO_OHNE_TEXT
from app.channels.base import Antwort
from app.channels.telegram import (
    FOTO_UNLESBAR_TEXT,
    ZU_VIELE_FOTOS_TEXT,
    TelegramKanal,
    groesstes_foto,
    medientyp,
)
from app.db.models import AuditLog, Message
from tests.conftest import ERLAUBT_ID, FREMD_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block

CHAT_ID = 5
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bilddaten" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bilddaten" * 20


class FakeFoto:
    """Eine Größe eines Telegram-Fotos; merkt sich, ob sie heruntergeladen wurde."""

    def __init__(self, breite: int, hoehe: int, daten: bytes = JPEG, file_size: int | None = None):
        self.width = breite
        self.height = hoehe
        self.daten = daten
        self.file_size = len(daten) if file_size is None else file_size
        self.geladen = 0

    async def get_file(self):
        async def download_as_bytearray() -> bytearray:
            self.geladen += 1
            return bytearray(self.daten)

        return SimpleNamespace(download_as_bytearray=download_as_bytearray)


def _update(
    fotos: list[FakeFoto],
    telegram_id: int = ERLAUBT_ID,
    text: str | None = None,
    album: str | None = None,
    message_id: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name="Test Nutzer"),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_message=SimpleNamespace(
            photo=tuple(fotos), caption=text, media_group_id=album, message_id=message_id
        ),
    )


@pytest.fixture
def empfangen() -> list:
    return []


@pytest.fixture
def gesendet() -> list[str]:
    return []


@pytest.fixture
def baue_kanal(settings, session_fabrik, freigaben, kosten, alarme, user, empfangen, gesendet):
    def _baue(handler=None, **settings_werte) -> TelegramKanal:
        async def standard(nachricht, user):
            empfangen.append(nachricht)
            return Antwort(text=f"{len(nachricht.bilder)} Foto(s) gelesen")

        kanal = TelegramKanal(
            settings.model_copy(update=settings_werte),
            session_fabrik,
            handler=handler or standard,
            freigaben=freigaben,
            kosten=kosten,
            alarme=alarme,
        )

        async def sende_antwort(chat_id: int, text: str) -> None:
            gesendet.append(text)

        kanal.sende_antwort = sende_antwort
        kanal.album_wartezeit = 0.01
        return kanal

    return _baue


def test_medientyp_wird_am_inhalt_erkannt():
    assert medientyp(JPEG) == "image/jpeg"
    assert medientyp(PNG) == "image/png"
    assert medientyp(b"GIF89a....") == "image/gif"
    assert medientyp(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert medientyp(b"%PDF-1.7") is None


async def test_groesstes_bild_wird_gewaehlt_und_uebergeben(baue_kanal, empfangen, gesendet):
    klein, mittel, gross = FakeFoto(90, 60), FakeFoto(320, 240), FakeFoto(1280, 960, PNG)
    assert groesstes_foto([mittel, gross, klein]) is gross

    await baue_kanal()._bei_foto(_update([klein, gross, mittel], text="Plan KW 42"), None)

    assert (klein.geladen, mittel.geladen, gross.geladen) == (0, 0, 1)
    (nachricht,) = empfangen
    assert nachricht.text == "Plan KW 42"
    (bild,) = nachricht.bilder
    assert bild.medientyp == "image/png"
    assert bild.daten == PNG
    assert gesendet == ["1 Foto(s) gelesen"]
    # Bilddaten tauchen auch in der Textdarstellung der Nachricht nicht auf.
    assert "png-bilddaten" not in repr(nachricht)


async def test_zu_grosses_foto_wird_hoeflich_abgelehnt(baue_kanal, empfangen, gesendet):
    foto = FakeFoto(4000, 3000, file_size=6 * 1024 * 1024)
    await baue_kanal()._bei_foto(_update([foto]), None)

    assert empfangen == []
    assert foto.geladen == 0
    (text,) = gesendet
    assert "größer als 5 MB" in text
    assert "kleineres Foto" in text


async def test_groessenlimit_ist_konfigurierbar_und_gilt_auch_nach_dem_download(
    baue_kanal, empfangen, gesendet
):
    # Telegram nennt keine Größe; erst die heruntergeladenen Daten zeigen sie.
    foto = FakeFoto(800, 600, JPEG + b"x" * 2048, file_size=0)
    await baue_kanal(photo_max_mb=0.001)._bei_foto(_update([foto]), None)
    assert empfangen == []
    assert "größer als 0.001 MB" in gesendet[0]


async def test_unlesbares_format_wird_abgelehnt(baue_kanal, empfangen, gesendet):
    await baue_kanal()._bei_foto(_update([FakeFoto(10, 10, b"%PDF-1.7 kein Bild")]), None)
    assert empfangen == []
    assert gesendet == [FOTO_UNLESBAR_TEXT]


async def test_foto_von_unbekanntem_nutzer_wird_ignoriert(
    baue_kanal, empfangen, gesendet, session_fabrik
):
    foto = FakeFoto(800, 600)
    await baue_kanal()._bei_foto(_update([foto], telegram_id=FREMD_ID), None)

    assert foto.geladen == 0
    assert empfangen == []
    assert gesendet == []
    async with session_fabrik() as session:
        (eintrag,) = list(await session.scalars(select(AuditLog)))
    assert eintrag.tool_name == "unbekannt"


async def test_album_wird_als_eine_anfrage_mit_hoechstens_fuenf_fotos_uebergeben(
    baue_kanal, empfangen, gesendet
):
    kanal = baue_kanal()
    # Länger als die Datenbankzugriffe zwischen zwei Fotos dauern.
    kanal.album_wartezeit = 0.3
    fotos = [FakeFoto(800, 600, JPEG + bytes([n])) for n in range(6)]
    # Telegram liefert die Fotos eines Albums als einzelne Nachrichten, nicht immer in Reihenfolge.
    for nummer in (2, 0, 1, 3, 5, 4):
        text = "Wochenplan" if nummer == 0 else None
        await kanal._bei_foto(
            _update([fotos[nummer]], text=text, album="g1", message_id=100 + nummer), None
        )
    assert empfangen == []
    # Auf den Abschluss des Albums warten statt auf eine feste Zeit.
    await asyncio.gather(*(album.aufgabe for album in list(kanal._alben.values())))

    (nachricht,) = empfangen
    assert nachricht.text == "Wochenplan"
    assert [bild.daten for bild in nachricht.bilder] == [foto.daten for foto in fotos[:5]]
    assert fotos[5].geladen == 0
    assert gesendet == [ZU_VIELE_FOTOS_TEXT, "5 Foto(s) gelesen"]
    assert kanal._alben == {}


async def test_fehler_bei_der_foto_verarbeitung_ergibt_neutrale_meldung(
    baue_kanal, gesendet, alarm_texte
):
    async def kaputt(nachricht, user):
        raise RuntimeError("geheimes internes Detail")

    await baue_kanal(handler=kaputt)._bei_foto(_update([FakeFoto(800, 600)]), None)
    assert len(gesendet) == 1
    assert "interner Fehler" in gesendet[0]
    assert "geheimes" not in alarm_texte[0][1]


async def test_foto_geht_als_base64_an_claude_aber_nie_in_die_datenbank(
    baue_kanal, baue_agent, session_fabrik, gesendet, user
):
    client = FakeAnthropic(claude_antwort(text_block("Erkannt: 3 Aufgaben")))
    kanal = baue_kanal(handler=baue_agent(client).beantworte)

    await kanal._bei_foto(_update([FakeFoto(800, 600)], text="Bitte anlegen"), None)
    await kanal._bei_foto(_update([FakeFoto(800, 600, PNG)]), None)

    assert gesendet == ["Erkannt: 3 Aufgaben"] * 2
    bild, text = client.aufrufe[0]["messages"][-1]["content"]
    assert bild == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(JPEG).decode(),
        },
    }
    assert text == {"type": "text", "text": "Bitte anlegen"}
    zweiter = client.aufrufe[1]["messages"]
    assert zweiter[-1]["content"][0]["source"]["media_type"] == "image/png"
    assert zweiter[-1]["content"][1]["text"] == FOTO_OHNE_TEXT
    # Schon beim zweiten Aufruf steht vom ersten Foto nur noch der Platzhalter im Verlauf.
    assert zweiter[0] == {"role": "user", "content": "[Foto] Bitte anlegen"}

    assert [m["content"] for m in await lade_verlauf(session_fabrik, user, CHAT_ID, 20)] == [
        "[Foto] Bitte anlegen",
        "Erkannt: 3 Aufgaben",
        "[Foto]",
        "Erkannt: 3 Aufgaben",
    ]
    async with session_fabrik() as session:
        gespeichert = [str(m.inhalt) for m in await session.scalars(select(Message))]
        gespeichert += [
            str(a.parameter) + a.ergebnis_kurz for a in await session.scalars(select(AuditLog))
        ]
    for marke in ("bilddaten", base64.standard_b64encode(JPEG).decode()[:40], "base64"):
        assert all(marke not in eintrag for eintrag in gespeichert)
