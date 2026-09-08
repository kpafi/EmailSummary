# MailDigest

MailDigest liest ein **Spiegel-Postfach**, in das du deine Mails weiterleitest, fasst jede
Mail mit einem Sprachmodell zusammen, lässt eine zweite, unabhängige Instanz auf Phishing
prüfen und schickt dir das Ergebnis als **reinen Text** an Telegram, Discord oder Signal.
Du siehst, was in deinem Postfach los ist, ohne dein Postfach zu öffnen — und ohne dass
irgendetwas Anklickbares bei dir ankommt.

```
Echtes Postfach ──Weiterleitung──► Mirror-Postfach ──► MailDigest ──► Messenger
```

## Das Sicherheitsmodell in fünf Sätzen

1. MailDigest bekommt nie Zugriff auf dein echtes Postfach, sondern nur auf ein
   dediziertes Spiegelpostfach — und löscht dort nichts.
2. Bevor ein Sprachmodell etwas zu sehen bekommt, zerlegt deterministischer Code die Mail
   in reinen Text: kein HTML, keine MIME-Teile, keine Anhangs-Binärdaten.
3. Die Sprachmodelle haben keine Werkzeuge, keinen Netz- und keinen Dateizugriff — das
   Einzige, was sie bewirken können, ist Text in einem geprüften JSON-Feld.
4. Was bei dir ankommt, hat als Letztes wieder Code geprüft: keine klickbaren Links, keine
   Anhänge, kein Markup — Domains erscheinen nur entschärft als `beispiel[.]de`.
5. Geht irgendwo etwas schief, bekommst du eine Notiz „konnte nicht sicher verarbeitet
   werden" statt ungeprüften Inhalts — nichts verschwindet stillschweigend.

Details: [docs/SECURITY.md](docs/SECURITY.md). Der vollständige CLI- und Config-Vertrag
steht in [docs/SPEC-CLI.md](docs/SPEC-CLI.md), der Betrieb als Dienst in
[docs/BETRIEB.md](docs/BETRIEB.md).

## Voraussetzungen

