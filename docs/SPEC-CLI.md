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

Jede Fehlermeldung geht auf **stderr** und beginnt mit `Error: `. Fortschritts- und
Ergebnismeldungen gehen auf **stdout**. Warnungen (Hinweise, die den Ablauf nicht
abbrechen) gehen auf stderr, ohne `Error: `-Präfix.

**Sprache der Ausgabe:** Jeder Text, den das Programm ausgibt — Abfragen, Schritt- und
Bilanzzeilen, Fehler- und Warnmeldungen sowie der gesamte Rahmen jeder zugestellten
Nachricht — ist **englisch**, unabhängig von jeder Einstellung und jeder Locale.
`[general] language` steuert ausschließlich die Sprache der vom Sprachmodell erzeugten
Textfelder (Zusammenfassung, Kopfzeile, Begründungen, Kategorie); ohne Sprachmodell gibt es
diese Felder nicht. Dieses Dokument ist damit der wörtliche Vertrag der **englischen**
Ausgabe (ADR-083).

Ein `SIGINT` (Strg-C) während einer Abfrage beendet das Kommando mit `Error: Aborted.` auf
stderr und **Exit-Code 1**; geschrieben wird dabei nichts.

Dazu kommt das **Protokoll** der inneren Schichten (etwa ein Wiederholversuch beim
Modell-Aufruf). Es besteht immer aus JSON-Zeilen im Format aus §4 `run`. Bei `run` gehen
sie auf **stdout** — dort liest der Dienstbetrieb mit. Bei allen anderen Kommandos gehen
sie ab Stufe `WARNING` auf **stderr**, damit stdout ausschließlich die in §4 festgelegte
Ausgabe enthält.

Fehlermeldungen enthalten **niemals** Passwörter, API-Keys, Bot-Tokens oder Webhook-URLs.
Zitiert eine Gegenstelle den gesendeten API-Key in ihrer Fehlerantwort, wird er vor der
Ausgabe durch `***` ersetzt (auch als Präfix ab acht Zeichen) — die Zusage hängt nicht am
Wohlverhalten des Anbieters.

Jede Fehlerzeile läuft vor der Ausgabe durch eine **Zeichen-Allowlist** (druckbares ASCII,
Umlaute/ß, die üblichen Satzzeichen; alles andere wird zu `·`, ADR-055). Damit kann ein
Text, der von einer Gegenstelle stammt — Fehlertext des Modell-Anbieters, Ordnername des
IMAP-Servers —, keine ANSI-/Steuersequenz auf das Terminal bringen und dort Bildschirm,
Farbe, Fenstertitel oder eine erfundene Programmmeldung setzen.

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

1. `Language of the summaries (de/en) [de]: `
2. `Length of the summaries (short/medium/long) [medium]: `
3. `Deliver individually from importance (low/normal/high) [normal]: `
4. `Time of the daily digest (HH:MM) [18:00]: `
5. `Custom-Instructions: ` (eine Zeile; leer = keine; wird auf 2000 Zeichen gekürzt);
   davor steht im interaktiven Modus die Erläuterung
   `Custom instructions: one line about what matters to you (leave empty for none).`

Eine unerlaubte Eingabe bei 1–3 wird abgelehnt (`Invalid value. Allowed: …` auf stderr)
und erneut gefragt; nach drei Fehlversuchen endet das Kommando mit
`Error: Too many invalid entries — aborted.` und Exit-Code 2.

Danach entsteht die Datei mit **Dateirechten 0600** und dem vollständigen Feldsatz aus
Abschnitt 5: Jedes Feld mit Default steht mit diesem Default darin. Pflichtfelder ohne
Default (`[imap] host`, `[imap] username`), alle Secrets und alle übrigen Felder ohne
Default — dazu gehören alle vier Overrides in `[llm.critic]` — stehen als auskommentierte
Beispielzeilen darin; die Datei ist nach `init` also gültiges TOML, aber noch keine
vollständige Konfiguration. `[llm] model` gehört **nicht** dazu: Es hat laut Abschnitt 5
den Default `""` und ist erst Pflicht, sobald `provider` nicht `none` ist (ADR-076). Die
Frage-Erläuterung zu den Custom-Instructions erscheint nur im interaktiven Modus.

