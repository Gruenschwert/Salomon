# gs-assistant

Interner KI-Assistent der Grünschwert GmbH (Canasups, Kiffkraut). Bedienung per Telegram, Claude als Gehirn, Tools für Shopify und Asana.

Die verbindlichen Vorgaben stehen in [BAUPLAN.md](BAUPLAN.md) (Phase 0 und 1), [ASANA_TOOL.md](ASANA_TOOL.md) (Asana-Assistent und Foto-Eingang) und [ASANA_TOOL_ERWEITERUNG.md](ASANA_TOOL_ERWEITERUNG.md) (Asana-Erweiterung) und [MEHRBENUTZER.md](MEHRBENUTZER.md) (mehrere Personen, Rollen, Datentrennung, Kosten, Modellstufen). Alle vier sind umgesetzt.

## Was der Bot kann

- Antwortet nur Personen, die ein Admin angelegt hat. Alle anderen werden ohne Antwort ignoriert und im Audit-Log als `unbekannt` vermerkt.
- Mehrere Personen mit Rollen und Rechten, je eigenem Verlauf, eigenen Notizen, eigenem Asana-Zugang und eigenem Kostenlimit. Details unter [Mehrere Personen](#mehrere-personen) und [Datenschutz](#datenschutz).
- `shopify_lagerbestand`: Bestand je Lagerort, Suche nach Produktname oder SKU.
- `shopify_offene_bestellungen`: offene Bestellungen, optional nur ohne Tracking, höchstens 20.
- `demo_notiz`: speichert eine Notiz, aber erst nach Klick auf ✅. Dient nur dem Test des Freigabe-Flows.
- Asana lesen: Projekte, Aufgaben, Aufgabendetails, Abschnitte, Nutzer und Tags suchen.
- Asana ändern: anlegen, bearbeiten, verschieben, erledigen, kommentieren und löschen, immer als Änderungssatz mit einer Freigabe. Details unter [Asana](#asana).
- Fotos von Papierplänen lesen und daraus einen Änderungssatz vorschlagen.
- Fotos und Dateien aus dem Chat an Aufgaben oder Projekte anhängen, Anhänge (Bilder, PDFs) aus Asana lesen.
- Benutzerdefinierte Felder, Vorlagen, Kopien, Mitglieder, Teams, Zeiterfassung, Statusmeldungen, Projekt-Briefing, Portfolios und Ziele. Übersicht unter [Was der Bot in Asana kann](#was-der-bot-in-asana-kann).
- `/status` (nur Admins): Version, Uptime, Kosten heute, eigene offene Freigaben.
- Jeder Tool-Aufruf steht im `audit_log`, jede Antwort des Modells mit Modell, Tokens und Kosten in `usage`.

## Deploy auf dem Server

Voraussetzung: Ubuntu 24.04 mit Docker und Docker Compose v2. Es werden keine Ports geöffnet und keine Domain benötigt (Telegram per Long Polling).

### 1. Zugänge anlegen

| Was | Wo | Hinweis |
|---|---|---|
| Telegram-Bot-Token | Telegram, Chat mit `@BotFather`, Befehl `/newbot` | |
| Eigene Telegram-ID | Telegram, z. B. Chat mit `@userinfobot` | Zahl, nicht der Nutzername |
| Anthropic-API-Key | Anthropic Console | |
| Shopify-Token je Shop | Shopify-Admin → Apps → App entwickeln → Custom App | **Nur Leserechte:** `read_products`, `read_inventory`, `read_locations`, `read_orders` |
| Asana Personal Access Token | Asana → Profilbild → Einstellungen → Apps → Entwicklerkonsole → „Neues Zugriffstoken“ | Jede Person legt ihren eigenen an und verbindet ihn mit `/verbinden asana`. Der Bot kann für sie alles, was ihr Asana-Konto darf |
| Hauptschlüssel für Zugangsdaten | `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` | Als `SECRETS_MASTER_KEY` in `.env`. Zusätzlich an einem zweiten sicheren Ort aufbewahren: Ohne ihn sind die gespeicherten Zugänge verloren |

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
- `SECRETS_MASTER_KEY`: verschlüsselt die persönlichen Zugänge, siehe [Zugänge pro Person](#zugänge-pro-person). Ohne ihn gibt es kein `/verbinden`.
- `ASANA_*` und `PHOTO_MAX_MB`: siehe [Asana](#asana). `ASANA_TOKEN` ist nur noch der Zugang des ersten Admins.
- `MODEL_EINFACH`, `MODEL_STANDARD`, `MODEL_KOMPLEX`: siehe [Modellstufen](#modellstufen). Ein vorhandenes `MODEL_DEFAULT` gilt weiter als Standardstufe, solange `MODEL_STANDARD` leer ist.
- `ANTHROPIC_WORKSPACE_ID` ist optional. Ist sie gesetzt, geht sie bei jedem Claude-Aufruf als Header `anthropic-workspace-id` mit.
- In `.env` stehen Kommentare immer in eigenen Zeilen, nie hinter einem Wert.
- `USD_EUR_RATE`: fester Umrechnungskurs für die Kostenrechnung. `PRICE_*` gilt nur noch für Modelle, deren Preis nicht in `app/agent/preise.py` steht.
- `.env` wird nie committet (steht in `.gitignore`).

Ein Shop ohne Domain/Token ist erlaubt: Das Tool meldet dann „nicht konfiguriert“.

### 3. Starten

```
docker compose up -d --build
docker compose logs -f app
```

Beim Start führt die App erst `alembic upgrade head` aus, dann startet der Bot. Auch ein Update mit neuen Tabellen braucht deshalb keinen eigenen Schritt; die Migrationen lassen sich beliebig oft ausführen. Die Admins erhalten die Meldung „gs-assistant … wurde gestartet“.

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

Personen verwaltet ein Admin im Chat, siehe [Mehrere Personen](#mehrere-personen). Die Listen in `.env` gelten nur für die erste Einrichtung; Konten aus `TELEGRAM_ADMIN_USER_IDS` werden bei jedem Start als Admin sichergestellt.

## Mehrere Personen

Wer der Bot vor sich hat, bestimmt allein der Server aus der Telegram-ID. Das Modell bekommt nie eine Nutzer-ID als Parameter und kann sich nicht als jemand anderes ausgeben.

### Update von einer älteren Version

`git pull && docker compose up -d --build` genügt. Beim ersten Start mit dieser Version passiert von selbst:

- Alle bisherigen Konten bleiben. Admins behalten die Rolle `admin`, alle anderen bekommen `mitarbeiter`.
- Verlauf, Freigaben und Verbrauch bleiben erhalten.
- Der Bot arbeitet in der Datenbank ab sofort als eingeschränkte Rolle `app_laufzeit`; dafür ist kein neues Passwort nötig.
- Ist `SECRETS_MASTER_KEY` gesetzt, wird `ASANA_TOKEN` einmalig verschlüsselt dem ersten Admin (kleinste ID in `TELEGRAM_ADMIN_USER_IDS`) zugeordnet.
- Ist `SECRETS_MASTER_KEY` noch leer, nutzt nur dieser erste Admin weiter `ASANA_TOKEN`. Alle anderen bekommen bei Asana-Fragen den Hinweis, dass die Zugänge noch nicht eingerichtet sind, und `/verbinden` ist abgeschaltet.

Empfohlen direkt nach dem Update: `SECRETS_MASTER_KEY` erzeugen (Befehl in der Tabelle oben), in `.env` eintragen, `docker compose up -d`.

Die Variablen `ASANA_DELETE_ROLES`, `ASANA_API_AUFRUF_ROLES` und `ASANA_TEAM_VERWALTUNG_ROLES` gibt es nicht mehr. Stehen sie noch in `.env`, werden sie ignoriert; was jemand darf, regeln die Rollen.

### Rollen und Rechte

| Rolle | Darf |
|---|---|
| `mitarbeiter` | Asana lesen und ändern (mit Freigabe), eigene Mails |
| `buchhaltung` | Buchhaltung lesen und schreiben, eigene Mails, Asana lesen |
| `apotheken_updates` | Apotheken-Daten lesen und schreiben, Bestand lesen, Asana lesen |
| `admin` | Alles, dazu Personen, Rollen und Limits verwalten und die Kosten aller sehen |

Eine Person kann mehrere Rollen haben; es gilt die Vereinigung. Zu Buchhaltung, Apotheken, Mail, Bestand und Klaviyo gibt es die Rechte schon, die Tools dazu noch nicht. In Asana löschen, Teams verwalten und den allgemeinen API-Aufruf nutzen dürfen nur Admins. Die Zuordnung steht in `app/auth/rechte.py`.

Rechte wirken an zwei Stellen: Das Modell sieht nur die Tools, die die Person nutzen darf, und vor jeder Ausführung wird noch einmal geprüft. Auch ein Admin kommt nicht an Verlauf, Notizen, Zugänge oder Freigaben anderer; ein solches Recht gibt es nicht.

### Befehle

Befehle laufen direkt im Bot und nie über das Modell. `/hilfe` zeigt nur, was die Person nutzen darf.

| Befehl | Wirkung |
|---|---|
| `/start`, `/hilfe` | Begrüßung, Liste der Befehle |
| `/verbinden asana` | Eigenen Asana-Token hinterlegen. Der Bot fragt danach, prüft ihn bei Asana und löscht die Nachricht mit dem Token aus dem Chat |
| `/trennen asana`, `/verbunden` | Zugang entfernen, verbundene Dienste anzeigen (nur Namen) |
| `/kosten [1\|3\|7\|30]` | Eigene Kosten im Zeitraum, nach Modell und Grund der Modellwahl |
| `/modell einfach\|standard\|komplex\|auto` | Modellstufe festlegen, gilt bis zum nächsten Neustart des Bots |
| `/profil` | Name, Anrede (du/Sie) und Zeitzone anzeigen oder ändern |
| `/merken <text>`, `/gemerkt` | Persönliche Notiz speichern, Notizen anzeigen |
| `/vergessen` | Eigenen Verlauf und eigene Notizen löschen |
| `/nutzer` (Admin) | Alle Personen mit Rollen |
| `/nutzer_neu <telegram_id> <name>` (Admin) | Person anlegen, Rolle `mitarbeiter` |
| `/rolle <name> +rolle` oder `-rolle` (Admin) | Rolle geben oder nehmen. Der letzte Admin lässt sich nicht entfernen |
| `/sperren <name>`, `/entsperren <name>` (Admin) | Zugang sperren oder wieder öffnen |
| `/limit <name> <euro>` (Admin) | Tageslimit einer Person; `standard` setzt es zurück |
| `/kosten <tage> alle` (Admin) | Kosten aller Personen, nur Summen und Zahlen |

Befehle, die etwas an anderen Personen ändern, fragen vor dem Ausführen mit Ja/Nein nach.

### Zugänge pro Person

Jede Person verbindet ihren eigenen Asana-Token. Einen gemeinsamen Token als Rückfall gibt es nicht; wer nichts verbunden hat, bekommt „Verbinde zuerst deinen Asana-Zugang mit /verbinden asana.“ `ASANA_WORKSPACE_GID` und `ASANA_DEFAULT_TEAM_GID` bleiben gemeinsame Konfiguration.

Die Tokens liegen mit AES-256-GCM verschlüsselt in `user_secrets`. Jede Person hat einen eigenen Schlüssel, abgeleitet aus `SECRETS_MASTER_KEY` und ihrer ID. Der Token wird nie geloggt, nie im Verlauf gespeichert und nie an das Modell geschickt.

Schlüssel wechseln:

1. Neuen Schlüssel erzeugen (Befehl in der Tabelle oben).
2. In `.env` den bisherigen Wert nach `SECRETS_MASTER_KEY_ALT` kopieren, den neuen in `SECRETS_MASTER_KEY` eintragen, `SECRETS_MASTER_KEY_VERSION` um 1 erhöhen.
3. `docker compose run --rm app python -m scripts.schluessel_rotieren`
4. `SECRETS_MASTER_KEY_ALT` leeren, `docker compose up -d`.

### Kosten und Limits

- Jede Antwort des Modells wird einzeln gebucht: Person, Modell, Eingabe-, Ausgabe- und Cache-Tokens, Kosten in USD und EUR, Grund der Modellwahl. Die Preise je Modell stehen in `app/agent/preise.py` (Stand der offiziellen Preisliste vom 08.10.2026) und sind bei Preisänderungen dort anzupassen.
- `DAILY_COST_LIMIT_EUR` (Standard 5) gilt pro Person und Tag, `/limit` ändert es für eine Person. Bei 80 % bekommt die Person einmal eine Warnung, bei 100 % lehnt der Bot ihre Anfragen bis zum nächsten Tag (Europe/Berlin) ab.
- `DAILY_COST_LIMIT_TOTAL_EUR` (Standard 20) gilt für alle zusammen. Bei 80 % werden die Admins gewarnt, bei 100 % sind alle gesperrt.
- `/kosten` funktioniert auch bei erreichtem Limit.

### Modellstufen

| Stufe | Variable | Standard | Wann |
|---|---|---|---|
| einfach | `MODEL_EINFACH` | `claude-haiku-4-5-20251001` | Kurze Lesefrage (unter 200 Zeichen, Stichwörter wie „Status“, „Wie viel“, „Zeig mir“, „Liste“) ohne Änderungswunsch |
| standard | `MODEL_STANDARD` | `claude-sonnet-5-5` | Alles andere |
| komplex | `MODEL_KOMPLEX` | `claude-opus-5-5` | Foto oder PDF, mehr als 1500 Zeichen, Stichwörter wie „Analyse“, „Auswertung“, „Projektplan“, „Konzept“, „Buchhaltung“, „Vergleich“, sowie Tools mit dem Kennzeichen `komplex` |

- Die Wahl trifft `waehle_modell` in `app/agent/router.py` nach festen Regeln, ohne zusätzlichen KI-Aufruf. Die Stichwortlisten sind dort Konstanten.
- Scheitert das einfache Modell (leere Antwort, ungültiger Tool-Aufruf) oder will es etwas ändern, läuft die Anfrage genau einmal neu mit dem Standardmodell. Beide Läufe werden gebucht.
- Meldet Anthropic ein Modell als nicht verfügbar, nimmt der Bot das Standardmodell und schreibt eine Warnung ins Log. Für das Standardmodell selbst gibt es keinen Ersatz.
- Der feste Teil des System-Prompts und die Tool-Definitionen liegen im Prompt-Cache von Anthropic (fünf Minuten). Folgeanfragen und die Runden innerhalb einer Anfrage lesen diesen Teil zu einem Bruchteil des Eingabepreises. Jede Stufe und jede Rechte-Kombination hat ihren eigenen Cache.

## Datenschutz

### Was gespeichert wird

| Daten | Tabelle | Wie lange |
|---|---|---|
| Gesprächsverlauf (Texte der Fragen und Antworten, keine Fotos, keine Tool-Ergebnisse) | `messages` | `MESSAGE_RETENTION_DAYS` (Standard 90 Tage), dann täglich gelöscht; sofort mit `/vergessen` |
| Persönliche Notizen | `user_memory` | Bis `/vergessen` |
| Zugangsdaten (verschlüsselt) | `user_secrets` | Bis `/trennen` |
| Freigaben mit den vorgeschlagenen Änderungen | `approvals`, `asana_operationen` | Unbegrenzt |
| Tool-Aufrufe: Zeit, Tool, gekürzte Parameter, Kurzergebnis | `audit_log` | Unbegrenzt |
| Verbrauch: Modell, Tokens, Kosten, Grund der Modellwahl | `usage` | Unbegrenzt |
| Verweise auf Dateien aus dem Chat, ohne Inhalt | `telegram_dateien` | Unbegrenzt |
| Name, Telegram-ID, Rollen, Anrede, Zeitzone, Limit | `users`, `user_roles` | Solange das Konto besteht |

Fotos und Dateien selbst speichert der Bot nicht. Eine automatische Löschfrist gibt es bisher nur für den Verlauf.

### Wer es lesen kann

- Jede Person nur ihre eigenen Daten. Das erzwingt die Datenbank selbst (Row Level Security auf `messages`, `user_memory`, `user_secrets`, `approvals`, `asana_operationen`, `notizen`, `telegram_dateien`, `audit_log`), nicht nur der Code des Bots. Eine Abfrage ohne Nutzerkontext liefert null Zeilen.
- Admins sehen im Bot von anderen nur Namen, Rollen, Limits und Kostensummen, keine Inhalte.
- Die Logs enthalten keine Nachrichteninhalte und keine Zugangsdaten. Alle Secrets aus `.env` werden in jeder Log-Zeile durch `***` ersetzt, auch die Adresse der Telegram-API mit dem Bot-Token. `httpx` und `telegram` loggen erst ab WARNING, Datenbankfehler ohne die Werte der Abfrage.
- Außerhalb des Servers: Anthropic erhält bei jeder Anfrage den Verlauf der Person, ihre Notizen, die Frage und die Tool-Ergebnisse. Telegram transportiert alle Nachrichten; Bot-Chats sind nicht Ende-zu-Ende-verschlüsselt. Asana und Shopify sehen die Aufrufe mit dem jeweiligen Token.

### Die Grenze

Wer Root-Zugriff auf den Server hat und den Hauptschlüssel kennt, kann technisch alles entschlüsseln und lesen: Der Schlüssel steht in `.env`, und der Eigentümer der Datenbank umgeht die Row Level Security, ebenso ein Backup mit `pg_dump`. Der Schutz gilt gegenüber dem Bot, seinen Tools und allen Bot-Rollen einschließlich Admin, nicht gegenüber der Person, die den Server betreibt. Eine Stufe weiter (Schlüssel aus einem persönlichen Passwort ableiten) ist möglich, aber nicht gebaut.

Der Bot meldet sich standardmäßig mit dem Login aus `DATABASE_URL` an und wechselt bei jeder Verbindung in die eingeschränkte Rolle `app_laufzeit`. Das schützt vor Fehlern im Bot. Wer zusätzlich ausschließen will, dass der Prozess je als Eigentümer arbeitet, legt einen eigenen Login an und trägt ihn als `APP_DATABASE_URL` ein:

```
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "CREATE ROLE gs_bot LOGIN PASSWORD '"'"'<passwort>'"'"' IN ROLE app_laufzeit"'
```

Die Migrationen beim Start laufen weiter über `DATABASE_URL`.

### Organisatorisch nötig

Das löst der Code nicht, es gehört aber vor den Betrieb mit mehreren Mitarbeitern:

- Vertrag zur Auftragsverarbeitung mit Anthropic (Verarbeitung der Gespräche) und mit Hetzner (Server). Bei Anthropic zusätzlich die Übermittlung in ein Drittland prüfen.
- Die Mitarbeiter informieren: was der Bot speichert, wie lange, wer es lesen kann, und dass Gespräche an Anthropic gehen.
- Den Bot ins Verzeichnis der Verarbeitungstätigkeiten aufnehmen.

Das ist keine Rechtsberatung; die Punkte stammen aus der Vorgabe und sollten mit dem Datenschutzbeauftragten abgestimmt werden.

## Asana

### Einrichten

1. Personal Access Token anlegen (siehe Tabelle oben) und im Chat mit `/verbinden asana` hinterlegen. Der erste Admin kann ihn stattdessen als `ASANA_TOKEN` in `.env` eintragen; er wird beim Start übernommen.
2. `ASANA_WORKSPACE_GID` leer lassen, wenn das Konto genau einen Workspace sieht. Sieht es mehrere, antwortet der Bot beim ersten Asana-Aufruf mit einer Fehlermeldung, die Namen und GIDs aller Workspaces nennt. Die passende GID dann eintragen.
3. `ASANA_DEFAULT_TEAM_GID` nur setzen, wenn der Workspace eine Organisation ist. Dort braucht jedes neue Projekt ein Team. Die GID steht in der Adresse der Team-Seite in Asana.
4. `docker compose up -d`, dann im Chat „Welche Asana-Projekte haben wir?“ fragen.

| Variable | Standard | Bedeutung |
|---|---|---|
| `ASANA_MAX_OPS_PER_CHANGESET` | 100 | Größere Sätze werden abgelehnt, Claude teilt sie dann auf |
| `ASANA_MAX_DELETES_PER_CHANGESET` | 20 | Höchstzahl Löschoperationen je Satz |
| `ASANA_DELETE_ENABLED` | true | `false` schaltet Löschen ganz ab, auch für Admins |
| `PHOTO_MAX_MB` | 5 | Größere Fotos lehnt der Bot ab |
| `ASANA_ATTACHMENT_VIEW_MAX_MB` | 5 | Größere Anhänge lädt der Bot nicht zum Lesen herunter |
| `ASANA_API_AUFRUF_ENABLED` | true | `false` schaltet den allgemeinen API-Aufruf ganz ab |
| `MAX_OUTPUT_TOKENS` | 8000 | Ein Änderungssatz ist Claudes Ausgabe. Bei kleineren Werten passen große Pläne nicht in einen Satz. Unter 4000 warnt der Bot beim Start |
| `AGENT_MAX_ROUNDS` | 25 | Höchstzahl der Runden je Nachricht. Danach meldet der Bot den Zwischenstand; mit „weiter“ geht es dort weiter |

### So läuft eine Änderung ab

1. Du sagst, was sich ändern soll. Claude liest zuerst den aktuellen Stand aus Asana.
2. Claude schlägt einen Änderungssatz vor. Der Bot liest dafür jeden betroffenen Eintrag noch einmal und zeigt eine Vorschau: oben die Zahlen („12 anlegen, 3 ändern, 2 🗑 löschen“), darunter je Operation eine Zeile mit Vorher und Nachher. Lange Vorschauen kommen in mehreren Nachrichten, die Buttons hängen an der letzten.
3. ✅ führt den Satz der Reihe nach aus, ❌ verwirft ihn. Die Freigabe gilt 15 Minuten und nur für den Nutzer, der gefragt hat.
4. Enthält der Satz Löschungen, kommt nach ✅ die Rückfrage „Wirklich löschen? N Objekte“ mit den Buttons „🗑 Ja, löschen“ und „Abbrechen“. Bis zu dieser zweiten Bestätigung läuft **keine** Operation des Satzes, auch keine harmlose.
5. Danach meldet der Bot das Ergebnis mit Link. Scheitert eine Operation, hört der Satz dort auf. Die Meldung nennt, was erledigt ist, was fehlgeschlagen ist und was nicht mehr lief. Es wird nichts wiederholt und nichts zurückgerollt.

Sammelaufgaben („hake alle überfälligen ab“): Der Bot zählt zuerst mit einer einzigen Abfrage, nennt dir Anzahl und Stichtag und schlägt dann einen Änderungssatz vor. Ab mehr als 15 gleichartigen Operationen ist die Vorschau gruppiert („57 mal Aufgabe erledigen“), zeigt die ersten 10 Zeilen und bringt die vollständige Liste als `.txt`-Datei mit. Über 100 Operationen teilt er in Pakete.

Lange Texte teilt der Bot an Zeilenenden in mehrere Nachrichten. Erst über 20.000 Zeichen kommt nur der Anfang in den Chat und der ganze Text als Datei. Die Buttons einer Freigabe hängen immer an einer kurzen letzten Nachricht („Freigabe für N Änderungen, gültig 15 Minuten“). Kommt eine Freigabe nicht bei dir an, wird sie sofort verworfen und der Bot sagt dir, warum; es bleibt nichts offen, was später laufen könnte.

Termine gibt es in drei Formen: nur Fälligkeit (mit oder ohne Uhrzeit), ganze Tage von–bis und Zeitfenster mit Start- und Endzeit. Start ohne Uhrzeit und Fälligkeit mit Uhrzeit lassen sich in Asana nicht mischen; die Vorschau lehnt das ab, bevor etwas freigegeben wird. Wird an einer Aufgabe nur Start oder nur Ende geändert, übernimmt der Bot den anderen Wert aus dem aktuellen Stand. Uhrzeiten gelten als Ortszeit Europe/Berlin und gehen in UTC an Asana.

Fotos: ein Foto (oder ein Album mit bis zu 5 Fotos) mit oder ohne Bildunterschrift schicken. Claude gibt erst wieder, was es erkannt hat, fragt Fehlendes gebündelt nach und schlägt dann den Änderungssatz vor. Die Bilder werden nicht gespeichert; im Verlauf steht nur „[Foto]“ plus Bildunterschrift.

Eine leere Zeile wie `ASANA_API_AUFRUF_ROLES=` in `.env` bedeutet den Standardwert.

### Was der Bot in Asana kann

Lesen (ohne Freigabe): Projekte, Aufgaben samt Details, Abschnitte, Nutzer, Tags, benutzerdefinierte Felder, Projekt- und Aufgabenvorlagen, Teams und ihre Mitglieder, Portfolios, Ziele und Teilziele, Anhänge, Zeiteinträge, Statusmeldungen. Bilder und PDFs unter den Anhängen kann Claude ansehen. Aufgaben zeigen zusätzlich die Werte benutzerdefinierter Felder, die Anzahl der Anhänge und eine Wiederholungsregel.

Ändern (immer als Änderungssatz mit Freigabe):

| Bereich | Operationen |
|---|---|
| Projekte | anlegen, ändern (auch Sichtbarkeit, Standardansicht, Notizen, Farbe), archivieren, löschen, aus Vorlage, duplizieren, Mitglieder und Follower, Briefing |
| Abschnitte | anlegen, umbenennen, löschen, umsortieren |
| Aufgaben | anlegen, ändern, erledigen, verschieben, löschen, aus Vorlage, duplizieren, mehreren Projekten zuordnen, Unteraufgaben umhängen oder herauslösen, umsortieren, Genehmigungen, Follower, „Gefällt mir“ |
| Kommentare | hinzufügen, eigene bearbeiten und löschen |
| Anhänge | Datei oder Foto aus dem Chat, externer Link, löschen |
| Benutzerdefinierte Felder | anlegen, ändern, löschen, Option hinzufügen, im Projekt einrichten oder entfernen, Werte an Aufgaben setzen |
| Tags, Abhängigkeiten | anlegen, zuweisen, entfernen |
| Teams (nur `ASANA_TEAM_VERWALTUNG_ROLES`) | anlegen, ändern, Mitglieder hinzufügen und entfernen |
| Zeiterfassung | erfassen, ändern, löschen |
| Statusmeldungen | erstellen (Projekt, Portfolio, Ziel), löschen |
| Portfolios | anlegen, ändern, löschen, Projekte aufnehmen und herausnehmen |
| Ziele | anlegen, ändern, löschen, mit Projekt, Aufgabe, Portfolio oder Teilziel verknüpfen |
| Wiederholungen (experimentell) | von einer Aufgabe übernehmen, entfernen |

Dateien anhängen: Schick ein Foto oder Dokument und schreib dazu, wohin es soll („häng das an die Aufgabe X“). Der Bot merkt sich nur, wo die Datei bei Telegram liegt (Tabelle `telegram_dateien`), und lädt sie erst nach deiner Freigabe, um sie an Asana zu übergeben. Telegram gibt Bots nur Dateien bis 20 MB heraus; größere musst du direkt in Asana hochladen. Ein Foto ohne solchen Wunsch behandelt der Bot wie bisher als Plan. Ist unklar, was gemeint ist, fragt er einmal nach.

Benutzerdefinierte Felder an Aufgaben setzt du mit Namen: Der Bot findet Feld und Option selbst. Gibt es den Namen mehrfach, fragt er nach. Ist ein Feld im Projekt der Aufgabe nicht eingerichtet, sagt er das und bietet an, es dort einzurichten.

Projekte aus Vorlagen und Kopien erledigt Asana im Hintergrund. Der Bot wartet bis zu 60 Sekunden darauf. Dauert es länger, bricht der Satz ab und meldet das; das Projekt entsteht in Asana meist trotzdem, also dort nachsehen, bevor du es noch einmal anstößt.

Wiederholungen: Die Asana-Schnittstelle beschreibt das Feld dafür nicht. Der Bot baut deshalb keine Wiederholung selbst, sondern überträgt sie von einer Aufgabe, an der du sie in Asana von Hand eingerichtet hast („gib Aufgabe A dieselbe Wiederholung wie Aufgabe B“). Lege dir dafür je Muster (täglich, wöchentlich, monatlich, jährlich) eine Beispielaufgabe an. Lehnt Asana den Aufruf ab, sagt der Bot das und nennt den Weg in Asana: Aufgabe öffnen, auf das Datum klicken, „Wiederholen“.

Je nach Asana-Tarif fehlen benutzerdefinierte Felder, Zeiterfassung, Portfolios, Ziele oder Startzeiten. Der Bot meldet dann „Das ist in eurem Asana-Tarif nicht verfügbar oder dein Token darf das nicht“ und bricht den Satz an dieser Stelle ab.

Was nicht geht, weil die Asana-Schnittstelle es nicht anbietet (Stand Oktober 2026, gegen die API-Referenz geprüft): Regeln und Automatisierungen anlegen oder ändern (nur das Auslösen von Regeln mit Web-Request-Auslöser), Formulare, Dashboards und Berichtsdiagramme, gespeicherte Ansichten und Filter, Benachrichtigungseinstellungen und die Inbox. Der Bot beschreibt dann den Weg in Asana.

### Allgemeiner API-Aufruf

Für alles, wofür es keine eigene Funktion gibt, kann Claude mit `asana_api_aufruf` einen beliebigen Endpunkt unter `https://app.asana.com/api/1.0` ansprechen. Das dürfen nur Admins.

- GET läuft sofort, liefert höchstens eine Seite und wird gekürzt.
- POST und PUT zeigen Methode, Pfad, Begründung und Body als Vorschau und laufen erst nach ✅.
- DELETE braucht zusätzlich die zweite Bestätigung und das Recht zu löschen.
- Der Pfad darf nur aus einfachen Segmenten bestehen (`/tasks/123/stories`). Host, `..`, Fragezeichen und Sonderzeichen werden abgelehnt, bevor etwas gesendet wird. Den Token setzt nur der Client.
- Immer gesperrt: Änderungen an Nutzern, Workspaces und Workspace-Mitgliedschaften, Rollen, Budgets, Zugriffsanfragen, Webhooks, Organisations- und Massenexporte, das Audit-Log der Organisation, Sammelaufrufe (`/batch`), Token- und Anmelde-Endpunkte sowie Datei-Uploads. Die Liste steht als Konstante in `app/tools/asana_ops_api.py` und ist per Test abgesichert.
- Jeder Aufruf steht mit Pfad und Body im `audit_log`.

### Abnahme

1. „Welche Asana-Projekte haben wir?“ und „Was ist bei Projekt X offen?“ → Listen mit Asana vergleichen.
2. „Verschiebe alle offenen Aufgaben von Max auf nächsten Montag“ → Vorschau mit Vorher/Nachher, nach ✅ in Asana prüfen.
3. „Lösche die Aufgabe Y“ → 🗑-Vorschau, zweite Rückfrage, erst danach gelöscht.
4. Foto eines handschriftlichen Plans → Erkennung zur Kontrolle, dann Vorschau. ✅ legt Projekt, Abschnitte und Aufgaben an, ❌ nichts.
5. Eine PDF-Datei schicken mit „häng das an die Aufgabe Y“ → Vorschau, nach ✅ hängt die Datei in Asana an der Aufgabe.
6. „Setz bei Aufgabe Y das Feld Priorität auf Hoch“ → Vorschau mit altem und neuem Wert.
7. „Leg aus der Vorlage Z ein Projekt an“ → der Bot fragt nach den Terminen der Vorlage, nach ✅ existiert das Projekt.
8. Wiederholung: in Asana an einer Testaufgabe von Hand einrichten, dann „gib Aufgabe A dieselbe Wiederholung“. Das ist der Live-Test für die experimentelle Funktion.
9. `/status` zählt wartende Freigaben mit. Der Token darf in `docker compose logs app` nirgends stehen.

### Nachvollziehen

```
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select approval_id, position, art, status, gid, fehler from asana_operationen order by id desc limit 30"'
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select zeit, ergebnis_kurz, parameter from audit_log where tool_name = '"'"'asana_aenderungen_ausfuehren'"'"' order by id desc limit 5"'
```

`asana_operationen` hält je Operation fest, ob sie lief (`erledigt`, `fehlgeschlagen`, `nicht ausgeführt`). Im `audit_log` steht zu jedem ausgeführten Satz ein Eintrag mit Operationen, GIDs, geänderten Feldnamen und den Vorher-Werten. Beschreibungen sind dort gekürzt, der Token steht nie darin.

### Sicherheit

- Geändert wird ausschließlich über `asana_aenderungen_ausfuehren` und die schreibenden Aufrufe von `asana_api_aufruf`; beides läuft nur aus einer erteilten Freigabe heraus und über denselben Code.
- Als Löschung zählen auch Anhänge, Felder, Portfolios, Ziele, Statusmeldungen, Kommentare, Briefings, Zeiteinträge, das Entfernen von Team-Mitgliedern und jeder DELETE über den allgemeinen API-Aufruf.
- Anhänge lädt der Bot von der Adresse, die Asana nennt, ohne den Asana-Token mitzuschicken.
- Gelöscht wird nur mit einer GID aus einem Lese-Tool, nie nach Name und nie über einen Platzhalter. Die Löschregeln werden beim Vorschlagen und noch einmal beim Ausführen geprüft.
- Alle Secrets (Telegram, Anthropic, Asana, Shopify, Datenbank-Passwort, Hauptschlüssel) werden in jeder Log-Zeile durch `***` ersetzt, auch in Fehlerausgaben. `httpx` und `telegram` loggen erst ab WARNING.
- Texte aus Asana und aus Fotos gelten für Claude als Daten, nicht als Anweisungen.

## Entwicklung

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
ruff check . && ruff format --check . && pytest
```

Die Tests brauchen weder Docker noch Zugangsdaten. Anthropic, Shopify und Asana sind Attrappen. Die Datenbank ist ein echtes PostgreSQL: Ohne weitere Angabe startet `pgserver` (Entwicklungsabhängigkeit) eines im Hintergrund; mit `TEST_DATABASE_URL` laufen die Tests gegen eine eigene, leere Datenbank. Der ganze Lauf dauert gut zwei Minuten. Die CI (GitHub Actions) führt bei jedem Push dieselben drei Befehle aus.

Neues Tool: eine Datei in `app/tools/` mit einer Unterklasse von `BasisTool` anlegen. Die Registry findet sie automatisch. Mit `schreibend = True` läuft das Tool nur nach Freigabe und braucht eine `vorschau()`. `erforderliche_rechte` legt fest, wer es sieht und nutzen darf; `komplex = True` schaltet beim Aufruf auf das starke Modell. Persönliche Daten liest und schreibt ein Tool nur über `db_sitzung(session_fabrik, aktueller_nutzer.get())`. Muss die Vorschau erst etwas nachlesen, überschreibt das Tool stattdessen das asynchrone `bereite_vor()`. Optional sind `ergebnis_text()` für eine eigene Ergebnis-Meldung und `zweite_bestaetigung()` für eine zweite Rückfrage.

Neue Asana-Operation: eine Klasse mit `vorschau` und `ausfuehren` in einem der Module `app/tools/asana_ops_*.py`, registriert über `@registriere`. Das Modul trägt seine Felder in `ZUSATZ_SCHEMA` und `ZUSATZ_BESCHREIBUNG` ein und wird in `asana_schreiben.py` importiert. Feldnamen und Bodys stammen aus der API-Referenz von Asana, nicht aus dem Gedächtnis.

## Abweichungen vom Bauplan

Alle wurden vor der Umsetzung abgestimmt:

- `MODEL_DEFAULT` stand in der Vorlage auf `claude-sonnet-5`. Seit MEHRBENUTZER.md heißt die Variable `MODEL_STANDARD` mit dem Standard `claude-sonnet-5-5`; ein gesetztes `MODEL_DEFAULT` gilt weiter.
- Zusätzliche Variablen `PRICE_INPUT_USD_PER_MTOK`, `PRICE_OUTPUT_USD_PER_MTOK`, `USD_EUR_RATE` für die Kostenrechnung in Euro.
- Zusätzliche Tabelle `notizen` für `demo_notiz`.
- `MAX_OUTPUT_TOKENS` steht standardmäßig auf 8000 statt 1500, damit ein großer Asana-Änderungssatz in eine Antwort passt.
- `MAX_TOOL_ITERATIONS` (8) ist durch `AGENT_MAX_ROUNDS` (25) ersetzt; die alte Variable wird ignoriert.

Festlegungen zu ASANA_TOOL.md, wo das Dokument offen war (ebenfalls abgestimmt):

- `aufgabe_aendern`: `tags` und `follower` werden hinzugefügt, Vorhandenes bleibt. Tags entfernt `tag_zuweisen` mit `entfernen=true`.
- `aufgabe_verschieben` mit `aus_projekt_entfernen`: Die Aufgabe wird nur entfernt, wenn sie in genau einem anderen Projekt liegt. Bei mehreren lehnt das Tool ab, und der Assistent fragt nach.
- Das Ergebnis einer Asana-Freigabe (ausgeführt, verworfen, abgelaufen) wird als „[Ergebnis der Freigabe]“ in den Gesprächsverlauf geschrieben, damit Claude es bei der nächsten Nachricht kennt.
- Zusätzliche Tabelle `asana_operationen` und zusätzlicher Freigabe-Status `bestätigung` (erstes ✅ ist da, die Lösch-Rückfrage steht aus).

Festlegungen zu ASANA_TOOL_ERWEITERUNG.md:

- Zusätzliche Tabelle `telegram_dateien` (Migration 0003) mit Verweisen auf Dateien aus dem Chat, ohne deren Inhalt.
- Sperrliste des allgemeinen API-Aufrufs über die Vorgabe hinaus erweitert: `/batch`, `/exports`, `/organization_exports`, `/audit_log_events`, `/workspace_memberships` und alle schreibenden Aufrufe unter `/users` und `/workspaces`, nicht nur PUT.
- `wiederholung_setzen` nimmt keine frei beschriebene Wiederholung an, nur die einer Vorlage-Aufgabe.
- Eine neue Genehmigung legt `aufgabe_anlegen` mit `genehmigung=true` an; `aufgabe_genehmigung` setzt den Stand.

Festlegungen zu MEHRBENUTZER.md:

- Der Bot bekommt keinen eigenen Datenbank-Login, sondern wechselt mit dem vorhandenen in die Rolle `app_laufzeit`. Nur so läuft das Update ohne manuellen Schritt; `APP_DATABASE_URL` ist die strengere Variante.
- Ohne `SECRETS_MASTER_KEY` nutzt der erste Admin weiter `ASANA_TOKEN` aus `.env`, damit nach dem Update nichts ausfällt. Für alle anderen gibt es keinen Rückfall.
- Das einfache Modell eskaliert auch dann auf das Standardmodell, wenn es ein schreibendes Tool aufrufen will. Die Vorgabe nennt für die einfache Stufe nur lesende Tools.
- Zusätzlich zur Vorgabe schaltet ein Tool mit `komplex = True` (bisher nur das Lesen von Asana-Anhängen) für den Rest der Anfrage auf das starke Modell.

## Bekannte Grenzen

- Im Gesprächsverlauf wird je Runde nur der Text gespeichert, keine Tool-Ergebnisse. Claude kennt in der nächsten Nachricht also seine eigene Antwort, nicht die Rohdaten dahinter. Ob eine Freigabe erteilt wurde, erfährt Claude nur bei Asana-Änderungssätzen, nicht bei `demo_notiz`.
- Während ein Änderungssatz läuft, bearbeitet der Bot keine anderen Nachrichten. Ein Satz mit 100 Operationen kann eine Minute und länger dauern, bei erreichtem Asana-Abfragelimit entsprechend mehr.
- Antwortet Asana auf einen schreibenden Aufruf nicht (Timeout nach 10 s), bricht der Satz ab und meldet, dass unklar ist, ob diese eine Änderung angekommen ist. Sie wird bewusst nicht wiederholt; bitte in Asana nachsehen.
- Die Vorschau zeigt den Stand zum Zeitpunkt des Vorschlags. Ändert jemand in den bis zu 15 Minuten bis zum Klick etwas in Asana, gilt beim Ausführen der dann aktuelle Stand; die Vorher-Werte im Audit-Log stammen vom Zeitpunkt der Ausführung.
- Die Aufgabensuche über den ganzen Workspace gibt es bei Asana nur in bezahlten Tarifen. Ohne sie sucht der Bot je Projekt oder je Zuständigem und sagt das, wenn eine Angabe fehlt.
- Dokumente aus dem Chat kann der Bot anhängen, aber nicht lesen; lesen kann er Fotos im Chat sowie Bilder und PDFs, die schon in Asana hängen.
- Die Aufgabensuche liefert standardmäßig kompakte Zeilen für bis zu 300 Aufgaben. Die Zahl der Anhänge je Treffer gibt es nur in der ausführlichen Ansicht (höchstens 30 Aufgaben); das sind dann bis zu 30 zusätzliche Aufrufe.
- Ohne die Workspace-Suche von Asana (nicht in jedem Tarif) geht der Bot für „alle überfälligen“ selbst alle aktiven Projekte durch. Das dauert bei vielen Projekten entsprechend länger.
- Beim Duplizieren eines Projekts lässt sich kein Team angeben: Die API-Referenz kennt dafür kein Feld, die Kopie landet im Team des Originals.
- Die Wiederholungsregel ist in der Asana-Doku nicht beschrieben. Lesen und Übertragen sind deshalb ungeprüft, bis du sie einmal live ausprobiert hast.
- Portfolios zeigt Asana über einen persönlichen Token nur an, wenn sie dem Token-Inhaber gehören.
- `MODEL_CHEAP` wird nicht mehr gelesen; an seine Stelle tritt `MODEL_EINFACH`.
- `/modell` gilt nur bis zum nächsten Neustart des Bots.
- Der Router arbeitet mit Stichwörtern. Eine schwere Frage ohne eines dieser Wörter läuft auf dem Standardmodell; `/modell komplex` hilft dann.
- Für das Audit-Log, die Freigaben und den Verbrauch gibt es keine automatische Löschfrist.
- Der Filter „ohne Tracking“ prüft die 50 neuesten offenen Bestellungen; gibt es mehr, weist das Ergebnis darauf hin. Shopify liefert mit `read_orders` standardmäßig nur Bestellungen der letzten 60 Tage.
- Abgelaufene Freigaben werden erst beim Klick als `abgelaufen` markiert; `/status` zählt sie trotzdem nicht mehr mit.
