# ASANA_TOOL_ERWEITERUNG.md – Asana-Tool auf "alles, was ein Mensch kann"

Diese Spezifikation erweitert das bestehende Asana-Tool (`ASANA_TOOL.md`, Stand: Zeitfenster-Fix gemerged).
Sie ändert nichts an den Sicherheitsregeln: Schreiben immer mit Vorschau und Freigabe, Löschen mit zweiter
Bestätigung, Audit-Log, Limits pro Änderungssatz.

## 0. Arbeitsweise (verbindlich)

1. Lies zuerst die vorhandenen Dateien: `app/tools/asana_client.py`, `asana_lesen.py`, `asana_schreiben.py`,
   `asana_operationen.py`, `app/agent/prompts.py`, die Tests. Halte dich an Stil und Struktur des Bestands.
2. Arbeite auf einem neuen Branch `asana-erweiterung`. Am Ende: Tests grün, dann nach `main` mergen
   (`git checkout main && git merge asana-erweiterung`) und `git push origin main`. Ein Branch, der nicht in
   `main` gemerged und gepusht ist, erreicht den Server nicht.
3. Nichts behaupten, ohne es zu belegen. Zeige am Ende die Beweise aus Abschnitt 9.
4. Offizielle Doku vor jeder Umsetzung lesen: https://developers.asana.com/reference
   (jeweils die Seite zum Endpunkt). Keine Feldnamen raten.
5. `.env` niemals anfassen oder committen. Neue Variablen nur mit Standardwert in `config.py` und in
   `.env.example` (Kommentare auf eigenen Zeilen, leere Werte müssen akzeptiert werden).

## 1. Ziel

