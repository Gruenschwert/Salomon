# gs-assistant

Interner KI-Assistent der Grünschwert GmbH (Canasups, Kiffkraut). Bedienung per Telegram, Claude als Gehirn, Tools für Shopify.

Die verbindliche Vorgabe steht in [BAUPLAN.md](BAUPLAN.md).

## Entwicklung

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
ruff check . && ruff format --check . && pytest
```

Die Deploy-Anleitung folgt mit Schritt 9 des Bauplans.