Die erste Zeile lautet `Setting up MailDigest — configuration: <pfad>`, nach dem Schreiben
folgt `Configuration created: <pfad> (file mode 0600).` Danach folgt ein Absatz, der
einordnet, was MailDigest ohne Sprachmodell zustellt; **zum Schluss** steht die Liste der
nächsten Schritte. Die Ausgabe endet mit dieser Liste:

```
Next steps:
  1) maildigest connect-mail        (mirror mailbox)
  2) maildigest connect-messenger   (Telegram/Discord/Signal)
  3) maildigest connect-llm         (optional: real summaries)
  4) maildigest test                (self-test)
  5) maildigest run                 (continuous operation)
```

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
  IMAP-Host übersetzt; die Übersetzung wird als Zeile `  -> IMAP host for <Anbieter>: <Host>`
  angezeigt. Ein unbekannter Wert bleibt unverändert — geraten wird nicht.
* Für einen **erkannten Anbieter** folgt eine Anleitung: welche Art Passwort der Server
  verlangt (Konto- oder App-Passwort), die nötigen Schritte, gegebenenfalls ein Direktlink
  und ein Hinweis auf die häufigste Stolperfalle.
* Für einen Anbieter, bei dem Passwort-Anmeldung serverseitig **abgeschaltet** ist —
  derzeit Outlook.com/Hotmail/Live (OAuth2-Pflicht, `LOGINDISABLED`) und Proton Mail
  (kein offenes IMAP) —, endet das Kommando **vor** der Passwortfrage mit Exit-Code 2,
  nennt den Grund und den Ausweg (Spiegel-Postfach woanders anlegen und dorthin
  weiterleiten). Es wird nichts gespeichert. Das gilt für die blanke Domain **und** für
  eine Mailadresse dieses Anbieters (`me@outlook.com`) gleichermaßen, auch zusammen mit
  `--no-test`.

Der Vorgabewert für Frage 2 ist der Port des erkannten Anbieters (bei allen bekannten
Anbietern 993), sofern die Konfiguration noch keinen Port enthält. Vor Frage 3 wird ein
Beispiel gedruckt, das die vollständige Mailadresse als Benutzernamen zeigt; vor Frage 4
steht bei erkanntem Anbieter, welche Art Passwort erwartet wird.

Abfragen in dieser Reihenfolge:

1. `IMAP host [<bisheriger Wert>]: ` — Pflichtangabe
2. `Port [993]: ` — ganze Zahl von 1 bis 65535
3. `Username [<bisheriger Wert>]: ` — Pflichtangabe
4. `Password (leave empty to use MAILDIGEST_IMAP_PASSWORD instead): ` — ohne
   Bildschirmecho, sofern ein Terminal vorhanden ist. Ist die Umgebungsvariable
   `MAILDIGEST_IMAP_PASSWORD` gesetzt, entfällt diese Frage; es wird dann **kein** Passwort
   in die Datei geschrieben. Liegt weder ein eingegebenes noch ein gespeichertes noch ein
   Umgebungs-Passwort vor, endet das Kommando mit Exit-Code 2.
5. Nach erfolgreicher Verbindung: die Zeile `Which folder should MailDigest read?`, die
   nummerierte Ordnerliste des Servers und `Folder [<Nummer des aktuellen Ordners>]: `

Port 143 (Klartext-IMAP) wird immer abgelehnt (Exit-Code 1). Verbindet sich MailDigest
nicht, endet das Kommando mit Exit-Code 1 und schreibt **nichts** in die Datei. Die
Meldung nennt Host, Port, Ordner und die **Fehlerklasse**; der Antworttext des Servers
wird bewusst **nicht** ausgegeben — er kann den gesendeten Benutzernamen zitieren (I5,
docs/SECURITY.md §6). Danach folgt eine Erklärung, die zur Fehlerklasse passt:

* **Anmeldefehler** (der Server hat die Zugangsdaten abgelehnt): der anbieterspezifische
  Hinweis, woran die Anmeldung typischerweise scheitert — meist ein separat erzeugtes
  App-Passwort (bei unbekanntem Anbieter ein allgemeiner Hinweis).
* **Transportfehler** (Verbindung abgelehnt, Zeitüberschreitung, TLS): der Hinweis, dass
  gar kein Login versucht wurde und Host, Port und Netzverbindung zu prüfen sind.