Der Bot soll in Asana alles können, was die API hergibt, und damit so viel wie ein Mensch bei der täglichen
Arbeit. Was die API nicht anbietet, meldet der Bot ehrlich ("Das geht über die Asana-Schnittstelle nicht,
das musst du in Asana selbst machen") und beschreibt den Weg in der Oberfläche.

Zwei Bausteine:
- **Baustein A:** eigene, gut beschriebene Funktionen für die häufigen Aufgaben (Abschnitt 2 bis 5).
- **Baustein B:** ein allgemeiner API-Aufruf als Auffangnetz für alles andere (Abschnitt 6).

## 2. Neue Lese-Tools (ohne Freigabe)

Alle mit Paginierung (Asana-Seiten bis 100 Einträge, `offset`-Token weiterverfolgen, Obergrenze einstellbar),
und mit einer kurzen, für das Modell lesbaren Ausgabe (keine rohen JSON-Berge).

| Tool | Zweck | Endpunkte (zur Orientierung) |
|---|---|---|
| `asana_felder_anzeigen` | Benutzerdefinierte Felder eines Workspace oder Projekts, inkl. Typ und Auswahloptionen | GET /workspaces/{gid}/custom_fields, GET /projects/{gid}/custom_field_settings |
| `asana_vorlagen_anzeigen` | Projektvorlagen und Aufgabenvorlagen, inkl. nötiger Datumsvariablen (`requested_dates`) | GET /project_templates, GET /project_templates/{gid}, GET /task_templates |
| `asana_teams_anzeigen` | Teams und deren Mitglieder | GET /workspaces/{gid}/teams, GET /teams/{gid}/users |
| `asana_portfolios_anzeigen` | Portfolios und deren Projekte | GET /portfolios, GET /portfolios/{gid}/items |
| `asana_ziele_anzeigen` | Ziele (Goals) und Teilziele | GET /goals |
| `asana_anhaenge_anzeigen` | Anhänge einer Aufgabe oder eines Projekts mit Name, Typ, Größe | GET /attachments?parent= |
| `asana_zeiteintraege_anzeigen` | Zeiteinträge einer Aufgabe | GET /tasks/{gid}/time_tracking_entries |
| `asana_statusmeldungen_anzeigen` | Statusmeldungen eines Projekts oder Portfolios | GET /status_updates?parent= |
| `asana_anhang_ansehen` | Lädt einen Bild- oder PDF-Anhang herunter und gibt ihn dem Modell zum Lesen (nur Bilder und PDFs, Obergrenze `ASANA_ATTACHMENT_VIEW_MAX_MB`, Standard 5) | GET /attachments/{gid} (download_url) |

Bestehende Lese-Tools erweitern: `asana_aufgabe_details` und `asana_aufgaben_suchen` liefern zusätzlich die
Werte der benutzerdefinierten Felder, die Anzahl der Anhänge und, falls vorhanden, die Wiederholungsregel.

## 3. Neue Schreib-Operationen

Sie kommen in das bestehende Tool `asana_aenderungen_ausfuehren` (gleiche Vorschau, gleiche Freigabe, gleiche
Platzhalter `$p1…`, gleiche Limits). Jede Operation hat eine deutsche Vorschau-Zeile.

### 3.1 Anhänge
- `anhang_hinzufuegen`: Datei (aus einer Telegram-Datei oder einem Foto der aktuellen Nachricht) oder externer
  Link an Aufgabe oder Projekt. Datei-Upload als multipart (POST /attachments mit `parent`, `file`, `name`).
  Externer Link: `resource_subtype=external`, `url`, `name`.
- `anhang_loeschen` (zählt als Löschung, zweite Bestätigung).
- Telegram-Eingang: Schickt der Nutzer ein Foto oder Dokument mit der Absicht "häng das an Aufgabe X", wird
  angehängt. Ohne diese Absicht bleibt das bisherige Verhalten (Foto eines Projektplans in Aufgaben umwandeln).
  Ist die Absicht unklar, fragt der Bot einmal nach.
- Dateigrößen: Telegram erlaubt Bots nur Downloads bis 20 MB, Asana Uploads bis 100 MB. Bei Überschreitung
  klare Fehlermeldung.

### 3.2 Benutzerdefinierte Felder
- `feld_anlegen`: Typen text, number, enum, multi_enum, date, people (inkl. Auswahloptionen, Nachkommastellen).
- `feld_aendern`, `feld_loeschen`, `feldoption_hinzufuegen`.
- `feld_zu_projekt_hinzufuegen` und `feld_aus_projekt_entfernen` (POST /projects/{gid}/addCustomFieldSetting
  bzw. removeCustomFieldSetting).
- `aufgabe_anlegen` und `aufgabe_aendern` bekommen ein Feld `felder`: Objekt mit Feldname oder GID als Schlüssel
  und dem Wert. Das Tool löst Namen in GIDs auf (Feld und bei Auswahlfeldern die Option) und meldet Mehrdeutigkeit.
  Wertformate laut Doku: text als String, number als Zahl, enum als Options-GID, multi_enum als Liste von
  Options-GIDs, date als Objekt mit `date`, people als Liste von Nutzer-GIDs.
- Ist ein Feld im Projekt der Aufgabe nicht eingerichtet, sagt der Bot das und bietet `feld_zu_projekt_hinzufuegen` an.

### 3.3 Vorlagen und Kopien
- `projekt_aus_vorlage`: POST /project_templates/{gid}/instantiateProject mit Name, Team und den geforderten
  Datumsvariablen (aus `requested_dates`). Das Ergebnis ist ein Job: pollen mit GET /jobs/{gid}, bis
  `succeeded`, Timeout 60 Sekunden, dann die GID des neuen Projekts zurückgeben (wird als Platzhalter nutzbar).
  Request-Format unterscheidet sich je nach `is_organization` des Workspace (siehe Doku).
- `aufgabe_aus_vorlage`: POST /task_templates/{gid}/instantiateTask.
- `aufgabe_duplizieren`: POST /tasks/{gid}/duplicate mit `name` und `include` (z. B. Unteraufgaben,
  Zuständige, Daten, Anhänge, Tags).
- `projekt_duplizieren`: POST /projects/{gid}/duplicate (Name, Team, include, optional Datumsverschiebung).
  Auch Jobs hier pollen.

### 3.4 Mitglieder, Teams, Sichtbarkeit
- `projekt_mitglied_hinzufuegen` und `projekt_mitglied_entfernen` (POST /projects/{gid}/addMembers bzw.
  removeMembers), mit Nutzername statt GID, aufgelöst über `asana_nutzer_suchen`.
- `projekt_follower_hinzufuegen` und `projekt_follower_entfernen`.
- `team_anlegen`, `team_aendern`, `team_mitglied_hinzufuegen`, `team_mitglied_entfernen`
  (Rolle `admin` erforderlich, siehe Abschnitt 7).
- Projekt: `privat` bzw. `sichtbar fuer Team` über das Feld `privacy_setting`/`public` laut Doku, `farbe`,
  `standardansicht` (list, board, calendar, timeline) und `notizen` in `projekt_aendern` ergänzen, soweit die API
  das Feld bietet.

### 3.5 Aufgaben, die noch fehlen
- `aufgabe_zu_projekt_hinzufuegen` und `aufgabe_aus_projekt_entfernen` (Mehrfachzuordnung; POST
  /tasks/{gid}/addProject und removeProject, mit optionalem Abschnitt und Position).
- `unteraufgabe_umhaengen` und `unteraufgabe_zu_aufgabe_machen` (POST /tasks/{gid}/setParent).
- `reihenfolge_aendern`: Aufgaben in einem Abschnitt oder Abschnitte in einem Projekt umsortieren
  (insert_before/insert_after).
- `kommentar_bearbeiten` und `kommentar_loeschen` (PUT/DELETE /stories/{gid}); nur eigene Kommentare.
- `aufgabe_genehmigung`: Aufgabentyp "Genehmigung" (`resource_subtype=approval`) anlegen und
  `approval_status` setzen (approved, rejected, changes_requested, pending).
- `aufgabe_gefaellt_mir` (Herz) ist optional, nur wenn trivial.
- `follower_hinzufuegen` und `follower_entfernen` an Aufgaben (existiert evtl. schon, dann nur prüfen).

### 3.6 Wiederkehrende Aufgaben (undokumentiert, deshalb vorsichtig)
Die Asana-API kann Wiederholungen lesen und setzen, dokumentiert ist das aber nicht (Stand März 2026, laut
Asana-Forum). Vorgehen:
1. Auf einer Test-Aufgabe in Asana von Hand eine Wiederholung einrichten (täglich, wöchentlich, monatlich,
   jährlich) und per `GET /tasks/{gid}?opt_fields=recurrence` die zurückgegebene Struktur ansehen.
2. Genau diese Struktur beim Setzen verwenden (`wiederholung_setzen`, `wiederholung_entfernen`). Keine
   Struktur erfinden.
3. Als "experimentell" kennzeichnen: schlägt der Aufruf fehl, meldet der Bot den Grund und sagt, dass die
   Wiederholung in Asana selbst unter "Datum > Wiederholen" eingestellt werden muss.
4. Tests mit einer gemockten Antwort. Der Live-Test passiert beim Nutzer.

### 3.7 Zeiterfassung, Status, Briefing, Ziele, Portfolios
- `zeit_erfassen` (POST /tasks/{gid}/time_tracking_entries mit `duration_minutes`, `entered_on`),
  `zeit_aendern`, `zeit_loeschen`.
- `statusmeldung_erstellen` (POST /status_updates: Elternobjekt Projekt, Portfolio oder Ziel; Status
  on_track, at_risk, off_track usw. laut Doku; Titel; Text), `statusmeldung_loeschen`.
- `projektbriefing_setzen` (Projekt-Briefing anlegen oder aktualisieren, Text als `html_text` laut Doku),
  `projektbriefing_loeschen`.
- `portfolio_anlegen`, `portfolio_aendern`, `portfolio_loeschen`, `portfolio_projekt_hinzufuegen`,
  `portfolio_projekt_entfernen`.
- `ziel_anlegen`, `ziel_aendern`, `ziel_loeschen`, `ziel_aufgabe_oder_projekt_verknuepfen`.

Hinweis zu Tarifen: Benutzerdefinierte Felder, Zeiterfassung, Portfolios, Ziele und Startzeiten sind je nach
Asana-Tarif nicht überall verfügbar. Antwortet Asana mit einem Tarif- oder Berechtigungsfehler (402/403 mit
entsprechendem Text), übersetzt das Tool das in einen klaren deutschen Satz ("Das ist in eurem Asana-Tarif
nicht verfügbar oder dein Token darf das nicht") und bricht den Änderungssatz an dieser Stelle wie gewohnt ab.

## 4. Gemeinsames Verhalten aller neuen Operationen

- Namen statt GIDs: Der Bot nimmt Namen vom Nutzer, löst sie über die Lese-Tools auf und fragt bei
  Mehrdeutigkeit nach, statt zu raten.
- Datums- und Zeitregeln aus dem Zeitfenster-Fix gelten unverändert (start_at/due_at nur UTC, nie gemischt).
- Vorschau: eine verständliche deutsche Zeile pro Operation, ohne Markdown-Sternchen. Der Nutzer wird geduzt.
- Limits (`ASANA_MAX_OPS_PER_CHANGESET`, `ASANA_MAX_DELETES_PER_CHANGESET`) gelten für alle neuen Operationen.
  Als Löschung zählen: `anhang_loeschen`, `feld_loeschen`, `portfolio_loeschen`, `ziel_loeschen`,
  `statusmeldung_loeschen`, `kommentar_loeschen`, `projektbriefing_loeschen`, `zeit_loeschen`, `team_*` mit Entfernen.
- Rate Limits: Bei HTTP 429 den `Retry-After`-Header beachten, höchstens 3 Wiederholungen, dann sauberer Fehler.
- Audit-Log: jede neue Operation mit Nutzer, Zeit, Art, Ziel-GID und Erfolg oder Fehler. Bei Änderungen den
  Vorher-Zustand speichern, soweit leicht lesbar.
- Teilausfälle: kein automatisches Zurückrollen, Bericht, was geklappt hat und was nicht (wie bisher).

## 5. Systemprompt-Ergänzungen

In `app/agent/prompts.py` den Asana-Abschnitt ergänzen:
- Kurze Liste, was der Bot in Asana kann (Abschnitte 2 und 3) und was nicht (Abschnitt 8).
- Regel: Bei "häng das an" mit Foto oder Datei immer Anhang, bei einem Foto eines Projektplans Projekt aus Plan.
- Regel: Uhrzeiten gehören in die Zeitfelder, Zeitfenster mit `startzeit` + `faellig_um`.
- Regel: Der Bot behauptet nie, etwas sei in Asana nicht möglich, ohne vorher Abschnitt 6 (allgemeiner
  API-Aufruf) geprüft zu haben. Nur wenn auch das nicht geht, sagt er es dem Nutzer und beschreibt den
  Weg in der Asana-Oberfläche.
- Weiter duzen, kein Markdown mit Sternchen.

## 6. Baustein B: allgemeiner API-Aufruf (`asana_api_aufruf`)

Ein einziges zusätzliches Tool für alles, was Asana per API kann und wofür es noch keine eigene Funktion gibt.

Parameter: `methode` (GET, POST, PUT, DELETE), `pfad` (z. B. `/tasks/123`), `abfrage` (Objekt, optional),
`body` (Objekt, optional), `begruendung` (kurzer Satz, wofür).

Sicherheitsregeln (zwingend):
1. Ziel ist immer nur `https://app.asana.com/api/1.0` plus `pfad`. `pfad` muss mit `/` beginnen, darf keine
   Schema- oder Host-Teile, kein `..`, keine Steuerzeichen enthalten. Der Token wird nur vom Client gesetzt,
   nie vom Modell und nie in Logs oder Antworten ausgegeben.
2. GET: ohne Freigabe, mit Ergebnis-Kürzung (Obergrenze Zeichen, Paginierung begrenzt).
3. POST, PUT: nur mit Vorschau und Freigabe über denselben Freigabemechanismus. Die Vorschau zeigt Methode,
   Pfad, `begruendung` und den Body in lesbarer Form. Zählt in `ASANA_MAX_OPS_PER_CHANGESET`.
4. DELETE: Vorschau, Freigabe und zweite Bestätigung, nur Rolle `ASANA_DELETE_ROLES`, zählt in
   `ASANA_MAX_DELETES_PER_CHANGESET`.
5. Sperrliste (immer abgelehnt, mit klarer Meldung): Änderungen an Nutzern und Workspaces (`PUT /users/*`,
   `PUT /workspaces/*`), Rollen und Budgets (`/roles`, `/budgets`), Zugriffs- und Webhook-Verwaltung
   (`/access_requests`, `/webhooks`), Token- und Authentifizierungs-Endpunkte. Die Sperrliste liegt als
   Konstante im Code, ist per Test abgesichert und nur durch Codeänderung erweiterbar, nie durch das Modell.
6. Kein Datei-Upload über dieses Tool (dafür gibt es `anhang_hinzufuegen`).
7. Alles ins Audit-Log, inklusive Pfad und Body (ohne Token).
8. Umschaltbar: `ASANA_API_AUFRUF_ENABLED` (Standard `true`), nur für Rollen aus `ASANA_API_AUFRUF_ROLES`
   (Standard `admin`).

## 7. Rollen

- Neue Variablen mit Standardwerten: `ASANA_API_AUFRUF_ENABLED=true`, `ASANA_API_AUFRUF_ROLES=admin`,
  `ASANA_TEAM_VERWALTUNG_ROLES=admin`, `ASANA_ATTACHMENT_VIEW_MAX_MB=5`.
- Team-Verwaltung (`team_*`) nur für diese Rollen.
- Alles andere wie bisher für alle freigegebenen Nutzer.

## 8. Was ausdrücklich nicht geht (Bot sagt es ehrlich)

Nach der Dokumentation der Asana-API (Stand Oktober 2026) gibt es keine Endpunkte zum Anlegen oder Ändern von:
Regeln und Automatisierungen (nur das Auslösen von Regeln mit Web-Request-Auslöser ist möglich), Formularen,
Dashboards und Berichtsdiagrammen, gespeicherten Ansichten und Filtern, Benachrichtigungseinstellungen und der
Inbox. Der Bot beschreibt in diesen Fällen den Weg in der Oberfläche und bietet an, die Vorarbeit zu machen
(zum Beispiel die Aufgaben anzulegen, die später in ein Formular gehören).
Vor dem Einbau dieser Aussagen kurz gegen https://developers.asana.com/reference prüfen und bei Abweichung
den Text im Systemprompt anpassen.

## 9. Tests und Abnahme

Tests (pytest, API immer gemockt):
- Pfadprüfung des allgemeinen Aufrufs: abgelehnt werden `https://evil.example`, `//evil`, `/../`, Pfade mit
  Steuerzeichen, Sperrlisten-Pfade (auch mit Groß-/Kleinschreibung und Querystring-Tricks).
- DELETE im allgemeinen Aufruf ohne zweite Bestätigung wird nicht ausgeführt, ohne Rolle abgelehnt.
- Anhang: Datei-Upload erzeugt multipart mit `parent`, `file`, `name`; externer Link erzeugt `external`.
- Felder: Namensauflösung inklusive Mehrdeutigkeit, Wertformate je Typ.
- Vorlagen: Job-Polling bis `succeeded`, Timeout, Fehlerfall.
- Rate Limit 429 mit `Retry-After`.
- Tarifbezogene 402/403 werden in verständliche deutsche Meldungen übersetzt.
- Limits zählen auch für neue Operationen.
- Wiederholung: Struktur wird 1:1 aus der gemockten Lese-Antwort übernommen.
- Vorschau-Texte enthalten kein `**`.

Abnahme, Beweise zeigen:
a) `git diff --stat` und die Liste der neuen Tool- und Operationsnamen
b) Ausgabe von `pytest -q`
c) für einen Anhang, ein benutzerdefiniertes Feld und eine Projektvorlage je ein Beispiel-Payload aus dem Test
d) Auszug aus dem Systemprompt mit den neuen Regeln
e) Nach dem Merge: `git log --oneline -5` auf `main` und `git status`

Danach auf `main` mergen und pushen (siehe Abschnitt 0). Auf dem Server rollt der Nutzer aus.

## 10. Reihenfolge (jeweils mit Tests, jeweils ein Commit)

1. Lese-Tools und Erweiterung der Details (Abschnitt 2).
2. Anhänge samt Telegram-Eingang (3.1).
3. Benutzerdefinierte Felder (3.2).
4. Vorlagen und Kopien samt Job-Polling (3.3).
5. Mitglieder, Teams, Projekt-Einstellungen (3.4) und fehlende Aufgaben-Operationen (3.5).
6. Zeiterfassung, Status, Briefing, Portfolios, Ziele (3.7).
7. Wiederkehrende Aufgaben (3.6).
8. Allgemeiner API-Aufruf (Abschnitt 6) mit Sperrliste und Tests.
9. Systemprompt, Rollen, README, `.env.example` (Abschnitte 5 und 7).
10. Abnahme (Abschnitt 9) und Merge.
