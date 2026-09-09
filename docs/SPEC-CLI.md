# MailDigest — CLI- und Konfigurations-Spezifikation

> **Dieses Dokument ist der Vertrag.** Alles, was hier steht, muss das Programm genau so
> tun; was hier nicht steht, existiert für den Cold-Tester (docs/TESTING.md §3) nicht.
> Stand: WP9. Änderungen nur zusammen mit dem Code und mit ADR in docs/DECISIONS.md.

## 1. Installation und Aufruf

MailDigest braucht Python ≥ 3.11. Empfohlen ist die Installation mit `pipx install .` —
nur so liegt der Befehl `maildigest` im Suchpfad. Eine Installation in ein virtuelles Umfeld
(`pip install -e .`) legt ihn dagegen nur unter `.venv/bin/maildigest` ab; ohne aktiviertes
venv meldet die Shell dann `command not found`. Der Modulaufruf funktioniert in beiden
Fällen.

Nach der Installation des Pakets gibt es zwei gleichwertige Aufrufformen:

```
maildigest <KOMMANDO> [OPTIONEN]
python -m maildigest <KOMMANDO> [OPTIONEN]
```

Sechs Kommandos: `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test`, `run`.

Ein Aufruf ohne Kommando gibt die Hilfe auf **stdout** aus und endet mit Exit-Code 2.
`maildigest --help` und `maildigest <KOMMANDO> --help` geben Hilfe aus und enden mit
Exit-Code 0.

## 2. Exit-Codes

| Code | Bedeutung |
|------|-----------|
| 0 | Erfolg |
| 1 | Laufzeit- oder Konfigurationsfehler (Verbindung fehlgeschlagen, Datei fehlt, Konfiguration ungültig, Selbsttest fail-closed) |
| 2 | Bedienfehler (unbekanntes Kommando, unbekannte Option, unerlaubter Optionswert — auch ein Zahlenwert außerhalb des erlaubten Bereichs, z. B. `--port 0` / `--port 99999` —, fehlende Pflichtangabe im nicht-interaktiven Modus, zu viele ungültige Eingaben, abgebrochene Eingabe/EOF auf stdin) |

Ein Wert, der als Zahl im erlaubten Bereich liegt, aber als **Konfiguration** unzulässig ist,
bleibt Exit-Code 1 — etwa `--port 143` (Klartext-IMAP) oder `--low-digest-time 25:99`
(ADR-069).

Jede Fehlermeldung geht auf **stderr** und beginnt mit `Fehler: `. Fortschritts- und
Ergebnismeldungen gehen auf **stdout**. Warnungen (Hinweise, die den Ablauf nicht
abbrechen) gehen auf stderr, ohne `Fehler: `-Präfix.

Dazu kommt das **Protokoll** der inneren Schichten (etwa ein Wiederholversuch beim
Modell-Aufruf). Es besteht immer aus JSON-Zeilen im Format aus §4 `run`. Bei `run` gehen
sie auf **stdout** — dort liest der Dienstbetrieb mit. Bei allen anderen Kommandos gehen
sie ab Stufe `WARNING` auf **stderr**, damit stdout ausschließlich die in §4 festgelegte
Ausgabe enthält.

Fehlermeldungen enthalten **niemals** Passwörter, API-Keys, Bot-Tokens oder Webhook-URLs.

## 3. Globale Optionen

Diese Optionen gelten für jedes Kommando und dürfen **vor oder nach** dem Kommandonamen
stehen (`maildigest --config x init` und `maildigest init --config x` sind gleichwertig).

| Option | Wert | Bedeutung |
|---|---|---|
| `--config` | PFAD | Pfad der Konfigurationsdatei. Ohne Angabe gilt die Umgebungsvariable `MAILDIGEST_CONFIG`, sonst `config.toml` im aktuellen Verzeichnis. |
| `--non-interactive` | – | Stellt keine Rückfragen. Es gelten die Optionen der Kommandozeile, die bisherigen Werte der Datei und die Defaults. Fehlt dann eine Pflichtangabe, endet das Kommando mit Exit-Code 2 und nennt die zuständige Option. |
| `--help` | – | Hilfe anzeigen und mit Exit-Code 0 beenden. |

## 4. Kommandos

### `maildigest init`

Legt eine neue Konfigurationsdatei an. Existiert die Datei bereits, bricht das Kommando mit
Exit-Code 1 ab und ändert nichts (außer mit `--force`).

Interaktive Abfragen in dieser Reihenfolge; in Klammern der Default, der bei leerer
Eingabe gilt:

1. `Sprache der Zusammenfassungen (de/en) [de]: `
2. `Länge der Zusammenfassungen (short/medium/long) [medium]: `
3. `Einzeln zustellen ab Wichtigkeit (low/normal/high) [normal]: `
4. `Uhrzeit des täglichen Sammel-Digests (HH:MM) [18:00]: `
5. `Custom-Instructions: ` (eine Zeile; leer = keine; wird auf 2000 Zeichen gekürzt)

Eine unerlaubte Eingabe bei 1–3 wird abgelehnt (`Ungültiger Wert. Erlaubt: …` auf stderr)
und erneut gefragt; nach drei Fehlversuchen endet das Kommando mit Exit-Code 2.

Danach entsteht die Datei mit **Dateirechten 0600** und dem vollständigen Feldsatz aus
Abschnitt 5. Pflichtfelder ohne Default (`[imap] host`, `[imap] username`, `[llm] model`),
alle Secrets und alle Felder ohne Default — dazu gehören alle vier Overrides in
`[llm.critic]` — stehen als auskommentierte Beispielzeilen darin; die Datei ist nach `init`
also gültiges TOML, aber noch keine vollständige Konfiguration. Die Frage-Erläuterung zu den
Custom-Instructions erscheint nur im interaktiven Modus. Die Ausgabe endet
mit der Liste der nächsten Schritte.

Ein unzulässiger Wert (z. B. `--low-digest-time 25:99`) führt zu Exit-Code 1 mit einer
feldbezogenen Meldung; die Datei wird dann **nicht** angelegt.

**Optionen**

| Option | Wert | Bedeutung |
|---|---|---|
| `--force` | – | Vorhandene Datei überschreiben (der bisherige Inhalt geht verloren) |
| `--language` | `de` \| `en` | Antwort auf Frage 1 |
| `--summary-length` | `short` \| `medium` \| `long` | Antwort auf Frage 2 |
| `--min-importance` | `low` \| `normal` \| `high` | Antwort auf Frage 3 |
| `--low-digest-time` | HH:MM | Antwort auf Frage 4 |
| `--instructions` | TEXT | Antwort auf Frage 5 |

### `maildigest connect-mail`

Trägt die Zugangsdaten des Mirror-Postfachs ein, testet die Verbindung, lässt den Ordner
wählen und druckt am Ende die Anleitung zur Weiterleitung im echten Postfach. Setzt eine
vorhandene Konfigurationsdatei voraus (sonst Exit-Code 1 mit Hinweis auf `maildigest init`).

Vor der ersten Frage druckt das Kommando eine Erklärung, was ein IMAP-Host ist, samt
Beispielliste der großen Anbieter. Ist in der Konfiguration noch kein Host hinterlegt und
läuft das Kommando interaktiv, geht dem eine Empfehlung voraus, bei welchem Anbieter sich
ein Spiegel-Postfach mit dem geringsten Aufwand anlegen lässt.

**Anbietererkennung.** Nach Frage 1 wird der eingetragene Wert gegen eine eingebaute
Anbieterliste geprüft (Datenstand 2026-09-09):

* Eine **Mailadresse oder blanke Domain** eines bekannten Anbieters wird in dessen
  IMAP-Host übersetzt; die Übersetzung wird als Zeile `  → IMAP-Host für <Anbieter>: <Host>`
  angezeigt. Ein unbekannter Wert bleibt unverändert — geraten wird nicht.
* Für einen **erkannten Anbieter** folgt eine Anleitung: welche Art Passwort der Server
  verlangt (Konto- oder App-Passwort), die nötigen Schritte, gegebenenfalls ein Direktlink
  und ein Hinweis auf die häufigste Stolperfalle.
* Für einen Anbieter, bei dem Passwort-Anmeldung serverseitig **abgeschaltet** ist —
  derzeit Outlook.com/Hotmail/Live (OAuth2-Pflicht, `LOGINDISABLED`) und Proton Mail
  (kein offenes IMAP) —, endet das Kommando **vor** der Passwortfrage mit Exit-Code 2,
  nennt den Grund und den Ausweg (Spiegel-Postfach woanders anlegen und dorthin
  weiterleiten). Es wird nichts gespeichert.

Der Vorgabewert für Frage 2 ist der Port des erkannten Anbieters (bei allen bekannten
Anbietern 993), sofern die Konfiguration noch keinen Port enthält. Vor Frage 3 wird ein
Beispiel gedruckt, das die vollständige Mailadresse als Benutzernamen zeigt; vor Frage 4
steht bei erkanntem Anbieter, welche Art Passwort erwartet wird.

Abfragen in dieser Reihenfolge:

1. `IMAP-Host [<bisheriger Wert>]: ` — Pflichtangabe
2. `Port [993]: ` — ganze Zahl von 1 bis 65535
3. `Benutzername [<bisheriger Wert>]: ` — Pflichtangabe
4. `Passwort (leer lassen, wenn MAILDIGEST_IMAP_PASSWORD gesetzt werden soll): ` — ohne
   Bildschirmecho, sofern ein Terminal vorhanden ist. Ist die Umgebungsvariable
   `MAILDIGEST_IMAP_PASSWORD` gesetzt, entfällt diese Frage; es wird dann **kein** Passwort
   in die Datei geschrieben. Liegt weder ein eingegebenes noch ein gespeichertes noch ein
   Umgebungs-Passwort vor, endet das Kommando mit Exit-Code 2.
5. Nach erfolgreicher Verbindung: nummerierte Ordnerliste des Servers und
   `Ordner [<Nummer des aktuellen Ordners>]: `

Port 143 (Klartext-IMAP) wird immer abgelehnt (Exit-Code 1). Verbindet sich MailDigest
nicht, endet das Kommando mit Exit-Code 1 und schreibt **nichts** in die Datei; die Meldung
enthält die Serverantwort, danach einen anbieterspezifischen Hinweis, woran die Anmeldung
typischerweise scheitert (bei unbekanntem Anbieter einen allgemeinen), und den Verweis auf
`--no-test`. Lässt sich die Ordnerliste nicht abrufen, bleibt es bei einer
Warnung auf stderr und beim bisherigen Ordner.

Gespeichert wird erst nach dem Test. Die Datei behält die Rechte 0600.

**Optionen**

| Option | Wert | Bedeutung |
|---|---|---|
| `--host` | HOST | Antwort auf Frage 1 |
| `--port` | PORT | Antwort auf Frage 2 |
| `--username` | NAME | Antwort auf Frage 3 |
| `--folder` | ORDNER | Ordner setzen, ohne die Liste zu benutzen |
| `--move-processed-to` | ORDNER | Verarbeitete Mails dorthin verschieben (leerer Wert = nur als gelesen markieren) |
| `--no-test` | – | Angaben ohne Verbindungstest speichern; die Ordnerabfrage entfällt |

Für das Passwort gibt es **bewusst keine Option**: Es kommt aus der Abfrage oder aus
`MAILDIGEST_IMAP_PASSWORD` und landet damit nicht in der Prozessliste oder der
Shell-History.

### `maildigest connect-llm`

Wählt Provider und Modell, hinterlegt den API-Key und macht einen Testaufruf. Setzt eine
vorhandene Konfigurationsdatei voraus.

Abfragen in dieser Reihenfolge:

1. Eine nummerierte **Auswahlliste der Betriebsarten** (interaktiv; mit `--provider` oder
   `--non-interactive` entfällt sie und die Option entscheidet):

   1. kein Sprachmodell — funktioniert sofort, keine Anmeldung nötig (Vorgabe)
   2. Groq — Gratis-Kontingent ohne Kreditkarte
   3. OpenRouter — Gratis-Modelle ohne Kreditkarte
   4. Cerebras — Gratis-Kontingent ohne Kreditkarte
   5. lokales Modell (Ollama/LM Studio/vLLM) — gratis und vollständig privat
   6. Anthropic — kostenpflichtig, beste Qualität
   7. anderer OpenAI-kompatibler Endpunkt — Basis-URL selbst eintragen

   Danach folgt die Anleitung zur gewählten Option: woher der Schlüssel kommt, welche
   Stolperfalle dort typisch ist und — bei den Gratis-Anbietern — wo die aktuellen
   Modell-IDs stehen. Bei Auswahl 1 endet das Kommando sofort mit Exit-Code 0: Es gibt
   weder Modellnamen noch Schlüssel noch Testaufruf, `[llm] model` und `base_url` werden
   geleert und ein etwaiger gespeicherter Schlüssel entfernt.

2. `Modellname (exakte Modell-ID des Anbieters) [<bisheriger Wert>]: ` — Pflichtangabe, es
   gibt bewusst keinen Default
3. nur bei `openai_compatible`:
   `Basis-URL des Endpunkts (z. B. http://localhost:11434/v1) [<bisheriger Wert>]: `
4. `API-Key (leer lassen, wenn MAILDIGEST_LLM_API_KEY gesetzt werden soll oder der Endpunkt
   keinen Key braucht): ` — ohne Bildschirmecho. Ist `MAILDIGEST_LLM_API_KEY` gesetzt,
   entfällt die Frage und es wird kein Key in die Datei geschrieben.

Ist die Basis-URL weder `https://` noch `http://localhost`/`http://127.`, erscheint eine
Warnung auf stderr; das Kommando läuft weiter.

Der Testaufruf schickt einen kurzen, im Programm formulierten Prompt (kein Mail-Inhalt) mit
`max_tokens = 16`. Die Antwort des Modells wird **nicht** angezeigt — gemeldet werden nur
ihre Länge und ob sie das erwartete Wort enthält:
`Antwort erhalten (N Zeichen, erwartete Antwort).` bzw. `… unerwartete Antwort).`
Schlägt der Aufruf fehl, endet das Kommando mit Exit-Code 1 und schreibt nichts.