Zum Schluss steht in beiden Fällen der Verweis auf `--no-test`
(`With --no-test the values can also be saved without testing them.`). Lässt sich die
Ordnerliste nicht abrufen, bleibt es bei der Warnung
`Folder list unavailable (<klasse>); keeping the current setting.` auf stderr und beim
bisherigen Ordner. Existiert der eingestellte Ordner nicht, steht auf stderr
`The configured folder "<name>" does not exist on the server.`; läuft das Kommando
nicht-interaktiv, wird danach die nummerierte Ordnerliste auf stdout gedruckt und
anschließend auf stderr darauf verwiesen (`Keeping it unchanged — pick one of the folders
listed on stdout with --folder, otherwise the next run will fail.`); der eingestellte
Ordner bleibt unverändert.

Weitere wörtliche Zeilen dieses Kommandos auf stdout:
`Connecting the mirror mailbox (IMAPS only, certificate check always on)`,
`Detected: <Anbieter>`, `Username = <Beispiel>`, `Password = <Art des Passworts>.`,
`Password: from MAILDIGEST_IMAP_PASSWORD (not written to the file)`, `Connecting ...`,
`Connection established.`, `Connection test skipped (--no-test).` und
`Saved to <pfad> (file mode 0600).`

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

   ```
    1) No language model — works immediately, nothing to sign up for (default)
    2) Groq — free tier, no credit card (fast, OpenAI-compatible)
    3) OpenRouter — free models, no credit card
    4) Cerebras — free tier, no credit card
    5) Local model (Ollama, LM Studio, vLLM) — free and fully private
    6) Anthropic — paid, best summary quality
    7) Other OpenAI-compatible endpoint — enter the base URL yourself
   ```

   Die Auswahl selbst lautet `Option [1]: `.

   Danach folgt die Anleitung zur gewählten Option: woher der Schlüssel kommt, welche
   Stolperfalle dort typisch ist und — bei den Gratis-Anbietern — wo die aktuellen
   Modell-IDs stehen. Wird die Liste mit `--provider` oder `--non-interactive`
   übersprungen, entscheidet der Optionswert: `none` und `anthropic` führen zur jeweiligen
   Anleitung, `openai_compatible` zur **generischen** Anleitung für OpenAI-kompatible
   Endpunkte. Ein anbieterspezifischer Erklärtext und eine anbieterspezifische `base_url`
   (Groq, OpenRouter, Cerebras, Ollama) kommen dann **nicht** zum Zug: `base_url` bleibt
   auf dem bisherigen Dateiwert bzw. leer, sofern `--base-url` nichts anderes sagt.

   Bei Auswahl 1 endet das Kommando sofort mit Exit-Code 0: Es gibt weder Modellnamen
   noch Schlüssel noch Testaufruf, `[llm] model` und `base_url` werden geleert und ein
   etwaiger gespeicherter Schlüssel entfernt.

2. `Model name (exact model ID used by the provider) [<bisheriger Wert>]: ` —
   Pflichtangabe, es gibt bewusst keinen Default
3. nur bei `openai_compatible`:
   `Base URL of the endpoint (e.g. http://localhost:11434/v1) [<bisheriger Wert>]: `
4. `API key (leave empty to use MAILDIGEST_LLM_API_KEY, or if the endpoint needs no key): `
   — ohne Bildschirmecho. Ist `MAILDIGEST_LLM_API_KEY` gesetzt, entfällt die Frage, es
   erscheint `API key: from MAILDIGEST_LLM_API_KEY (not written to the file)` und es wird
   kein Key in die Datei geschrieben.

Ist die Basis-URL weder `https://` noch `http://localhost`/`http://127.`, erscheint eine
Warnung auf stderr; das Kommando läuft weiter.

Der Testaufruf (`Test call ...` auf stdout, `Test call skipped (--no-test).` mit
`--no-test`) schickt einen kurzen, im Programm formulierten Prompt (kein Mail-Inhalt) mit
`max_tokens = 16`. Die Antwort des Modells wird **nicht** angezeigt — gemeldet werden nur
ihre Länge und ob sie das erwartete Wort enthält:
`Response received (N characters, expected reply).` bzw. `… unexpected reply).`
Schlägt der Aufruf fehl, endet das Kommando mit Exit-Code 1
(`Error: Test call failed (<Fehlerklasse>): …` plus `Check the model name, API key and base
URL.`) und schreibt nichts. Bei Auswahl 1 lauten die beiden letzten Zeilen
`Saved to <pfad> (file mode 0600).` und `MailDigest now runs without a language model. Run
this command again at any time to connect one.`

