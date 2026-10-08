"""Rotiert den Hauptschlüssel der Zugangsdaten.

Ablauf:
1. Neuen Schlüssel erzeugen:
   python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
2. In .env: den bisherigen Wert von SECRETS_MASTER_KEY nach SECRETS_MASTER_KEY_ALT kopieren,
   den neuen Wert in SECRETS_MASTER_KEY eintragen, SECRETS_MASTER_KEY_VERSION um 1 erhöhen.
3. Dieses Skript ausführen:
   docker compose run --rm app python -m scripts.schluessel_rotieren
4. SECRETS_MASTER_KEY_ALT wieder leeren und den Bot neu starten.

Das Skript gibt nur Zahlen aus, nie Schlüssel oder Zugangsdaten.
"""

import asyncio

from sqlalchemy import select

from app.auth.tresor import Tresor, rotiere
from app.config import get_settings
from app.db.models import User
from app.db.session import erstelle_engine, erstelle_session_fabrik


async def main() -> None:
    settings = get_settings()
    tresor = Tresor.aus_settings(settings)
    if not tresor.verfuegbar:
        raise SystemExit("SECRETS_MASTER_KEY ist leer; es gibt nichts zu rotieren.")
    url = settings.app_database_url.get_secret_value().strip()
    engine = erstelle_engine(url or settings.database_url.get_secret_value())
    session_fabrik = erstelle_session_fabrik(engine)
    async with session_fabrik() as session:
        nutzer_ids = list(await session.scalars(select(User.id)))
    anzahl = await rotiere(session_fabrik, tresor, nutzer_ids)
    await engine.dispose()
    print(f"{anzahl} Einträge auf Schlüsselversion {tresor.aktuelle_version} umgestellt.")


if __name__ == "__main__":
    asyncio.run(main())