**Optionen**

| Option | Wert | Bedeutung |
|---|---|---|
| `--provider` | `none` \| `anthropic` \| `openai_compatible` | Antwort auf Frage 1; überspringt die Auswahlliste |
| `--model` | ID | Antwort auf Frage 2 |
| `--base-url` | URL | Antwort auf Frage 3 |
| `--no-test` | – | Ohne Testaufruf speichern |

Für den API-Key gibt es keine Option; er kommt aus der Abfrage oder aus
`MAILDIGEST_LLM_API_KEY`.

### `maildigest connect-messenger`

Richtet den Zustellweg ein und schickt eine Testnachricht. Setzt eine vorhandene
Konfigurationsdatei voraus.

Erste Abfrage immer:
`Messenger (telegram/discord/signal) [telegram]: `

**Telegram.** Anleitung zum Anlegen des Bots über @BotFather — einschließlich des
Schrittes, dem eigenen Bot zuerst selbst eine Nachricht zu schreiben, ohne den die
Chat-Ermittlung im nächsten Schritt nichts finden kann. Dann
`Bot-Token (leer lassen, wenn MAILDIGEST_TELEGRAM_TOKEN gesetzt werden soll): ` (ohne Echo;
entfällt, wenn die Umgebungsvariable gesetzt ist — dann steht kein Token in der Datei).
Ohne `--chat-id` folgt der getUpdates-Flow: Die Ausgabe fordert auf, dem Bot jetzt eine
Nachricht zu schreiben, und fragt Telegram bis zu 10-mal im Abstand von 3 Sekunden ab
(`Noch keine Nachricht empfangen — warte (n/10) …`). Gefunden wird die Chat-ID aus der
ersten passenden Nachricht (`Chat-ID gefunden: <id>`); bei mehreren Chats erscheint eine
nummerierte Auswahl, die **nur** die numerische Chat-ID und den Chat-Typ (`private`,
`group`, `supergroup`, `channel`, `unbekannt`) zeigt — nie einen Namen aus dem Chat. Wird
nichts gefunden, endet das Kommando mit Exit-Code 1 (steht bereits eine Chat-ID in der
Datei, bleibt sie stehen und es gibt nur eine Warnung). Ein abgelehntes Token führt zu
Exit-Code 1 mit dem Hinweis auf das Bot-Token.

Nach erfolgreicher Einrichtung nennt die Ausgabe die optionalen Befehle `/digest` und `/status` samt des Schalters `accept_commands` (ADR-077), damit der Nutzer von ihrer Existenz erfaehrt; die zugestellte Testnachricht enthaelt dieselbe Auskunft.

**Discord.** Hinweis zum Anlegen des Webhooks, dann `Webhook-URL: ` (ohne Echo, weil die
URL selbst das Secret ist). Ohne URL: Exit-Code 2.

**Signal.** Hinweis auf ein laufendes `signal-cli --daemon --socket <pfad>`, dann
`Pfad des signal-cli-Sockets [<bisheriger Wert>]: ` (Pflichtangabe). Das Kommando setzt
`[messenger.signal] enabled = true`. Zugestellt wird an „Notiz an mich".

Danach folgt (außer bei `--no-test`) ein Erreichbarkeitstest und **eine Testnachricht**:

```
✅ MailDigest Testnachricht
Die Zustellung funktioniert — ab jetzt landen hier deine Mail-Zusammenfassungen
```

Ist der Dienst nicht erreichbar oder scheitert die Zustellung, endet das Kommando mit
Exit-Code 1 und schreibt nichts in die Datei.

**Optionen**

| Option | Wert | Bedeutung |
|---|---|---|
| `--messenger` | `telegram` \| `discord` \| `signal` | Antwort auf die erste Frage |
| `--chat-id` | ID | Telegram-Chat-ID direkt setzen; der getUpdates-Flow entfällt |
| `--webhook-url` | URL | Discord-Webhook-URL |
| `--signal-socket` | PFAD | Pfad des signal-cli-Sockets |
| `--no-test` | – | Ohne Erreichbarkeitstest und ohne Testnachricht speichern |

Für das Bot-Token gibt es keine Option; es kommt aus der Abfrage oder aus
`MAILDIGEST_TELEGRAM_TOKEN`.

### `maildigest test`

Ohne `--dry-run` geht der eigentlichen Nachricht ein kurzer Vorspann voraus (Kennzeichnung als Selbsttest mit dem Hinweis, dass die Mail nicht aus dem Postfach stammt). Ohne ihn waere die zugestellte Zusammenfassung von einer echten nicht zu unterscheiden, und der Nutzer suchte im Postfach nach einer Mail, die es nie gab.