**Optionen**

| Option | Wert | Bedeutung |
|---|---|---|
| `--provider` | `none` \| `anthropic` \| `openai_compatible` | Setzt `[llm] provider`; überspringt die Auswahlliste. Belegt **keine** anbieterspezifische `base_url` vor — dafür ist `--base-url` da |
| `--model` | ID | Antwort auf Frage 2 |
| `--base-url` | URL | Antwort auf Frage 3 |
| `--no-test` | – | Ohne Testaufruf speichern |

Für den API-Key gibt es keine Option; er kommt aus der Abfrage oder aus
`MAILDIGEST_LLM_API_KEY`.

### `maildigest connect-messenger`

Richtet den Zustellweg ein und schickt eine Testnachricht. Setzt eine vorhandene
Konfigurationsdatei voraus.

Die erste Zeile lautet `Connecting the messenger`, danach immer die Abfrage
`Messenger (telegram/discord/signal) [telegram]: `

**Telegram.** Anleitung zum Anlegen des Bots über @BotFather — einschließlich des
Schrittes, dem eigenen Bot zuerst selbst eine Nachricht zu schreiben, ohne den die
Chat-Ermittlung im nächsten Schritt nichts finden kann. Dann
`Bot token (leave empty to use MAILDIGEST_TELEGRAM_TOKEN instead): ` (ohne Echo;
entfällt, wenn die Umgebungsvariable gesetzt ist — dann erscheint `Bot token: from
MAILDIGEST_TELEGRAM_TOKEN (not written to the file)` und es steht kein Token in der Datei).
Ohne `--chat-id` folgt der getUpdates-Flow: Die Ausgabe fordert mit
`Now send your bot a message in Telegram (e.g. /start).` dazu auf, dem Bot jetzt eine
Nachricht zu schreiben, und fragt Telegram bis zu 10-mal im Abstand von 3 Sekunden ab
(`No message received yet — waiting (n/10) ...`). Gefunden wird die Chat-ID aus der
ersten passenden Nachricht (`Chat ID found: <id>`); bei mehreren Chats erscheint nach
`Several chats found — which one should it be?` eine nummerierte Auswahl (`Chat [1]: `),
die **nur** die numerische Chat-ID und den Chat-Typ (`private`, `group`, `supergroup`,
`channel`, `unknown`) zeigt — nie einen Namen aus dem Chat. Wird nichts gefunden, endet das
Kommando mit Exit-Code 1 (`Error: No message to the bot found. …`); steht bereits eine
Chat-ID in der Datei, bleibt sie stehen und es gibt nur die Warnung
`No new message found — keeping the existing chat ID.` Ein abgelehntes Token führt zu
Exit-Code 1 mit `Error: Telegram request failed: … / Is the bot token correct?`.

Nach jeder erfolgreichen Telegram-Einrichtung nennt die Ausgabe **genau einmal** die optionalen Befehle `/digest` und `/status` samt des Schalters `accept_commands` (ADR-077), damit der Nutzer von ihrer Existenz erfaehrt — auf dem getUpdates-Weg ebenso wie mit `--chat-id`; die zugestellte Testnachricht enthaelt dieselbe Auskunft. Fehlt `[messenger.telegram] accept_commands` in einer aelteren Datei, traegt `connect-messenger` den Default nach, damit der Feldsatz aus Abschnitt 5 vollstaendig bleibt.

**Discord.** Hinweis zum Anlegen des Webhooks, dann `Webhook URL: ` (ohne Echo, weil die
URL selbst das Secret ist). Ohne URL: Exit-Code 2.

**Signal.** Der Hinweis
`Prerequisite: \`signal-cli --daemon --socket <path>\` is already running.`, dann
`Path of the signal-cli socket [<bisheriger Wert>]: ` (Pflichtangabe). Das Kommando setzt
`[messenger.signal] enabled = true`. Zugestellt wird an „Notiz an mich".

Danach folgt (außer bei `--no-test`; sonst `Test message skipped (--no-test).`) ein
Erreichbarkeitstest und **eine Testnachricht**:

```
✅ MailDigest test message
Delivery works — your mail summaries will arrive here from now on
```

Bei Telegram hängt daran — und nur dort — der Befehls-Absatz:

