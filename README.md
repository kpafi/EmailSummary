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
5. Geht irgendwo etwas schief, bekommst du eine Notiz („This mail could not be processed
   safely — no content delivered.") statt ungeprüften Inhalts — nichts verschwindet
   stillschweigend.

Der Weg einer Mail — jede Stufe sieht nur, was die vorige durchgelassen hat:

```
Mirror-Postfach
      │  IMAP (nur lesen, Gelesen-Flag, optional Verschieben)
      ▼
 [1] Ingest ─────► [2] Sanitizer ────► [3] Summarizer ───► [4] Kritiker ────► [5] Output- ───► [6] Messenger
     Code               Code                LLM                 LLM               Sanitizer         Telegram/
     Dedupe             MIME → Klartext     rechtelos           rechtelos         Code              Discord/
                        HTML/Anhänge weg    JSON-Ausgabe        Phishing-Check    letzte Prüfung    Signal
                             ▲                                                          │
                             └── ab hier existiert die Rohmail nicht mehr               └── keine Links,
                                                                                            kein Markup
```

Die beiden LLM-Stufen sind die einzigen, die fremden Text *interpretieren* — und die
einzigen, die nichts können: kein Werkzeug, kein Netz, keine Datei, kein Zugriff auf das
Postfach. Vor ihnen und hinter ihnen steht deterministischer Code.

Details: [docs/SECURITY.md](docs/SECURITY.md). Der vollständige CLI- und Config-Vertrag
steht in [docs/SPEC-CLI.md](docs/SPEC-CLI.md), der Betrieb als Dienst in
[docs/BETRIEB.md](docs/BETRIEB.md).

## Voraussetzungen

* Python ≥ 3.11 (läuft auf einem kleinen VPS oder einem Raspberry Pi)
* ein zweites, leeres IMAP-Postfach (das „Mirror-Postfach") — am besten bei einem anderen
  Anbieter als dein Hauptpostfach, mit eigenem, einmaligem Passwort oder App-Passwort
  (welcher Anbieter sich eignet: siehe [Das Spiegel-Postfach](#das-spiegel-postfach))
* **optional** ein Zugang zu einem Sprachmodell — ohne ihn läuft MailDigest ebenfalls,
  liefert dann aber Auszüge statt Zusammenfassungen (siehe
  [Mit oder ohne Sprachmodell](#mit-oder-ohne-sprachmodell))
* ein Telegram-Bot, ein Discord-Webhook oder ein laufendes `signal-cli`

## Das Spiegel-Postfach

MailDigest liest **nie** dein echtes Postfach. Es liest ein zweites, leeres Postfach, in
das du deine Mails weiterleitest. Dieses Postfach ist ein reines Ablagefach: Du liest es
nie selbst, es braucht keinen schönen Namen, und ein frisch angelegtes Konto ist besser
als ein bestehendes.

### Welcher Anbieter?

Entscheidend ist, ob sich IMAP mit einem Passwort nutzen lässt. Die folgende Übersicht
wurde am 2026-09-09 gegen die echten Server geprüft:

| Anbieter | IMAP-Host | Aufwand |
|---|---|---|
| **Posteo** | `posteo.de` | ~1 €/Monat, **am einfachsten** — IMAP ab Werk offen, Kontopasswort genügt |
| **mailbox.org** | `imap.mailbox.org` | ~1 €/Monat, ebenso unkompliziert |
| GMX | `imap.gmx.net` | kostenlos, aber IMAP muss erst in den Einstellungen freigeschaltet werden |
| WEB.DE | `imap.web.de` | wie GMX (gleicher Konzern) |
| Gmail | `imap.gmail.com` | App-Passwort nötig, dafür zwingend Zwei-Faktor-Anmeldung |
| iCloud | `imap.mail.me.com` | app-spezifisches Passwort, Zwei-Faktor-Anmeldung Pflicht |
| Yahoo | `imap.mail.yahoo.com` | App-Passwort nötig |
| Telekom/T-Online | `secureimap.t-online.de` | eigenes „Passwort für E-Mail-Programme" nötig |
| IONOS/1&1 | `imap.ionos.de` | Postfachpasswort genügt |
| **Outlook.com / Hotmail** | — | **funktioniert nicht.** Microsoft hat die Passwort-Anmeldung für IMAP abgeschaltet (der Server meldet `LOGINDISABLED`) und verlangt OAuth2, das MailDigest nicht kann |
| **Proton Mail** | — | **funktioniert nicht.** Kein offenes IMAP; die Proton-Bridge spricht unverschlüsseltes STARTTLS auf einem lokalen Port, MailDigest verbindet nur per IMAPS |

Ein Outlook- oder Proton-Konto ist trotzdem kein Ausschlusskriterium: Leg das
Spiegel-Postfach bei einem der anderen Anbieter an und lass Outlook bzw. Proton **dorthin
weiterleiten**. Dein Hauptkonto bleibt unangetastet.

`maildigest connect-mail` kennt diese Tabelle. Trägst du einen Host ein, nennt es dir die
passende Anleitung; trägst du einen Anbieter ein, der nicht funktionieren kann, sagt es das
sofort, statt dich in Anmeldefehler laufen zu lassen. Du kannst dort auch einfach die
Mailadresse eintippen — der Host wird daraus abgeleitet.

### Warum das Kontopasswort meist nicht reicht

Fast alle großen Anbieter lehnen das normale Kontopasswort für IMAP ab und verlangen ein
eigens erzeugtes **App-Passwort** — eine lange Zeichenkette, die nur für dieses eine
Programm gilt und sich einzeln widerrufen lässt. Das ist der mit Abstand häufigste Grund
für „Anmeldung fehlgeschlagen", obwohl Host, Benutzername und Passwort scheinbar stimmen.
Der Benutzername ist dabei fast immer die **vollständige Mailadresse**, nicht nur der Teil
davor.

## Mit oder ohne Sprachmodell

MailDigest läuft **ab Werk ohne jedes Sprachmodell**. Nach `maildigest init` ist
`[llm] provider = "none"` gesetzt, und du kannst sofort loslegen — ohne Konto, ohne
Kreditkarte, ohne API-Schlüssel.

**Was du in diesem Modus bekommst:** Betreff (ab 100 Zeichen mit `…` gekürzt), Absender,
einen ausdrücklich als solchen beschrifteten Auszug des Mailtextes, je Anhang, aus dem Text
gelesen werden konnte, einen ebenso beschrifteten Auszug (`— datei.txt: Excerpt: …`), die
Liste der geblockten Anhänge — und **alle Warnungen**. Der Phishing-Schutz hängt nämlich gar nicht am Sprachmodell: Fehlgeschlagene
SPF/DKIM-Prüfungen, abweichende Antwortadressen, Punycode-Domains, versteckter Text im
HTML und entfernte Links berechnet das Programm selbst, in Code. Was ohne Modell fehlt,
ist der zusammenfassende Text und die Einschätzung „wichtig oder nicht" — nicht der Schutz.

**Was du mit Modell dazubekommst:** echte Zusammenfassungen statt Auszügen, eine
Wichtigkeits-Einstufung (und damit einen sinnvollen Sammel-Digest für Unwichtiges), sowie
den Kritiker, der die Zusammenfassung gegen den Mailtext prüft.

`maildigest connect-llm` stellt die Betriebsarten als Auswahlliste vor:

| Option | Kosten | Aufwand |
|---|---|---|
| **kein Sprachmodell** (Vorgabe) | keine | keiner |
| **Groq**, **OpenRouter**, **Cerebras** | Gratis-Kontingent, keine Kreditkarte | Anmeldung + Schlüssel einfügen |
| **lokales Modell** (Ollama, LM Studio, vLLM) | keine | Ollama installieren, Modell laden (einige GB) — dafür verlässt kein Mailinhalt deinen Rechner |
| **Anthropic** | kostenpflichtig | Anmeldung + Guthaben |

**Antwortbudget (`max_tokens`):** Ab Werk gibt es **kein Limit** — es gilt die Obergrenze
des Modells. `connect-llm` fragt am Ende, ob du eines setzen willst, und nennt sinnvolle
Werte. Der Grund für die Vorgabe: Viele aktuelle Modelle sind „Reasoning-Modelle"
(DeepSeek, Qwen-Thinking, OpenAI o-Serie, Gemini-Thinking) und ziehen ihre Denk-Tokens vom
Antwortbudget ab. Mit einem kleinen Limit wird das JSON abgeschnitten, und statt einer
Zusammenfassung kommt bei jeder Mail die Fail-closed-Notiz „could not be processed safely".
Ein Limit lohnt sich nur, wenn du die Kosten je Aufruf bewusst deckeln willst — dann sind
4096 für klassische Modelle ein sicherer Wert.

### Warum liegt kein Schlüssel bei?

Weil dieses Programm quelloffen ist. Ein mitgelieferter Zugang stünde für jeden lesbar im
Quelltext, wäre binnen Tagen abgegriffen und gesperrt — und die Rechnung ginge an jemand
anderen als dich. Deshalb der ehrliche Weg: Ohne Anmeldung geht es ohne Modell, und wer
Zusammenfassungen will, verbindet in zwei Minuten ein eigenes (auch gratis).

## Installation

**Empfohlen — mit `pipx`.** So landet der Befehl `maildigest` im Suchpfad und ist aus
jedem Verzeichnis heraus aufrufbar:

```bash
pipx install .
```

Fehlt `pipx`, installiert `sudo apt install pipx` es (Debian/Ubuntu/Kali); unter macOS
`brew install pipx`. Danach einmalig `pipx ensurepath` und ein neues Terminal öffnen.

**Achtung bei eigenen Änderungen am Quelltext:** `pipx install .` erstellt eine *Kopie*
des Pakets. Änderungen am Quelltext wirken sich auf den installierten Befehl dann **nicht**
aus — man arbeitet unbemerkt mit einem alten Stand weiter. Wer am Projekt selbst arbeitet
oder Änderungen ausprobiert, installiert deshalb verknüpft:

```bash
pipx install --force --editable .
```

Dann entspricht `maildigest` immer dem aktuellen Arbeitsstand. Bei einer bereits
vorhandenen Kopie-Installation genügt derselbe Befehl zum Umstellen.

**Alternative — im virtuellen Umfeld.** Praktisch zum Entwickeln, aber der Befehl liegt
dann nur in `.venv/bin` und steht außerhalb nicht zur Verfügung:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Ohne aktiviertes venv ist der Befehl dann `.venv/bin/maildigest`.

**`maildigest: command not found`?** Dann wurde ins virtuelle Umfeld installiert, nicht in
den Suchpfad. Entweder `pipx install .` nachholen oder — unabhängig von der Installations­art
— das Paket direkt als Modul aufrufen, das funktioniert immer:

```bash
python3 -m maildigest --help
```

### Hilfe und Handbuch

Jedes Kommando erklärt sich selbst: `maildigest --help` zeigt den typischen Ablauf,
`maildigest <kommando> --help` Beschreibung, Optionen und Beispiele. Die vollständige
Handbuchseite gibt es ohne Installation:

```bash
maildigest --man | man -l -
```

Wer `man maildigest` tippen will, kopiert die mitgelieferte Seite in den eigenen Manpfad:

```bash
mkdir -p ~/.local/share/man/man1 && cp man/maildigest.1 ~/.local/share/man/man1/
```

Die Seite wird aus derselben Quelle erzeugt wie die Hilfe (`maildigest --man`), ein Test
hält sie mit dem Programm synchron. Der Vertrag für jede Frage und jede Ausgabezeile bleibt
[docs/SPEC-CLI.md](docs/SPEC-CLI.md).

## Quickstart

```bash
maildigest init                # Konfiguration anlegen (Datei bekommt Rechte 0600)
maildigest connect-mail        # Mirror-Postfach: Zugang testen, Ordner wählen
maildigest connect-llm         # OPTIONAL: Sprachmodell (auch gratis) verbinden
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
From: Hausverwaltung Meier (hausverwaltung-meier[.]example) · 12.03. 09:14
Die Hausverwaltung kündigt eine Ablesung der Heizkörper an. Zutritt zwischen 9 und 13 Uhr nötig.
```

Der Rahmen der Nachricht (`From:`, `📎 Not processed:`, `🔍 Notes:` …) ist immer englisch;
die Zusammenfassung selbst schreibt das Sprachmodell in der Sprache aus
`[general] language` (ADR-083).

Die Zeile `🔍 Notes: …` kommt nur dazu, wenn es etwas zu melden gibt — eine
fehlgeschlagene Absender-Prüfung, eine Punycode-Domain, versteckter Text im HTML,
gekürzter Text. Entfernte Links sind kein Hinweis wert: Sie stehen als
`[Link #1: beispiel[.]de]` an ihrer Stelle im Text.

Unwichtige Mails kommen nicht einzeln, sondern einmal am Tag gesammelt:

```
🗂 12 low-priority mails: 8 newsletter, 3 benachrichtigung, 1 other
```

Und wenn etwas faul ist:

```
⚠️ SUSPECTED PHISHING: Absenderdomain passt nicht zum angeblichen Absender, Antwortadresse abweichend
📧 Dringende Zahlungsaufforderung [important]
From: Chef (mail-sicherheit[.]example) · 12.03. 03:41
Angebliche Zahlungsaufforderung des Chefs, Überweisung noch heute.
🔍 Notes: reply address differs from the sender
```

## Vom Handy aus anstoßen (optional)

Während `maildigest run` läuft, reagiert es auf genau zwei Wörter aus **deinem** Chat:

| Befehl | Wirkung |
|---|---|
| `/digest` | Ruft ab, statt das Poll-Intervall abzuwarten — spätestens zehn Sekunden später |
| `/status` | Kurzbericht: gelesener Ordner, wartende Zustellungen, gesammelte Mails |

Groß- und Kleinschreibung sind egal, Leerzeichen davor und dahinter auch; ein angehängtes
`@deinbotname` (das Telegram in Gruppen anfügt) wird abgetrennt, und alles, was hinter dem
Befehl steht, wird ignoriert — gelesen wird nur das erste Wort.

Läufst du per Cron mit `maildigest run --once`, gibt es keine Wartezeit, die sich
abkürzen ließe: Die Befehle werden dort am Ende des **nächsten** Laufs bedient. `/status`
antwortet dann, `/digest` ist wirkungslos — dieser Lauf hat gerade abgerufen.

**Was dieser Kanal ausdrücklich nicht kann:** Es gibt keinen Dialog. Schreibst du etwas
anderes — auch „fasse mir die Mail von gestern zusammen" —, wird es verworfen, ohne es zu
lesen, zu beantworten oder an das Sprachmodell zu geben. Das ist Absicht: Beliebiger Text
in ein Sprachmodell, dessen Antwort dann Aktionen steuert, ist genau die Kopplung, die
dieses Werkzeug vermeidet.

Das ist ab Werk an (ADR-078). Wer in diesen Chat schreiben kann, kann damit Abrufe
auslösen und so Kosten beim Modellanbieter verursachen — bei einem privaten Bot-Chat bist
das nur du. Ist `chat_id` dagegen eine **Gruppe**, in der nicht jeder das können soll,
schalte es ab:

```toml
[messenger.telegram]
accept_commands = false
```

**Die Alternative ohne jeden Rückkanal:** Lass MailDigest per systemd-Timer oder Cron
laufen (siehe [docs/BETRIEB.md](docs/BETRIEB.md)). Dann hast du die Zusammenfassungen
ohnehin auf dem Handy, ohne dass von außen irgendetwas hereinreicht.

## Anpassen

In der `config.toml`:

```toml
[general]
deliver_min_importance = "normal"   # low = alles einzeln, high = nur Dringendes
low_digest_time = "18:00"           # wann der Sammel-Digest kommt

[summarizer]
instructions = "Rechnungen und Termine sind immer wichtig. Werbung ist nie wichtig."

[llm]
# max_tokens = 4096   # fehlt = kein Limit; nur setzen, wenn du die Kosten je Aufruf deckeln willst
```

Die Custom-Instructions steuern Stil, Fokus und Wichtigkeit. Sie können die
Sicherheitsregeln nicht abschalten — der Kritiker sieht sie gar nicht erst.

Du musst dafür nicht in der Datei suchen. Das Kommando `maildigest instructions` zeigt den
aktuellen Text, und so änderst du ihn:

```bash
maildigest instructions --add "Alles von meiner Uni ist wichtig."   # Zeile anhängen
maildigest instructions --edit                                     # im Editor bearbeiten
maildigest instructions --set "Nur Rechnungen und Termine zählen."  # komplett ersetzen
maildigest instructions --clear                                    # löschen
```

Der Text gilt ab dem nächsten Lauf; bis zu 2000 Zeichen, mehrere Zeilen sind erlaubt.

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
erscheint als Zeile „📎 Not processed: rechnung[.]docx (34 KB)" — du weißt also, dass
es da ist, und entscheidest selbst.

**Was ist mit Phishing als Bild?**
Das ist die bekannteste Lücke: MailDigest liest keine Bildinhalte (kein OCR). Ein Angreifer
kann seinen Text als Screenshot verschicken; die Zusammenfassung sagt dann sinngemäß „Mail
ohne Text mit einem Bildanhang". Das ist auffällig, aber es ist kein Schutz — die
Bewertung musst du in dem Fall selbst treffen. Auch verschlüsselte Mails (PGP/S-MIME)
werden nicht entschlüsselt und deshalb nicht zusammengefasst — die Nachricht sagt das
dann ausdrücklich („encrypted (PGP/S-MIME) — content not readable by design").

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

Das Folgende ist keine Aufzählung von Kleinigkeiten, sondern die Liste der Stellen, an
denen du dich auf MailDigest **nicht** verlassen solltest.

**Noch nie gegen echte Gegenstellen gelaufen.** Version 0.1.0 ist vollständig gegen
Attrappen getestet: kein echtes IMAP-Postfach, keine echte LLM-API, kein echter Messenger.
Die Tests sind gründlich (1500+ Tests, ein Angriffskorpus, zwei dokumentierte
Prüfdurchläufe), aber
sie prüfen das Programm gegen ein nachgebautes Gegenüber. Ob ein realer IMAP-Server sich
so verhält wie unser Mock, ob ein reales Modell das JSON-Format hält, ob Telegram die
Nachricht so annimmt — das ist unbelegt. Rechne beim ersten Lauf mit Überraschungen und
fang mit `maildigest test --dry-run` an.

**Der zweite Blackbox-Durchlauf fehlt.** Der erste (docs/TESTING.md §6) fand zwei
schwerwiegende Fehler; beide sind behoben und mit Regressionstests belegt. Unser eigenes
Testprotokoll verlangt danach eine zweite, unabhängige Runde. Sie hat nicht stattgefunden.
Eine weitere Prüfrunde im September 2026 (docs/TESTING.md §7) hat 38 Befunde geliefert — alle
behoben —, ersetzt sie aber nicht: Die Nachprüfung lief dort mit Code-Zugriff.

**Bild-Phishing bleibt offen.** Kein OCR, keine Bildanalyse. Wer seinen Text als Screenshot
verschickt, bekommt eine Zusammenfassung wie „Mail ohne Text mit einem Bildanhang" — das
ist auffällig, aber es ist keine Prüfung.

**Verschlüsselte Mail wird nicht gelesen.** PGP und S/MIME werden nicht entschlüsselt. Du
bekommst trotzdem eine reguläre Nachricht — Kopfzeile, Absender, die Liste der nicht
verarbeiteten Teile (`📎 Not processed: …`) und die Hinweiszeile
`🔍 Notes: encrypted (PGP/S-MIME) — content not readable by design`. Zum Lesen musst du
ins echte Postfach. Signierte, aber unverschlüsselte Mail ist davon nicht betroffen.

**Signal nur als Notiz an dich selbst.** Der Signal-Adapter schreibt in „Notiz an mich" und
setzt ein laufendes `signal-cli --daemon` voraus. Andere Empfänger sind nicht vorgesehen.

**Neue Warnheuristiken sind ungeeicht.** Die Erkennung von KI-Anweisungen im Mail-Text, die
Schwelle für „HTML-Teil weicht vom Textteil ab" und die Regel, ab wann mehrere
Fälschungssignale zu einer Phishing-Warnung werden, sind an Testmails eingestellt, nicht an
deinem Posteingang. Zu viele Warnungen sind wahrscheinlicher als zu wenige — und eine
Warnung, die immer kommt, ist keine.

**Verschieben kann erst im Betrieb scheitern.** `connect-mail` prüft nicht vorab, ob dein
Server die MOVE-Erweiterung beherrscht und ob der Zielordner existiert. Beides fällt erst
beim ersten Lauf auf; verloren geht dabei nichts — die Mail bleibt als gelesen liegen.

**Ein Postfach, ein Prozess, fast keine Rückrichtung.** Kein Multi-Postfach-Betrieb, kein
Zugriff auf dein echtes Postfach, kein Dialog mit dem Bot — die einzige Ausnahme ist die
feste Befehlsliste `/digest` und `/status` aus deinem Chat, siehe
[Vom Handy aus anstoßen](#vom-handy-aus-anstoßen-optional).

Zwei bewusste Eigenheiten, die wie Fehler aussehen können:

* Zugestellter Text trägt **keine Formatierung**: Aufzählungen erscheinen als `•`,
  Überschriften und Kursivschrift verschwinden. Formatierung im Namen eines Absenders ist
  ein Vertrauenssignal, das MailDigest niemandem überlässt.
* Bei Mails mit Text- **und** HTML-Fassung fasst MailDigest die Textfassung zusammen — dein
  Mailprogramm zeigt dir die HTML-Fassung. Weichen beide deutlich voneinander ab, steht das
  als Hinweis in der Nachricht; inhaltlich vergleichen kann MailDigest sie nicht.

## Lizenz und Status

Version 0.1.0 (siehe [CHANGELOG.md](CHANGELOG.md)) — ein erstes vollständiges Release, noch
ohne Praxiserprobung. Die verbindlichen Anforderungen stehen in
[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), das Sicherheitsmodell samt Invarianten-Review
in [docs/SECURITY.md](docs/SECURITY.md).