* Python ≥ 3.11 (läuft auf einem kleinen VPS oder einem Raspberry Pi)
* ein zweites, leeres IMAP-Postfach (das „Mirror-Postfach") — am besten bei einem anderen
  Anbieter als dein Hauptpostfach, mit eigenem, einmaligem Passwort oder App-Passwort
* ein Zugang zu einem Sprachmodell: Anthropic-API oder ein OpenAI-kompatibler Endpunkt
  (damit auch lokale Modelle über Ollama, vLLM oder LM Studio)
* ein Telegram-Bot, ein Discord-Webhook oder ein laufendes `signal-cli`

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

## Quickstart

```bash
maildigest init                # Konfiguration anlegen (Datei bekommt Rechte 0600)
maildigest connect-mail        # Mirror-Postfach: Zugang testen, Ordner wählen
maildigest connect-llm         # Provider + Modell, Testaufruf
maildigest connect-messenger   # Telegram/Discord/Signal, Testnachricht
maildigest test                # Ende-zu-Ende-Selbsttest mit einer Beispielmail
maildigest run                 # Dauerbetrieb (Strg-C beendet sauber)
```

Jedes Kommando fragt, was es braucht, und erklärt den nächsten Schritt. `connect-mail`
druckt am Ende die Anleitung, wie du die Weiterleitung bei Gmail, posteo, mailbox.org oder
einem beliebigen anderen Anbieter einrichtest.

Secrets gibst du entweder in der Abfrage ein (dann landen sie in der `config.toml` mit
Dateirechten 0600) oder über Umgebungsvariablen — dann steht in der Datei nichts:

```bash
export MAILDIGEST_IMAP_PASSWORD=…
export MAILDIGEST_LLM_API_KEY=…
export MAILDIGEST_TELEGRAM_TOKEN=…
```

Für Cron statt Dauerbetrieb: `maildigest run --once`.

## So sieht eine Nachricht aus

```
📧 Heizungsablesung am Donnerstag
Von: Hausverwaltung Meier (hausverwaltung-meier[.]example) · 12.03. 09:14
Die Hausverwaltung kündigt eine Ablesung der Heizkörper an. Zutritt zwischen 9 und 13 Uhr nötig.
🔍 Hinweise: 1 Link entfernt
```

Unwichtige Mails kommen nicht einzeln, sondern einmal am Tag gesammelt:

```
🗂 12 unwichtige Mails: 8 newsletter, 3 benachrichtigung, 1 sonstiges
```

Und wenn etwas faul ist:

```
⚠️ PHISHING-VERDACHT: Absenderdomain passt nicht zum angeblichen Absender, Antwortadresse abweichend
📧 Dringende Zahlungsaufforderung
Von: Chef (mail-sicherheit[.]example) · 12.03. 03:41
```

## Anpassen

In der `config.toml`:

```toml
[general]
deliver_min_importance = "normal"   # low = alles einzeln, high = nur Dringendes
low_digest_time = "18:00"           # wann der Sammel-Digest kommt

[summarizer]
instructions = "Rechnungen und Termine sind immer wichtig. Werbung ist nie wichtig."
```

Die Custom-Instructions steuern Stil, Fokus und Wichtigkeit. Sie können die
Sicherheitsregeln nicht abschalten — der Kritiker sieht sie gar nicht erst.

## FAQ

**Warum bekomme ich keine Links?**
Weil ein Link das Angriffsmittel ist. Phishing funktioniert dadurch, dass du klickst — und
in einem Messenger klickt es sich besonders leicht. MailDigest ersetzt jeden Link durch
`[Link #1: beispiel[.]de]` und bricht alle Punkte, damit dein Messenger nichts davon
verlinkt. Wer wirklich hin will, öffnet bewusst sein Postfach. Willst du die vollständigen
(entschärften) Adressen mitgeliefert bekommen, setze `[links] footnote = true` — sie werden
dann als Liste unten an die Nachricht gehängt, ebenfalls mit gebrochenen Punkten.

**Warum wird meine `.docx`-Rechnung nicht zusammengefasst?**
Weil MailDigest nur öffnet, was es sicher öffnen kann. Inhaltlich verarbeitet werden
ausschließlich reine Textdateien und PDFs — und auch die nur, wenn die ersten Bytes der
Datei zum angegebenen Dateityp passen und die Extraktion in einem abgeschotteten
Unterprozess durchläuft. Office-Dateien können Makros ausführen, Archive schmuggeln
Inhalte an Filtern vorbei, `.html`-Anhänge sind ein eigener Angriffsweg. Alles davon
erscheint als Zeile „📎 Nicht verarbeitet: rechnung[.]docx (34 KB)" — du weißt also, dass
es da ist, und entscheidest selbst.

**Was ist mit Phishing als Bild?**
Das ist die bekannteste Lücke: MailDigest liest keine Bildinhalte (kein OCR). Ein Angreifer
kann seinen Text als Screenshot verschicken; die Zusammenfassung sagt dann sinngemäß „Mail
ohne Text mit einem Bildanhang". Das ist auffällig, aber es ist kein Schutz — die
Bewertung musst du in dem Fall selbst treffen. Auch verschlüsselte Mails (PGP/S-MIME)
werden nicht entschlüsselt und deshalb nicht zusammengefasst.

**Kann die KI durch eine Mail übernommen werden?**
Übernehmen kann sie nichts, weil sie nichts darf: kein Werkzeug, kein Netz, keine Datei.
Eine erfolgreiche Prompt-Injection kann höchstens eine falsche Zusammenfassung erzeugen —
dagegen prüft eine zweite Instanz mit eigenem Prompt und mit im Code berechneten Fakten
(Absender-Domains, Authentifizierungs-Ergebnisse, verdächtige Anhänge), und der Verdacht
erscheint als Hinweiszeile in deiner Nachricht.

**Wo liegen meine Daten?**
Der Mail-Text existiert nur im Arbeitsspeicher, solange die Mail verarbeitet wird. In der
SQLite-Datei stehen Status und Metadaten, dazu zwei Ausnahmen mit bereits geprüftem,
entschärftem Text: die Zeilen für den Sammel-Digest und eine noch nicht bestätigte
Zustellung; beide werden nach dem Versand gelöscht. Die Logs enthalten keine Mail-Inhalte
— außer du stellst `log_level = "DEBUG"` ein, dann können Tracebacks Inhalte enthalten.
An das Sprachmodell geht der sanitisierte Mail-Text; wähle den Anbieter entsprechend oder
nimm ein lokales Modell.

**Warum kommt eine Mail doppelt?**
Weil MailDigest im Zweifel lieber doppelt zustellt als etwas zu verlieren: Der Stand
„geprüft" wird gespeichert, bevor gesendet wird. Stürzt der Prozess genau dazwischen ab,
kann dieselbe Nachricht ein zweites Mal kommen. Bei langen Zusammenfassungen, die auf
mehrere Nachrichten aufgeteilt werden, betrifft das nur den einen Teil, dessen Bestätigung
ausblieb — die Teile davor werden nicht erneut geschickt.

**Kommt der Warnhinweis auch, wenn das Sprachmodell schludert?**
Ja. Der Hinweis „Mail enthielt Anweisungen an die KI (ignoriert)" hängt nicht mehr allein am
Urteil des Modells: Nachgebaute Programm-Marker, auffällig viele unsichtbare Zeichen und
wörtliche Anweisungen an ein Sprachmodell erkennt MailDigest selbst, im Code, bevor irgendein
Modell befragt wird. Dasselbe gilt für die Phishing-Warnung — treffen mehrere unabhängige
Fälschungssignale zusammen, setzt das Programm die Warnung auch gegen ein schweigendes
Modell.

**Was passiert im Mirror-Postfach?**
Gelesen wird, was ungelesen ist; danach wird die Mail als gelesen markiert und — wenn du
`move_processed_to` setzt — in den angegebenen Ordner verschoben (dafür muss dein Server die
MOVE-Erweiterung beherrschen; alle gängigen tun das — sonst bleibt die Mail als gelesen
liegen und du bekommst einen Hinweis im Log). Gelöscht wird nie; einen Codepfad dafür gibt
es nicht — auch kein `EXPUNGE`, das die Löschmarkierungen anderer Programme ausführen würde.

## Grenzen dieser Version

Kein OCR und keine Bildanalyse, keine Entschlüsselung von PGP/S-MIME, keine Antworten aus
dem Messenger heraus, ein Postfach pro Installation, kein Zugriff auf das echte Postfach.

Zugestellter Text trägt bewusst keine Formatierung: Aufzählungen erscheinen als `•`,
Überschriften und Kursivschrift verschwinden. Das ist Absicht — Formatierung im Namen eines
Absenders ist ein Vertrauenssignal, das MailDigest niemandem überlässt.

Bei Mails, die eine Text- und eine HTML-Fassung enthalten, fasst MailDigest die Textfassung
zusammen — dein Mailprogramm zeigt dir dagegen die HTML-Fassung. Weichen beide deutlich
voneinander ab, steht das als Hinweis in der Nachricht; inhaltlich vergleichen kann
MailDigest sie nicht.

## Lizenz und Status

Version 0.1, in Entwicklung. Die verbindlichen Anforderungen stehen in
[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).