```

This chat can also trigger MailDigest:
/digest — fetch and summarise right now
/status — short report on what is waiting
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

Ohne `--dry-run` geht der eigentlichen Nachricht ein kurzer Vorspann voraus (Kennzeichnung als Selbsttest mit dem Hinweis, dass die Mail nicht aus dem Postfach stammt). Ohne ihn waere die zugestellte Zusammenfassung von einer echten nicht zu unterscheiden, und der Nutzer suchte im Postfach nach einer Mail, die es nie gab. Der Vorspann nennt die **Herkunft der Testmail** in zwei Fassungen — mitgelieferte Beispielmail oder die mit `--eml` uebergebene Datei —, aber nie den Dateipfad: Punkte im Pfad wuerde der Output-Sanitizer sichtbar entschaerfen. Wortlaut:

```
🧪 MailDigest self-test
The next message is built from the bundled example mail, not from your mailbox — there is no such mail to look for
```

bzw. `… built from the file you supplied, not from your mailbox — …`. Laesst sich der
Vorspann nicht zustellen, steht auf stderr `Note: the self-test marker could not be
delivered.`; der Selbsttest laeuft weiter.

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
1/5 Configuration loaded: <pfad>
2/5 Test mail read: <pfad|bundled example mail> (N bytes)
3/5 Pipeline running (sanitizer -> summarizer -> critic -> delivery) ...
4/5 Sanitizer: N characters of text, N attachments (N processed), N links removed, N control characters removed
  Summarizer: importance=<high|normal|low>, injection suspected=<yes|no>
  Critic: phishing risk=<none|low|high>, summary accurate=<yes|no>
5/5 Delivered (N parts). Check your messenger.
```

Bei genau einem Teil lautet die Klammer `(1 part)` — hier und ebenso in
`5/5 Message created (N parts) — dry run, not sent:`. Ebenso stehen in Zeile 4/5 die
Einzahlformen, wenn der Zähler 1 ist: `1 attachment`, `1 link removed`,
`1 control character removed`.

Weder der Mail-Text noch die Modellausgabe erscheinen dabei auf dem Terminal; nur die
fertige, sanitisierte Nachricht bei `--dry-run`. Mit `--dry-run` steht zwischen Schritt 2
und 3 zusätzlich die Zeile `    Dry run: nothing is sent to the messenger (--dry-run).`,
und Schritt 5 lautet `5/5 Message created (N parts) — dry run, not sent:`, gefolgt von der
Nachricht selbst.

Wurde die Nachricht erzeugt, aber nicht zugestellt (der Messenger nimmt sie nicht an, sie
wartet in der Warteschlange), lautet Schritt 5
`5/5 Not delivered (N parts) — queued for retry.`; die Erklärung dazu steht auf
stderr, der Exit-Code ist 1. Damit hat die Schrittfolge in allen drei Ausgängen —
zugestellt, nicht zugestellt, fail-closed — eine abschließende 5/5-Zeile.

Exit-Codes: 0, wenn die Nachricht erzeugt **und** zugestellt wurde (bzw. bei `--dry-run`
erzeugt und ausgegeben). 1, wenn die Pipeline fail-closed endete (dann lautet Zeile 4/5
`4/5 Sanitizer: failed.` und Schritt 5 `5/5 Fail-closed: stage <stufe>, reason <grund>.`,
und der Messenger bekommt die Metadaten-Notiz aus Abschnitt 6) oder wenn die Zustellung
nicht bestätigt wurde. 1 auch bei fehlender, unlesbarer oder unvollständiger Konfiguration
und bei nicht lesbarer `--eml`-Datei.

Endet die Pipeline mit `--dry-run` fail-closed, geht **nichts** an den Messenger. Schritt 5
lautet dann

```
5/5 Fail-closed: stage <stufe>, reason <grund>.
    Metadata notice created — dry run, not sent:
```

gefolgt von der Notiz selbst auf stdout; auf stderr steht `Self-test failed — only the
metadata notice was created (delivered: no — dry run).` Ohne `--dry-run` steht dort
`(delivered: yes)` nur, wenn die Notiz den Messenger tatsächlich erreicht hat; blieb sie in
der Warteschlange liegen, steht dort `(delivered: no)` (ADR-071).

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
Warteschlange leeren → Sammel-Digest prüfen → Befehlskanal bedienen), dann endet der
Prozess. Das ist die Cron-taugliche Form.

