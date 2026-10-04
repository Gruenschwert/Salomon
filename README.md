# gs-assistant

Interner KI-Assistent der Grünschwert GmbH (Canasups, Kiffkraut). Bedienung per Telegram, Claude als Gehirn, Tools für Shopify.

Die verbindliche Vorgabe steht in [BAUPLAN.md](BAUPLAN.md). Umgesetzt sind Phase 0 und 1.

## Was der Bot kann

- Antwortet nur Telegram-Nutzern aus der Whitelist. Alle anderen werden ohne Antwort ignoriert und im Audit-Log als `unbekannt` vermerkt.
- `shopify_lagerbestand`: Bestand je Lagerort, Suche nach Produktname oder SKU.
- `shopify_offene_bestellungen`: offene Bestellungen, optional nur ohne Tracking, höchstens 20.
- `demo_notiz`: speichert eine Notiz, aber erst nach Klick auf ✅. Dient nur dem Test des Freigabe-Flows.
- `/status` (nur Admins): Version, Uptime, Kosten heute, offene Freigaben.
- Jeder Tool-Aufruf steht im `audit_log`, der Verbrauch je Tag und Nutzer in `usage`.

## Deploy auf dem Server

Voraussetzung: Ubuntu 24.04 mit Docker und Docker Compose v2. Es werden keine Ports geöffnet und keine Domain benötigt (Telegram per Long Polling).

### 1. Zugänge anlegen

| Was | Wo | Hinweis |
|---|---|---|
| Telegram-Bot-Token | Telegram, Chat mit `@BotFather`, Befehl `/newbot` | |
| Eigene Telegram-ID | Telegram, z. B. Chat mit `@userinfobot` | Zahl, nicht der Nutzername |
| Anthropic-API-Key | Anthropic Console | |
| Shopify-Token je Shop | Shopify-Admin → Apps → App entwickeln → Custom App | **Nur Leserechte:** `read_products`, `read_inventory`, `read_locations`, `read_orders` |

Jeder Admin muss dem Bot einmal eine Nachricht schreiben, sonst kann Telegram ihm keine Alarme zustellen.

### 2. Konfiguration

```
git clone <repo-url> gs-assistant && cd gs-assistant
cp .env.example .env
chmod 600 .env
nano .env
```

Alle leeren Werte in `.env` ausfüllen. Zu beachten:

- `POSTGRES_PASSWORD`: nur Buchstaben und Ziffern verwenden. Das Passwort wird in `DATABASE_URL` eingesetzt; Sonderzeichen wie `@`, `:` oder `/` machen die URL ungültig.
- `TELEGRAM_ADMIN_USER_IDS` muss eine Teilmenge von `TELEGRAM_ALLOWED_USER_IDS` sein, sonst startet die App nicht.
- `SHOPIFY_*_DOMAIN`: die `…myshopify.com`-Adresse des Shops. `SHOPIFY_API_VERSION` im Format `JJJJ-MM` (aktuelle stabile Version laut Shopify).
- `PRICE_*` und `USD_EUR_RATE`: Grundlage der Kostenrechnung. Die Preise müssen zu `MODEL_DEFAULT` passen; bei einem Modellwechsel beides anpassen.
- `.env` wird nie committet (steht in `.gitignore`).

Ein Shop ohne Domain/Token ist erlaubt: Das Tool meldet dann „nicht konfiguriert“.

### 3. Starten

```
docker compose up -d --build
docker compose logs -f app
```

Beim Start führt die App erst `alembic upgrade head` aus, dann startet der Bot. Die Admins erhalten die Meldung „gs-assistant … wurde gestartet“.

Damit der Bot einen Server-Neustart übersteht, muss Docker beim Booten starten (`sudo systemctl enable docker`); die Container haben `restart: unless-stopped`.

### 4. Abnahme prüfen

1. Als erlaubter Nutzer „Hallo“ schreiben → der Bot antwortet. Von einem fremden Konto → keine Antwort.
2. „Wie viele <Produkt> haben wir bei Kiffkraut?“ → Zahlen mit dem Shopify-Admin vergleichen.
3. „Zeig offene Kiffkraut-Bestellungen ohne Tracking“.
4. „Speichere die Notiz: Test“ → Vorschau mit Buttons. ✅ speichert, ❌ verwirft.
5. `/status` als Admin → Kosten und offene Freigaben.
6. `sudo reboot` → der Bot meldet sich danach von selbst wieder.

### Betrieb

```
docker compose logs --tail 100 app                 # Logs (Rotation: 3 × 10 MB)
git pull && docker compose up -d --build           # Update einspielen
docker compose exec db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql   # Backup
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select zeit, tool_name, fehler from audit_log order by id desc limit 20"'
```

Whitelist ändern: `.env` anpassen, dann `docker compose up -d`. Entfernte IDs werden beim Start deaktiviert.

Ist das Tageslimit (`DAILY_COST_LIMIT_EUR`, gilt für alle Nutzer zusammen) erreicht, lehnt der Bot neue Anfragen bis zum nächsten Tag (Europe/Berlin) ab. Bei 80 % erhalten die Admins eine Warnung.

## Entwicklung

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
ruff check . && ruff format --check . && pytest
```

Die Tests brauchen weder Docker noch Zugangsdaten: Sie laufen gegen SQLite im Speicher, Anthropic und Shopify sind Attrappen. Die CI (GitHub Actions) führt bei jedem Push dieselben drei Befehle aus.

Neues Tool: eine Datei in `app/tools/` mit einer Unterklasse von `BasisTool` anlegen. Die Registry findet sie automatisch. Mit `schreibend = True` läuft das Tool nur nach Freigabe und braucht eine `vorschau()`.

## Abweichungen vom Bauplan

Alle drei wurden vor der Umsetzung abgestimmt:

- `MODEL_DEFAULT` ist `claude-sonnet-5` statt `claude-sonnet-5-5`, weil es die zweite Modell-ID nicht gibt.
- Zusätzliche Variablen `PRICE_INPUT_USD_PER_MTOK`, `PRICE_OUTPUT_USD_PER_MTOK`, `USD_EUR_RATE` für die Kostenrechnung in Euro.
- Zusätzliche Tabelle `notizen` für `demo_notiz`.

## Bekannte Grenzen

- Im Gesprächsverlauf wird je Runde nur der Text gespeichert, keine Tool-Ergebnisse. Claude kennt in der nächsten Nachricht also seine eigene Antwort, nicht die Rohdaten dahinter, und erfährt nicht, ob eine Freigabe erteilt wurde.
- `MODEL_CHEAP` ist konfiguriert, wird in Phase 0/1 aber noch nirgends verwendet.
- Der Filter „ohne Tracking“ prüft die 50 neuesten offenen Bestellungen; gibt es mehr, weist das Ergebnis darauf hin. Shopify liefert mit `read_orders` standardmäßig nur Bestellungen der letzten 60 Tage.
- Abgelaufene Freigaben werden erst beim Klick als `abgelaufen` markiert; `/status` zählt sie trotzdem nicht mehr mit.
