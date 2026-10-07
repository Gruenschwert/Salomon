# gs-assistant

Interner KI-Assistent der Grünschwert GmbH (Canasups, Kiffkraut). Bedienung per Telegram, Claude als Gehirn, Tools für Shopify und Asana.

Die verbindlichen Vorgaben stehen in [BAUPLAN.md](BAUPLAN.md) (Phase 0 und 1) und [ASANA_TOOL.md](ASANA_TOOL.md) (Asana-Assistent und Foto-Eingang). Beide sind umgesetzt.

## Was der Bot kann

- Antwortet nur Telegram-Nutzern aus der Whitelist. Alle anderen werden ohne Antwort ignoriert und im Audit-Log als `unbekannt` vermerkt.
- `shopify_lagerbestand`: Bestand je Lagerort, Suche nach Produktname oder SKU.
- `shopify_offene_bestellungen`: offene Bestellungen, optional nur ohne Tracking, höchstens 20.
- `demo_notiz`: speichert eine Notiz, aber erst nach Klick auf ✅. Dient nur dem Test des Freigabe-Flows.
- Asana lesen: Projekte, Aufgaben, Aufgabendetails, Abschnitte, Nutzer und Tags suchen.
- Asana ändern: anlegen, bearbeiten, verschieben, erledigen, kommentieren und löschen, immer als Änderungssatz mit einer Freigabe. Details unter [Asana](#asana).
- Fotos von Papierplänen lesen und daraus einen Änderungssatz vorschlagen.
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
| Asana Personal Access Token | Asana → Profilbild → Einstellungen → Apps → Entwicklerkonsole → „Neues Zugriffstoken“ | Der Bot kann alles, was dieses Asana-Konto darf. Am besten ein eigenes Konto mit Zugriff nur auf die nötigen Teams |

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
- `ASANA_*` und `PHOTO_MAX_MB`: siehe [Asana](#asana). Ohne `ASANA_TOKEN` melden die Asana-Tools „nicht konfiguriert“, alles andere läuft normal.
- `ANTHROPIC_WORKSPACE_ID` ist optional. Ist sie gesetzt, geht sie bei jedem Claude-Aufruf als Header `anthropic-workspace-id` mit.
- In `.env` stehen Kommentare immer in eigenen Zeilen, nie hinter einem Wert.
- `PRICE_*` und `USD_EUR_RATE`: Grundlage der Kostenrechnung. Die Preise müssen zu `MODEL_DEFAULT` passen; bei einem Modellwechsel beides anpassen.
- `.env` wird nie committet (steht in `.gitignore`).

Ein Shop ohne Domain/Token ist erlaubt: Das Tool meldet dann „nicht konfiguriert“.

### 3. Starten

```
docker compose up -d --build
docker compose logs -f app
```

Beim Start führt die App erst `alembic upgrade head` aus, dann startet der Bot. Auch ein Update mit neuen Tabellen (zuletzt `asana_operationen`) braucht deshalb keinen eigenen Schritt. Die Admins erhalten die Meldung „gs-assistant … wurde gestartet“.

Damit der Bot einen Server-Neustart übersteht, muss Docker beim Booten starten (`sudo systemctl enable docker`); die Container haben `restart: unless-stopped`.

### 4. Abnahme prüfen

1. Als erlaubter Nutzer „Hallo“ schreiben → der Bot antwortet. Von einem fremden Konto → keine Antwort.
2. „Wie viele <Produkt> haben wir bei Kiffkraut?“ → Zahlen mit dem Shopify-Admin vergleichen.
3. „Zeig offene Kiffkraut-Bestellungen ohne Tracking“.
4. „Speichere die Notiz: Test“ → Vorschau mit Buttons. ✅ speichert, ❌ verwirft.
5. `/status` als Admin → Kosten und offene Freigaben.
6. `sudo reboot` → der Bot meldet sich danach von selbst wieder.
7. Asana: die Punkte unter [Asana → Abnahme](#abnahme).

### Betrieb

```
docker compose logs --tail 100 app                 # Logs (Rotation: 3 × 10 MB)
git pull && docker compose up -d --build           # Update einspielen
docker compose exec db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql   # Backup
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select zeit, tool_name, fehler from audit_log order by id desc limit 20"'
```

Whitelist ändern: `.env` anpassen, dann `docker compose up -d`. Entfernte IDs werden beim Start deaktiviert.

Ist das Tageslimit (`DAILY_COST_LIMIT_EUR`, gilt für alle Nutzer zusammen) erreicht, lehnt der Bot neue Anfragen bis zum nächsten Tag (Europe/Berlin) ab. Bei 80 % erhalten die Admins eine Warnung.

## Asana

### Einrichten

1. Personal Access Token anlegen (siehe Tabelle oben) und als `ASANA_TOKEN` in `.env` eintragen.
2. `ASANA_WORKSPACE_GID` leer lassen, wenn das Konto genau einen Workspace sieht. Sieht es mehrere, antwortet der Bot beim ersten Asana-Aufruf mit einer Fehlermeldung, die Namen und GIDs aller Workspaces nennt. Die passende GID dann eintragen.
3. `ASANA_DEFAULT_TEAM_GID` nur setzen, wenn der Workspace eine Organisation ist. Dort braucht jedes neue Projekt ein Team. Die GID steht in der Adresse der Team-Seite in Asana.
4. `docker compose up -d`, dann im Chat „Welche Asana-Projekte haben wir?“ fragen.

| Variable | Standard | Bedeutung |
|---|---|---|
| `ASANA_MAX_OPS_PER_CHANGESET` | 100 | Größere Sätze werden abgelehnt, Claude teilt sie dann auf |
| `ASANA_MAX_DELETES_PER_CHANGESET` | 20 | Höchstzahl Löschoperationen je Satz |
| `ASANA_DELETE_ENABLED` | true | `false` schaltet Löschen ganz ab, auch für Admins |
| `ASANA_DELETE_ROLES` | admin | Rollen, die Löschungen vorschlagen dürfen (`admin`, `user`, Komma-getrennt) |
| `PHOTO_MAX_MB` | 5 | Größere Fotos lehnt der Bot ab |
| `MAX_OUTPUT_TOKENS` | 8000 | Ein Änderungssatz ist Claudes Ausgabe. Bei kleineren Werten passen große Pläne nicht in einen Satz |

### So läuft eine Änderung ab

1. Du sagst, was sich ändern soll. Claude liest zuerst den aktuellen Stand aus Asana.
2. Claude schlägt einen Änderungssatz vor. Der Bot liest dafür jeden betroffenen Eintrag noch einmal und zeigt eine Vorschau: oben die Zahlen („12 anlegen, 3 ändern, 2 🗑 löschen“), darunter je Operation eine Zeile mit Vorher und Nachher. Lange Vorschauen kommen in mehreren Nachrichten, die Buttons hängen an der letzten.
3. ✅ führt den Satz der Reihe nach aus, ❌ verwirft ihn. Die Freigabe gilt 15 Minuten und nur für den Nutzer, der gefragt hat.
4. Enthält der Satz Löschungen, kommt nach ✅ die Rückfrage „Wirklich löschen? N Objekte“ mit den Buttons „🗑 Ja, löschen“ und „Abbrechen“. Bis zu dieser zweiten Bestätigung läuft **keine** Operation des Satzes, auch keine harmlose.
5. Danach meldet der Bot das Ergebnis mit Link. Scheitert eine Operation, hört der Satz dort auf. Die Meldung nennt, was erledigt ist, was fehlgeschlagen ist und was nicht mehr lief. Es wird nichts wiederholt und nichts zurückgerollt.

Fotos: ein Foto (oder ein Album mit bis zu 5 Fotos) mit oder ohne Bildunterschrift schicken. Claude gibt erst wieder, was es erkannt hat, fragt Fehlendes gebündelt nach und schlägt dann den Änderungssatz vor. Die Bilder werden nicht gespeichert; im Verlauf steht nur „[Foto]“ plus Bildunterschrift.

Was nicht geht, weil die Asana-API es nicht anbietet: wiederkehrende Aufgaben einrichten, Regeln/Automatisierungen, Formulare.

### Abnahme

1. „Welche Asana-Projekte haben wir?“ und „Was ist bei Projekt X offen?“ → Listen mit Asana vergleichen.
2. „Verschiebe alle offenen Aufgaben von Max auf nächsten Montag“ → Vorschau mit Vorher/Nachher, nach ✅ in Asana prüfen.
3. „Lösche die Aufgabe Y“ → 🗑-Vorschau, zweite Rückfrage, erst danach gelöscht.
4. Foto eines handschriftlichen Plans → Erkennung zur Kontrolle, dann Vorschau. ✅ legt Projekt, Abschnitte und Aufgaben an, ❌ nichts.
5. `/status` zählt wartende Freigaben mit. Der Token darf in `docker compose logs app` nirgends stehen.

### Nachvollziehen

```
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select approval_id, position, art, status, gid, fehler from asana_operationen order by id desc limit 30"'
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select zeit, ergebnis_kurz, parameter from audit_log where tool_name = '"'"'asana_aenderungen_ausfuehren'"'"' order by id desc limit 5"'
```

`asana_operationen` hält je Operation fest, ob sie lief (`erledigt`, `fehlgeschlagen`, `nicht ausgeführt`). Im `audit_log` steht zu jedem ausgeführten Satz ein Eintrag mit Operationen, GIDs, geänderten Feldnamen und den Vorher-Werten. Beschreibungen sind dort gekürzt, der Token steht nie darin.

### Sicherheit

- Geändert wird ausschließlich über `asana_aenderungen_ausfuehren`, und das läuft nur aus einer erteilten Freigabe heraus.
- Gelöscht wird nur mit einer GID aus einem Lese-Tool, nie nach Name und nie über einen Platzhalter. Die Löschregeln werden beim Vorschlagen und noch einmal beim Ausführen geprüft.
- Alle Secrets (Telegram, Anthropic, Asana, Shopify, Datenbank-Passwort) werden in jeder Log-Zeile durch `***` ersetzt, auch in Fehlerausgaben. `httpx` und `telegram` loggen erst ab WARNING.
- Texte aus Asana und aus Fotos gelten für Claude als Daten, nicht als Anweisungen.

## Entwicklung

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
ruff check . && ruff format --check . && pytest
```

Die Tests brauchen weder Docker noch Zugangsdaten: Sie laufen gegen SQLite im Speicher, Anthropic, Shopify und Asana sind Attrappen. Die CI (GitHub Actions) führt bei jedem Push dieselben drei Befehle aus.

Neues Tool: eine Datei in `app/tools/` mit einer Unterklasse von `BasisTool` anlegen. Die Registry findet sie automatisch. Mit `schreibend = True` läuft das Tool nur nach Freigabe und braucht eine `vorschau()`. Muss die Vorschau erst etwas nachlesen, überschreibt das Tool stattdessen das asynchrone `bereite_vor()`. Optional sind `ergebnis_text()` für eine eigene Ergebnis-Meldung und `zweite_bestaetigung()` für eine zweite Rückfrage.

Neue Asana-Operation: eine Klasse mit `vorschau` und `ausfuehren` in `app/tools/asana_operationen.py`, registriert über `@_registriere`.

## Abweichungen vom Bauplan

Alle wurden vor der Umsetzung abgestimmt:

- `MODEL_DEFAULT` ist `claude-sonnet-5` statt `claude-sonnet-5-5`, weil es die zweite Modell-ID nicht gibt.
- Zusätzliche Variablen `PRICE_INPUT_USD_PER_MTOK`, `PRICE_OUTPUT_USD_PER_MTOK`, `USD_EUR_RATE` für die Kostenrechnung in Euro.
- Zusätzliche Tabelle `notizen` für `demo_notiz`.
- `MAX_OUTPUT_TOKENS` steht standardmäßig auf 8000 statt 1500, damit ein großer Asana-Änderungssatz in eine Antwort passt.

Festlegungen zu ASANA_TOOL.md, wo das Dokument offen war (ebenfalls abgestimmt):

- `aufgabe_aendern`: `tags` und `follower` werden hinzugefügt, Vorhandenes bleibt. Tags entfernt `tag_zuweisen` mit `entfernen=true`.
- `aufgabe_verschieben` mit `aus_projekt_entfernen`: Die Aufgabe wird nur entfernt, wenn sie in genau einem anderen Projekt liegt. Bei mehreren lehnt das Tool ab, und der Assistent fragt nach.
- Das Ergebnis einer Asana-Freigabe (ausgeführt, verworfen, abgelaufen) wird als „[Ergebnis der Freigabe]“ in den Gesprächsverlauf geschrieben, damit Claude es bei der nächsten Nachricht kennt.
- Zusätzliche Tabelle `asana_operationen` und zusätzlicher Freigabe-Status `bestätigung` (erstes ✅ ist da, die Lösch-Rückfrage steht aus).

## Bekannte Grenzen

- Im Gesprächsverlauf wird je Runde nur der Text gespeichert, keine Tool-Ergebnisse. Claude kennt in der nächsten Nachricht also seine eigene Antwort, nicht die Rohdaten dahinter. Ob eine Freigabe erteilt wurde, erfährt Claude nur bei Asana-Änderungssätzen, nicht bei `demo_notiz`.
- Während ein Änderungssatz läuft, bearbeitet der Bot keine anderen Nachrichten. Ein Satz mit 100 Operationen kann eine Minute und länger dauern, bei erreichtem Asana-Abfragelimit entsprechend mehr.
- Antwortet Asana auf einen schreibenden Aufruf nicht (Timeout nach 10 s), bricht der Satz ab und meldet, dass unklar ist, ob diese eine Änderung angekommen ist. Sie wird bewusst nicht wiederholt; bitte in Asana nachsehen.
- Die Vorschau zeigt den Stand zum Zeitpunkt des Vorschlags. Ändert jemand in den bis zu 15 Minuten bis zum Klick etwas in Asana, gilt beim Ausführen der dann aktuelle Stand; die Vorher-Werte im Audit-Log stammen vom Zeitpunkt der Ausführung.
- Die Aufgabensuche über den ganzen Workspace gibt es bei Asana nur in bezahlten Tarifen. Ohne sie sucht der Bot je Projekt oder je Zuständigem und sagt das, wenn eine Angabe fehlt.
- Follower lassen sich hinzufügen, aber nicht entfernen. Fotos werden nur als Telegram-Foto gelesen, nicht als angehängte Datei.
- `MODEL_CHEAP` ist konfiguriert, wird in Phase 0/1 aber noch nirgends verwendet.
- Der Filter „ohne Tracking“ prüft die 50 neuesten offenen Bestellungen; gibt es mehr, weist das Ergebnis darauf hin. Shopify liefert mit `read_orders` standardmäßig nur Bestellungen der letzten 60 Tage.
- Abgelaufene Freigaben werden erst beim Klick als `abgelaufen` markiert; `/status` zählt sie trotzdem nicht mehr mit.