Zum Befehlskanal (`[messenger.telegram] accept_commands`, ADR-077/ADR-080): Im
Dauerbetrieb wird er **während** der Wartezeit alle 10 Sekunden abgefragt — ein `/digest`
wartet höchstens diese 10 Sekunden, nicht ein volles `poll_interval_seconds`. Bei `--once`
wird er **einmal am Ende** des Laufs bedient: `/status` wird beantwortet, `/digest` ist
dort wirkungslos (der Abruf lief gerade) und wird nur konsumiert, damit er sich nicht
staut. Im Cron-Betrieb kommt die Antwort also beim nächsten Lauf, verzögert um höchstens
das Cron-Intervall.

Sammel-Digest und Zustell-Warteschlange hängen **nicht** an der Erreichbarkeit des
Postfachs: Ist das Postfach ausgefallen, werden beide trotzdem abgearbeitet, bevor der
Lauf mit Exit-Code 1 endet (ADR-049 Nachtrag).

Ausgabe: strukturierte **JSON-Zeilen auf stdout** (ein Objekt je Ereignis, Felder `ts`,
`level`, `logger`, `event` und ereignisabhängige Zusatzfelder). Der Schwellwert kommt aus
`[general] log_level`. Mail-Inhalte, Betreffzeilen und Secrets erscheinen dort nie — nur
gekürzte Hashes, Absender-Domains, Statuswerte und Zähler. Bei `--once` kommt zusätzlich
eine Bilanzzeile auf **stderr**:

```
Run finished: N mails fetched, N processed, N duplicates, N errors, N messages delivered, N queued.
```

`N messages delivered` zählt **alle** in diesem Lauf zugestellten Nachrichten: direkt
zugestellte Einzelnachrichten, zugestellte Metadaten-Notizen, den Sammel-Digest und aus der
Warteschlange nachgelieferte Nachrichten (ADR-070). Nachrichten, die in der Warteschlange
verbleiben, zählen erst in dem Lauf, in dem sie durchgehen; bis dahin erscheinen sie unter
`N queued`.

Exit-Codes: 0 bei sauberem Ende, 1 bei unvollständiger Konfiguration, unbenutzbarer
State-Datenbank oder — nur bei `--once` — nicht erreichbarem Postfach
(`Error: Mailbox unreachable: …`). Ein fehlendes IMAP-Passwort (weder `[imap]
password` noch `MAILDIGEST_IMAP_PASSWORD`) zählt zum **ersten** Fall: Es wird vor dem
Verbindungsaufbau erkannt und als Konfigurationsfehler gemeldet
(`Error: Invalid configuration (<pfad>):` / `  - [imap] password: required value missing. …`),
nicht als Erreichbarkeitsproblem. Ein fehlgeschlagenes Verschieben ist **kein**
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
| `[messenger.telegram] accept_commands` | true/false | `true` | Ob MailDigest Befehle aus dem Chat annimmt (ADR-077, Vorgabe seit ADR-078 `true`). Eingeschaltet reagiert `run` auf `/digest` (Abrufzyklus, Wartezeit höchstens 10 s) und `/status` (Kurzbericht), **nur** aus `chat_id`; bei `run --once` wird der Kanal einmal am Ende des Laufs bedient und `/digest` ist dort wirkungslos (ADR-080). Erkannt werden die beiden Wörter tolerant: Groß-/Kleinschreibung egal, umgebende Leerzeichen und ein `@botname`-Suffix werden abgetrennt, Zusatztext hinter dem Befehl wird ignoriert. Jeder andere Text wird verworfen; kein Zeichen aus dem Chat — weder Zusatztext noch Chat-Titel noch Absendername — erreicht jemals ein Sprachmodell oder eine zugestellte Nachricht |
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
| `[limits] max_html_elements` | ≥ 1 | `50000` | Elemente der HTML-Konvertierung, als **Restbudget je Mail** über alle HTML-Teile; darüber wird der Teil nicht konvertiert (ADR-084). Höher stellen verlängert die Konvertierung linear — die Zusage „höchstens 10 s" gilt für die Vorgabe |
| `[limits] max_html_bytes` | ≥ 1024 | `1048576` | Bytebudget der HTML-Konvertierung je Mail (1 MB), geprüft **vor** dem Parsen; zusammen mit höchstens vier konvertierten `text/html`-Teilen deckelt es die Konvertierungszeit einer Mail auf rund zwei Sekunden (ADR-084-Nachtrag). Echte Newsletter liegen weit darunter; ein grösserer Wert verlängert die Konvertierung überproportional |

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
`📧`, `📎`, `🔍 Notes:`, `From:`) erzeugt ausschließlich das Programm; identische Anfänge in
Modelltext werden neutralisiert — **in beiden Sprachen**, also auch `Von:`, `Betreff:`,
`Hinweise:`, `Stufe:`, `Grund:`, `PHISHING-VERDACHT:` (ADR-062, ADR-083: die Ausgabe ist
englisch, ein Modelltext darf aber auch keine deutsch aussehende Kopfzeile fälschen können). Alle Domains und Dateinamen erscheinen mit gebrochenem
Punkt (`beispiel[.]de`, `rechnung[.]pdf`), weil Messenger nackte Domains automatisch
verlinken. Ist die Nachricht länger als das Limit des Zielsystems (Telegram 4096, Discord
2000, Signal 2000 Zeichen), wird sie an Zeilengrenzen auf mehrere Nachrichten aufgeteilt.
Muss dabei eine einzelne Zeile geschnitten werden, beginnt jede Fortsetzung mit `… `; das
Präfix macht sichtbar, dass die Zeile weiterläuft, und verhindert, dass ein Schnitt einen
der reservierten Zeilenanfänge an den Anfang einer Nachricht schiebt.

