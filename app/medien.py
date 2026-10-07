"""Erkennt Dateitypen am Inhalt, unabhängig von Dateiname oder Absenderangabe."""

PDF = "application/pdf"


def bild_medientyp(daten: bytes) -> str | None:
    """Medientyp eines Bildes; None bei Formaten, die Claude nicht als Bild liest."""
    if daten.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if daten.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if daten.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if daten[:4] == b"RIFF" and daten[8:12] == b"WEBP":
        return "image/webp"
    return None


def lesbarer_medientyp(daten: bytes) -> str | None:
    """Bild oder PDF – alles, was Claude direkt ansehen kann."""
    if daten.startswith(b"%PDF-"):
        return PDF
    return bild_medientyp(daten)


def groesse_text(anzahl_bytes: int | None) -> str:
    if anzahl_bytes is None:
        return "unbekannt"
    if anzahl_bytes < 1024 * 1024:
        return f"{max(1, round(anzahl_bytes / 1024))} KB"
    return f"{anzahl_bytes / (1024 * 1024):.1f} MB".replace(".", ",")
