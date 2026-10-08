"""Datenbank-Modelle (Bauplan Abschnitt 7 und MEHRBENUTZER.md, plus `notizen` für das Demo-Tool und
`asana_operationen` für den Ausführungsstand von Asana-Änderungssätzen und
`telegram_dateien` für Verweise auf Dateien aus dem Chat)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

ROLLE_ADMIN = "admin"
ROLLE_MITARBEITER = "mitarbeiter"
ROLLE_BUCHHALTUNG = "buchhaltung"
ROLLE_APOTHEKEN_UPDATES = "apotheken_updates"
ROLLEN = {
    ROLLE_ADMIN: "Verwaltung von Nutzern, Rollen und Kosten",
    ROLLE_MITARBEITER: "Standardrolle für Mitarbeiter",
    ROLLE_BUCHHALTUNG: "Buchhaltung",
    ROLLE_APOTHEKEN_UPDATES: "Apotheken-Scan und Sortenabgleich",
}
STANDARD_ZEITZONE = "Europe/Berlin"
STANDARD_TON = "du"

STATUS_OFFEN = "offen"
# Erstes ✅ ist da, die zweite Rückfrage (Löschen) steht noch aus.
STATUS_BESTAETIGUNG = "bestätigung"
STATUS_GENEHMIGT = "genehmigt"
STATUS_ABGELEHNT = "abgelehnt"
STATUS_ABGELAUFEN = "abgelaufen"


OP_OFFEN = "offen"
OP_LAEUFT = "läuft"
OP_ERLEDIGT = "erledigt"
OP_FEHLGESCHLAGEN = "fehlgeschlagen"
OP_NICHT_AUSGEFUEHRT = "nicht ausgeführt"


def jetzt() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """Zeitstempel mit Zeitzone; liefert auch dann UTC, wenn die Datenbank sie nicht speichert."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    anzeigename: Mapped[str] = mapped_column(String(200), default="")
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True)
    zeitzone: Mapped[str] = mapped_column(String(60), default=STANDARD_ZEITZONE)
    ton: Mapped[str] = mapped_column(String(10), default=STANDARD_TON)
    # Eigenes Tageslimit; NULL = Standard aus DAILY_COST_LIMIT_EUR
    tageslimit_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)
    gesperrt_am: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Role(Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    beschreibung: Mapped[str] = mapped_column(String(200), default="")


class UserRole(Base):
    """Eine Person kann mehrere Rollen haben."""

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_name: Mapped[str] = mapped_column(ForeignKey("roles.name"), primary_key=True)
    vergeben_von: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    vergeben_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)


class UserSecret(Base):
    """Verschlüsselte Zugangsdaten einer Person zu einem Dienst. Nie Klartext."""

    __tablename__ = "user_secrets"
    __table_args__ = (UniqueConstraint("user_id", "dienst"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    dienst: Mapped[str] = mapped_column(String(40))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    schluessel_version: Mapped[int] = mapped_column(Integer, default=1)
    erstellt_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)
    aktualisiert_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)


class UserMemory(Base):
    """Kurze persönliche Notizen und Vorlieben einer Person."""

    __tablename__ = "user_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    inhalt: Mapped[str] = mapped_column(Text)
    erstellt_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)


class SystemEinstellung(Base):
    """Merker des Systems, z. B. dass der Bestand aus der .env übernommen wurde."""

    __tablename__ = "system_einstellungen"

    schluessel: Mapped[str] = mapped_column(String(60), primary_key=True)
    wert: Mapped[str] = mapped_column(Text, default="")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    rolle: Mapped[str] = mapped_column(String(20))
    inhalt: Mapped[Any] = mapped_column(JSON)
    zeit: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    tool_name: Mapped[str] = mapped_column(String(100))
    parameter: Mapped[Any] = mapped_column(JSON)
    vorschau_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=STATUS_OFFEN, index=True)
    erstellt_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)
    entschieden_am: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zeit: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt, index=True)
    # NULL bei unbekannten Telegram-Nutzern (kein Eintrag in `users`)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(100))
    parameter: Mapped[Any] = mapped_column(JSON)
    ergebnis_kurz: Mapped[str] = mapped_column(Text, default="")
    dauer_ms: Mapped[int] = mapped_column(Integer, default=0)
    fehler: Mapped[str | None] = mapped_column(Text, nullable=True)


class Usage(Base):
    """Verbrauch je Modellantwort. Enthält nur Zahlen, nie Inhalte."""

    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zeit: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)
    datum: Mapped[date] = mapped_column(Date, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    modell: Mapped[str] = mapped_column(String(80), default="")
    eingabe_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    ausgabe_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_lese_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_schreib_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    kosten_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    kosten_eur: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    grund_modellwahl: Mapped[str] = mapped_column(String(200), default="")


class Notiz(Base):
    __tablename__ = "notizen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    erstellt_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)


class AsanaOperation(Base):
    """Ausführungsstand einer Operation eines Änderungssatzes (Schutz vor Duplikaten)."""

    __tablename__ = "asana_operationen"
    __table_args__ = (UniqueConstraint("approval_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    art: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default=OP_OFFEN)
    # GID des angelegten oder geänderten Objekts
    gid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fehler: Mapped[str | None] = mapped_column(Text, nullable=True)
    ausgefuehrt_am: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class TelegramDatei(Base):
    """Verweis auf eine Datei, die ein Nutzer im Chat geschickt hat.

    Gespeichert wird nur, wo die Datei bei Telegram liegt – nie ihr Inhalt.
    """

    __tablename__ = "telegram_dateien"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    file_id: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(300))
    medientyp: Mapped[str] = mapped_column(String(100), default="")
    groesse: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)