**Normale Zustellung**

```
⚠️ SUSPECTED PHISHING: <Gründe, kommasepariert, max. 5>   ← nur bei Phishing-Risiko high
📧 <Kopfzeile> [important]                                ← Tag nur bei Wichtigkeit high
From: <Anzeigename> (<domain>) · <TT.MM. HH:MM>           ← ohne Date-Header: `date unknown`
<Zusammenfassung>
— <datei>: <1–2 Sätze je verarbeitetem Anhang>
📎 Not processed: <datei (größe)>, … [and N more]
🔍 Notes: <Injection-Verdacht; verschlüsselte Mail; Message-ID-Kollision; Auth-Fehler;
           Punycode; gemischte Schriftsysteme; versteckter Text im HTML entfernt;
           HTML-Teil weicht vom Textteil ab; HTML-Teil zu komplex (nicht konvertiert);
           Reply-To-/Return-Path-Abweichung;
           Text gekürzt; Kritiker-Gründe bei Risiko low>
<Link-Fußnote (defanged), eine Adresse je Zeile>          ← nur bei [links] footnote = true
```

Die Hinweise hinter `🔍 Notes: ` sind kommasepariert und lauten wörtlich, in genau dieser
Reihenfolge: `the mail contained instructions aimed at the AI (ignored)`,
`encrypted (PGP/S-MIME) — content not readable by design`,
`Message-ID collides with an earlier mail`, `sender checks failed: <SPF=…, DKIM=…>`,
`punycode domain(s): <…>`, `mixed writing systems: <…>`,
`HTML part differs from the text part`, `HTML part too complex, not converted`,
`hidden text removed from the HTML`,
`reply address differs from the sender`, `return-path domain differs`, `text truncated`,
`critic: <Gründe>`. Fehlt jeder Hinweis, entfällt die Zeile.

Ersatztexte, wenn ein Feld leer ist: `📧 (no summary)` für die Kopfzeile, `(no summary)`
bzw. `(file)` in der Anhangszeile, `unknown` für einen fehlenden Absender, `(unnamed)` für
einen Anhang ohne Dateinamen. Ein Link ohne erkennbaren Host erscheint als
`[Link #n: unknown]`, eine `mailto:`-Adresse ohne Domain als `[Mail #n: unknown]`. Kürzt der
Sanitizer den Mail-Text, endet er auf `[truncated]`.

Zwei Hinweise erklären, warum eine Mail anders aussieht als erwartet, und haben deshalb
einen festen Wortlaut:

- `encrypted (PGP/S-MIME) — content not readable by design` — die Mail war Ende-zu-Ende
  verschlüsselt (`multipart/encrypted`, `application/pgp-encrypted`,
  `application/pkcs7-mime`). MailDigest entschlüsselt nicht; Kopfzeile, Absender und die
  Liste der nicht verarbeiteten Teile kommen trotzdem an (ADR-082). Signierte, aber
  unverschlüsselte Mail (`multipart/signed`) löst den Hinweis nicht aus.
