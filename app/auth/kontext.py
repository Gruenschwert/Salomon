"""Der NutzerKontext: Wer gerade handelt. Er kommt immer vom Server, nie vom Modell."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from app.db.models import ROLLE_ADMIN, STANDARD_TON, STANDARD_ZEITZONE


@dataclass(frozen=True)
class NutzerKontext:
    """Identität und Rechte der Person, in deren Auftrag eine Anfrage läuft.

    Der Telegram-Adapter baut ihn aus der Telegram-ID und der Datenbank. Der Agent-Code reicht
    ihn an jedes Tool und an jede Datenbank-Sitzung weiter. Kein Tool-Parameter, den das Modell
    füllt, kann ihn ersetzen.
    """

    nutzer_id: int
    telegram_id: int
    anzeigename: str = ""
    rollen: frozenset[str] = frozenset()
    rechte: frozenset[str] = frozenset()
    einstellungen: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def id(self) -> int:
        return self.nutzer_id

    @property
    def ton(self) -> str:
        return str(self.einstellungen.get("ton") or STANDARD_TON)

    @property
    def zeitzone(self) -> str:
        return str(self.einstellungen.get("zeitzone") or STANDARD_ZEITZONE)

    @property
    def ist_admin(self) -> bool:
        return ROLLE_ADMIN in self.rollen

    @property
    def rolle(self) -> str:
        """Frühere Einteilung in admin/user; wird mit den Rechten abgelöst."""
        return "admin" if self.ist_admin else "user"

    def darf(self, *rechte: str) -> bool:
        return all(recht in self.rechte for recht in rechte)