Ende-zu-Ende-Selbsttest: verarbeitet **eine `.eml`-Datei** durch dieselbe Pipeline wie im
Betrieb (Sanitizer → Summarizer → Kritiker → Output-Sanitizer → Messenger) und stellt das
Ergebnis zu. Das Postfach wird dabei nicht angefasst.

Ohne `--eml` wird die mitgelieferte Beispielmail benutzt (eine harmlose deutsche
Terminmail mit einem Link, ohne Anhang). Mit `--eml <pfad>` wird eine eigene Datei im
RFC-822-Format eingespeist — das ist das **Einspeise-Verfahren** für eigene Testmails
(Angriffsmails inklusive): Datei schreiben, `maildigest test --eml datei.eml` aufrufen,
Ergebnis im Messenger bzw. mit `--dry-run` auf stdout ansehen.

Zwei Eigenschaften sind für den Test wichtig:

* Der Selbsttest benutzt eine **eigene, temporäre State-Datenbank**. Er verändert weder
  den Dedupe-Stand noch die Zustell-Warteschlange des Betriebs; dieselbe Datei lässt sich
  beliebig oft einspeisen.
* Er ignoriert `[general] deliver_min_importance` (er stellt ab `low` zu), damit auch eine
  als unwichtig eingestufte Testmail sichtbar wird statt im Sammel-Digest zu landen.

Die Ausgabe hat fünf nummerierte Schritte:

```
1/5 Konfiguration geladen: <pfad>
2/5 Testmail gelesen: <pfad|mitgelieferte Beispielmail> (N Bytes)
3/5 Pipeline läuft (Sanitizer → Summarizer → Kritiker → Zustellung) …
4/5 Sanitizer: N Zeichen Klartext, N Anhänge (N verarbeitet), N Links entfernt, N Steuerzeichen entfernt
  Summarizer: Wichtigkeit=<high|normal|low>, Injection-Verdacht=<ja|nein>
  Kritiker: Phishing-Risiko=<none|low|high>, Zusammenfassung korrekt=<ja|nein>
5/5 Zugestellt (N Teile). Schau in deinen Messenger.
```

Bei genau einem Teil lautet die Klammer `(1 Teil)` — hier und ebenso in
`5/5 Nachricht erzeugt (N Teile) — Trockenlauf, nicht gesendet:`. Ebenso stehen in Zeile 4/5
die deutschen Einzahlformen, wenn der Zähler 1 ist: `1 Anhang`, `1 Link entfernt`.

Weder der Mail-Text noch die Modellausgabe erscheinen dabei auf dem Terminal; nur die
fertige, sanitisierte Nachricht bei `--dry-run`. Mit `--dry-run` steht zwischen Schritt 2
und 3 zusätzlich die Zeile `    Trockenlauf: es wird nichts an den Messenger geschickt
(--dry-run).`, und Schritt 5 lautet
`5/5 Nachricht erzeugt (N Teile) — Trockenlauf, nicht gesendet:`, gefolgt von der
Nachricht selbst.

Exit-Codes: 0, wenn die Nachricht erzeugt **und** zugestellt wurde (bzw. bei `--dry-run`
erzeugt und ausgegeben). 1, wenn die Pipeline fail-closed endete (dann steht in Schritt 5
`Fail-closed: Stufe <stufe>, Grund <grund>`, und der Messenger bekommt die Metadaten-Notiz
aus Abschnitt 6) oder wenn die Zustellung nicht bestätigt wurde. 1 auch bei fehlender,
unlesbarer oder unvollständiger Konfiguration und bei nicht lesbarer `--eml`-Datei.

Endet die Pipeline mit `--dry-run` fail-closed, geht **nichts** an den Messenger. Schritt 5
lautet dann

```
5/5 Fail-closed: Stufe <stufe>, Grund <grund>.
    Metadaten-Notiz erzeugt — Trockenlauf, nicht gesendet:
```

gefolgt von der Notiz selbst auf stdout; auf stderr steht `Selbsttest fehlgeschlagen — es
wurde nur die Metadaten-Notiz erzeugt (zugestellt: nein — Trockenlauf).` Ohne `--dry-run`
steht dort `(zugestellt: ja)` nur, wenn die Notiz den Messenger tatsächlich erreicht hat;
blieb sie in der Warteschlange liegen, steht dort `(zugestellt: nein)` (ADR-071).

**Optionen**

| Option | Wert | Bedeutung |
|---|---|---|
| `--eml` | PFAD | Eigene `.eml`-Datei statt der Beispielmail |
| `--dry-run` | – | Nachricht nur auf stdout ausgeben, nichts zustellen |

### `maildigest run`