- `Message-ID collides with an earlier mail` — die `Message-ID` dieser Mail war bereits von
  einer inhaltlich **anderen** Mail belegt. Sie wird trotzdem zugestellt (ADR-079); der
  Hinweis erklärt, warum ein Vorgang doppelt erscheinen kann.

Ohne Sprachmodell (`[llm] provider = "none"`) steht in der Anhangszeile statt der
Zusammenfassung ein beschrifteter Auszug des gelesenen Anhangstextes
(`— datei.txt: Excerpt: …`) — genau wie die Zusammenfassungszeile dort ein Auszug ist.

**Zahl- und Datumsformate** sind keine Literale und bleiben unabhängig von der
Ausgabesprache: Größen erscheinen als `34 KB` bzw. mit Dezimalkomma als `1,2 MB`, das Datum
der Absenderzeile als `TT.MM. HH:MM` (ADR-083).

Zeilen ohne Inhalt entfallen. Einzellimits: Kopfzeile 120, Zusammenfassung 3000,
Anhangs-Zusammenfassung 400, Kritiker-Grund 200, Anzeigename 80, Domain 100, Dateiname
80 Zeichen; höchstens 10 namentlich genannte Anhänge und 5 Banner-Gründe. Gekürzt wird
mit `…`. Bei Dateinamen wird in der **Mitte** gekürzt, damit die Endung erhalten bleibt
(`aaa…aaa.exe`) — bei einem nicht verarbeiteten Anhang ist sie die wichtigste Angabe.

**Metadaten-Notiz (fail-closed)** — immer genau diese fünf Zeilen:

```
⚠️ This mail could not be processed safely — no content delivered.
From: <domain>
Subject: <Betreff>
Stage: <sanitize|summarize|critic|compose|deliver> · Reason: <fehlerklasse>
Open your real mailbox to read it.
```

Fehlt die Domain, steht dort `unknown`; fehlt der Betreff, `(no subject)`. Lässt sich eine
Stufe oder Fehlerklasse nicht als Label darstellen, steht dort `unknown`.

Fehlerklassen: `sanitize_error`, `llm_timeout`, `llm_rate_limited`, `llm_invalid_response`,
`llm_transport_error`, `schema_invalid`, `summary_inaccurate`, `delivery_error`,
`state_error`, `delivery_failed` sowie `<stufe>_error` für Unbekanntes.

**Täglicher Sammel-Digest** — eine Nachricht ab `[general] low_digest_time`, gruppiert nach
Kategorie (größte Gruppe zuerst), je Mail eine Zeile; ab 60 Mails wird der Rest gezählt.
Eine leere Warteschlange erzeugt keine Nachricht.

```
🗂 12 low-priority mails: 8 newsletter, 3 benachrichtigung, 1 other

newsletter (8):
• Wochenrückblick KW 36 (news[.]example[.]org)
…
... and N more
```

Die Kategorienamen stammen aus der Modellausgabe und folgen deshalb `[general] language`;
eine leere Kategorie heißt `other`. Fehlt eine Kopfzeile, steht `(no subject)`, fehlt die
Domain, `unknown`.

## 7. Zusicherungen des Programms

Diese Punkte sind Teil des Vertrags und in REQUIREMENTS.md als F-SEC-* nachlesbar:

1. Im Mirror-Postfach wird **nichts gelöscht**. Geschrieben werden nur das Gelesen-Flag und
   — falls konfiguriert — das Verschieben in `move_processed_to`. Technisch sind das genau
   zwei IMAP-Kommandos: `UID STORE +FLAGS (\Seen)` und `UID MOVE`. Ein `EXPUNGE` wird nie
   gesendet — es würde auch fremde, von anderen Programmen als `\Deleted` markierte
   Nachrichten endgültig löschen (ADR-064).
2. Kein Sprachmodell sieht rohes HTML, rohe MIME-Teile oder Anhangs-Binärdaten.
3. Anhänge werden nie zugestellt. Inhaltlich verarbeitet werden nur `text/plain`-Dateien
   und PDFs (nach Prüfung der Magic-Bytes); alles andere erscheint nur in der Zeile
   `📎 Not processed:`.
4. Schlägt irgendeine Stufe fehl, kommt die Metadaten-Notiz — nie ungeprüfter Inhalt und
   nie stilles Verschwinden.
5. Secrets stehen ausschließlich in der Konfigurationsdatei (0600) oder in
   Umgebungsvariablen — nie in Prompts, Logs, Fehlermeldungen oder der State-Datenbank.
