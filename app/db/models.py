"""Datenbank-Modelle (Bauplan Abschnitt 7, plus `notizen` für das Demo-Tool und
`asana_operationen` für den Ausführungsstand von Asana-Änderungssätzen)."""

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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

ROLLE_ADMIN = "admin"
ROLLE_USER = "user"

STATUS_OFFEN = "offen"
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
    name: Mapped[str] = mapped_column(String(200), default="")
    rolle: Mapped[str] = mapped_column(String(20), default=ROLLE_USER)
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True)
    erstellt_am: Mapped[datetime] = mapped_column(UtcDateTime, default=jetzt)


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
    __tablename__ = "usage"

    datum: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    kosten_eur: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))


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