Der Betrieb. Ohne Optionen läuft MailDigest im Vordergrund und pollt das Postfach alle
`[imap] poll_interval_seconds` Sekunden; `SIGINT` (Strg-C) und `SIGTERM` beenden den
laufenden Zyklus sauber und danach den Prozess (Exit-Code 0).

Mit `--once` wird genau ein Zyklus ausgeführt (Warteschlange leeren → einmal pollen →
Warteschlange leeren → Sammel-Digest prüfen), dann endet der Prozess. Das ist die
Cron-taugliche Form.

Ausgabe: strukturierte **JSON-Zeilen auf stdout** (ein Objekt je Ereignis, Felder `ts`,
`level`, `logger`, `event` und ereignisabhängige Zusatzfelder). Der Schwellwert kommt aus
`[general] log_level`. Mail-Inhalte, Betreffzeilen und Secrets erscheinen dort nie — nur
gekürzte Hashes, Absender-Domains, Statuswerte und Zähler. Bei `--once` kommt zusätzlich
eine deutsche Bilanzzeile auf **stderr**:

```
Lauf beendet: N Mails geholt, N verarbeitet, N Duplikate, N Fehler, N Nachrichten zugestellt, N in der Warteschlange.
```

`N Nachrichten zugestellt` zählt **alle** in diesem Lauf zugestellten Nachrichten: direkt
zugestellte Einzelnachrichten, zugestellte Metadaten-Notizen, den Sammel-Digest und aus der
Warteschlange nachgelieferte Nachrichten (ADR-070). Nachrichten, die in der Warteschlange
verbleiben, zählen erst in dem Lauf, in dem sie durchgehen; bis dahin erscheinen sie unter
`N in der Warteschlange`.

Exit-Codes: 0 bei sauberem Ende, 1 bei unvollständiger Konfiguration, unbenutzbarer
State-Datenbank oder — nur bei `--once` — nicht erreichbarem Postfach
(`Fehler: Postfach nicht erreichbar: …`). Ein fehlgeschlagenes Verschieben ist **kein**
solcher Fall: Es betrifft eine einzelne Mail, wird als `imap_postprocess_failed`
protokolliert und bricht den Lauf nicht ab (ADR-065). Im Dauerbetrieb ist ein Postfach-Ausfall kein
Abbruch: Es wird mit wachsendem Abstand (5 s, 10 s, 20 s … maximal 10 Minuten) neu
verbunden.

**Optionen**

| Option | Wert | Bedeutung |
|---|---|---|
| `--once` | – | Einen Zyklus ausführen und beenden |

## 5. Konfigurationsdatei

Format TOML, UTF-8. Dateirechte **0600** — jedes Kommando, das schreibt, setzt sie erneut.
Unbekannte Felder sind ein Fehler (Tippfehler-Schutz): Beim Laden meldet MailDigest
`[sektion] feld: Unbekanntes Feld — Tippfehler?` und endet mit Exit-Code 1.

Alle Felder mit ihren Defaults:

| Sektion / Feld | Typ / Werte | Default | Bedeutung |
|---|---|---|---|
| `[general] language` | Text | `"de"` | Sprache der Zusammenfassungen |
| `[general] summary_length` | `short`/`medium`/`long` | `"medium"` | Ausführlichkeit |
| `[general] deliver_min_importance` | `low`/`normal`/`high` | `"normal"` | Ab dieser Wichtigkeit wird einzeln zugestellt; darunter Sammel-Digest |
| `[general] low_digest_time` | `HH:MM` | `"18:00"` | Uhrzeit des Sammel-Digests (lokale Zeit des Servers) |
| `[general] state_db` | Pfad | `""` | Leer = `state.db` neben der Konfigurationsdatei; relative Pfade gelten relativ zu deren Verzeichnis |
| `[general] log_level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `"INFO"` | Log-Schwellwert. `DEBUG` schaltet Tracebacks frei, die Mail-Inhalte enthalten können — solche Logs sind vertraulich |
| `[imap] host` | Text | — | **Pflicht.** Hostname des Mirror-Postfachs |
| `[imap] port` | 1–65535 | `993` | Nur IMAPS; 143 wird abgelehnt |
| `[imap] username` | Text | — | **Pflicht.** |
| `[imap] password` | Text | — | Alternativ `MAILDIGEST_IMAP_PASSWORD` |
| `[imap] folder` | Text | `"INBOX"` | Gelesener Ordner |
| `[imap] poll_interval_seconds` | ≥ 5 | `120` | Abrufintervall im Dauerbetrieb |
| `[imap] move_processed_to` | Text | `""` | Leer = nur Gelesen-Flag setzen. Der Ordner muss auf dem Server existieren, und der Server muss die MOVE-Erweiterung beherrschen. Fehlt eines von beidem, bleibt die Mail als gelesen im Quellordner liegen und der Lauf protokolliert `imap_postprocess_failed` mit dem Grund — der Zyklus läuft weiter (ADR-064/ADR-065) |
| `[llm] provider` | `none`/`anthropic`/`openai_compatible` | `"none"` | Anbieter. `none` = Betrieb ohne Sprachmodell (ADR-076): zugestellt wird ein beschrifteter Auszug statt einer Zusammenfassung, alle deterministischen Warnungen bleiben |
| `[llm] model` | Text | `""` | **Pflicht, sobald `provider` nicht `none` ist.** Exakte Modell-ID; bewusst kein Default |
| `[llm] api_key` | Text | — | Alternativ `MAILDIGEST_LLM_API_KEY`. Für `anthropic` erforderlich, für lokale Server meist nicht |
| `[llm] base_url` | URL | `""` | Endpunkt für `openai_compatible` |
| `[llm] max_tokens` | ≥ 1 | `1024` | Obergrenze je Antwort |
| `[llm.critic] provider` | wie `[llm]` | erbt | Override für den Kritiker |
| `[llm.critic] model` | Text | erbt | Override |
| `[llm.critic] base_url` | URL | erbt | Override |
| `[llm.critic] max_tokens` | ≥ 1 | erbt | Override |
| `[summarizer] instructions` | Text | `""` | Custom-Instructions: was ist wichtig, worauf achten. Steuert Stil und Wichtigkeit, kann die Sicherheitsregeln nicht abschalten |
| `[links] footnote` | `true`/`false` | `false` | Defangte Link-Liste als Fußnote an die Nachricht hängen |
| `[messenger] active` | `telegram`/`discord`/`signal` | `"telegram"` | Aktiver Zustellweg |
| `[messenger.telegram] token` | Text | — | Alternativ `MAILDIGEST_TELEGRAM_TOKEN` |
| `[messenger.telegram] chat_id` | Text | `""` | Ziel-Chat; `connect-messenger` ermittelt ihn |
| `[messenger.telegram] accept_commands` | true/false | `false` | Ob MailDigest Befehle aus dem Chat annimmt (ADR-077). Eingeschaltet reagiert `run` auf `/digest` (sofortiger Abrufzyklus) und `/status` (Kurzbericht), **nur** aus `chat_id` und **nur** auf diese beiden Wörter; jeder andere Text wird verworfen und erreicht kein Sprachmodell |
| `[messenger.discord] webhook_url` | URL | — | Webhook des Kanals (ist selbst ein Secret) |
| `[messenger.signal] enabled` | `true`/`false` | `false` | Signal-Adapter freischalten |
| `[messenger.signal] signal_cli_socket` | Pfad | `""` | Socket von `signal-cli --daemon` |
| `[limits] max_mail_bytes` | ≥ 1 | `26214400` | Größte verarbeitete Mail (25 MB); darüber nur Metadaten-Notiz |
| `[limits] max_text_chars` | ≥ 1 | `30000` | Klartext-Budget über Body und Anhänge |
| `[limits] pdf_max_input_bytes` | ≥ 1 | `10485760` | Größtes verarbeitetes PDF (10 MB) |
| `[limits] pdf_max_output_chars` | ≥ 1 | `50000` | Textausbeute je PDF |
| `[limits] pdf_timeout_seconds` | ≥ 1 | `20` | Zeitlimit der PDF-Extraktion |
| `[limits] max_mime_depth` | ≥ 1 | `10` | Maximale MIME-Verschachtelung |
| `[limits] max_attachments_processed` | ≥ 0 | `20` | Inhaltlich verarbeitete Anhänge je Mail |

**Umgebungsvariablen**

| Variable | Wirkung |
|---|---|
| `MAILDIGEST_CONFIG` | Pfad der Konfigurationsdatei, wenn `--config` fehlt |
| `MAILDIGEST_IMAP_PASSWORD` | Überschreibt `[imap] password` |
| `MAILDIGEST_LLM_API_KEY` | Überschreibt `[llm] api_key` |
| `MAILDIGEST_TELEGRAM_TOKEN` | Überschreibt `[messenger.telegram] token` |

Eine gesetzte Variable schlägt immer den Dateiwert; ein leerer Wert wird ignoriert.

## 6. Nachrichtenformat

Jede Zustellung ist **reiner Text**. Sie enthält nie einen klickbaren Link, nie einen
Anhang, nie HTML oder Markdown. Markdown-Konstrukte werden neutralisiert, auch die nur am
Zeilenanfang wirkenden (Überschriften, Listen, Zitate, Discord-Subtext) und Unterstriche am
Wortrand; Aufzählungen erscheinen als `•`, nummerierte Zeilen als `12 · …`. `@everyone` und
`@here` erscheinen als `(at)everyone`/`(at)here`. Die strukturgebenden Zeilenanfänge (`⚠️`,
`📧`, `📎`, `🔍 Hinweise:`, `Von:`) erzeugt ausschließlich das Programm; identische Anfänge in
Modelltext werden neutralisiert (ADR-062). Alle Domains und Dateinamen erscheinen mit gebrochenem
Punkt (`beispiel[.]de`, `rechnung[.]pdf`), weil Messenger nackte Domains automatisch
verlinken. Ist die Nachricht länger als das Limit des Zielsystems (Telegram 4096, Discord
2000, Signal 2000 Zeichen), wird sie an Zeilengrenzen auf mehrere Nachrichten aufgeteilt.

**Normale Zustellung**

```
⚠️ PHISHING-VERDACHT: <Gründe, kommasepariert, max. 5>   ← nur bei Phishing-Risiko high
📧 <Kopfzeile> [wichtig]                                  ← Tag nur bei Wichtigkeit high
Von: <Anzeigename> (<domain>) · <TT.MM. HH:MM>            ← ohne Date-Header: „Datum unbekannt"
<Zusammenfassung>
— <datei>: <1–2 Sätze je verarbeitetem Anhang>
📎 Nicht verarbeitet: <datei (größe)>, … [und N weitere]
🔍 Hinweise: <Injection-Verdacht; Auth-Fehler; Punycode; gemischte Schriftsysteme;
              versteckter Text im HTML entfernt; HTML-Teil weicht vom Textteil ab;
              Reply-To-/Return-Path-Abweichung; Text gekürzt; Kritiker-Gründe bei Risiko low>
<Link-Fußnote (defanged), eine Adresse je Zeile>          ← nur bei [links] footnote = true
```

Zeilen ohne Inhalt entfallen. Einzellimits: Kopfzeile 120, Zusammenfassung 3000,
Anhangs-Zusammenfassung 400, Kritiker-Grund 200, Anzeigename 80, Domain 100, Dateiname
80 Zeichen; höchstens 10 namentlich genannte Anhänge und 5 Banner-Gründe. Gekürzt wird
mit `…`.

**Metadaten-Notiz (fail-closed)** — immer genau diese fünf Zeilen:

```
⚠️ Mail konnte nicht sicher verarbeitet werden — kein Inhalt zugestellt.
Von: <domain>
Betreff: <Betreff>
Stufe: <sanitize|summarize|critic|compose|deliver> · Grund: <fehlerklasse>
Zum Lesen ins echte Postfach schauen.
```

Fehlerklassen: `sanitize_error`, `llm_timeout`, `llm_rate_limited`, `llm_invalid_response`,
`llm_transport_error`, `schema_invalid`, `summary_inaccurate`, `delivery_error`,
`state_error`, `delivery_failed` sowie `<stufe>_error` für Unbekanntes.

**Täglicher Sammel-Digest** — eine Nachricht ab `[general] low_digest_time`, gruppiert nach
Kategorie (größte Gruppe zuerst), je Mail eine Zeile; ab 60 Mails wird der Rest gezählt.
Eine leere Warteschlange erzeugt keine Nachricht.

```
🗂 12 unwichtige Mails: 8 newsletter, 3 benachrichtigung, 1 sonstiges

newsletter (8):
• Wochenrückblick KW 36 (news[.]example[.]org)
…
… und N weitere
```

## 7. Zusicherungen des Programms

Diese Punkte sind Teil des Vertrags und in REQUIREMENTS.md als F-SEC-* nachlesbar:

1. Im Mirror-Postfach wird **nichts gelöscht**. Geschrieben werden nur das Gelesen-Flag und
   — falls konfiguriert — das Verschieben in `move_processed_to`. Technisch sind das genau
   zwei IMAP-Kommandos: `UID STORE +FLAGS (\Seen)` und `UID MOVE`. Ein `EXPUNGE` wird nie
   gesendet — es würde auch fremde, von anderen Programmen als `\Deleted` markierte
   Nachrichten endgültig löschen (ADR-064).
2. Kein Sprachmodell sieht rohes HTML, rohe MIME-Teile oder Anhangs-Binärdaten.
3. Anhänge werden nie zugestellt. Inhaltlich verarbeitet werden nur `text/plain`-Dateien
   und PDFs (nach Prüfung der Magic-Bytes); alles andere erscheint nur als Zeile
   „Nicht verarbeitet".
4. Schlägt irgendeine Stufe fehl, kommt die Metadaten-Notiz — nie ungeprüfter Inhalt und
   nie stilles Verschwinden.
5. Secrets stehen ausschließlich in der Konfigurationsdatei (0600) oder in
   Umgebungsvariablen — nie in Prompts, Logs, Fehlermeldungen oder der State-Datenbank.
