# MailDigest — Betrieb

> Wie MailDigest dauerhaft läuft: als systemd-Dienst (empfohlen) oder per Cron.
> Stand WP9: Die hier benutzten Kommandos (`maildigest run`, `maildigest run --once`,
> `--config`) existieren und sind in [SPEC-CLI.md](SPEC-CLI.md) verbindlich beschrieben —
> bei Abweichungen zwischen den beiden Dokumenten gilt SPEC-CLI.md. Die Umgebungsvariablen
> für Secrets sind dort in §5 gelistet.

## 1. Grundannahmen

- **Ein Prozess, eine Instanz, ein Postfach.** Zwei gleichzeitig laufende Instanzen auf
  derselben State-Datenbank sind nicht vorgesehen (SQLite-Sperren, doppelte Zustellung).
  Der systemd-Dienst ist deshalb kein Template-Unit.
- Dateien im Arbeitsverzeichnis des Dienstes:
  - `config.toml` — Konfiguration **inklusive Secrets**, Modus `0600`.
  - `state.db` (+ `-wal`/`-shm`) — Status, Sammel-Digest- und Zustell-Warteschlange,
    Modus `0600`. Pfad änderbar über `[general] state_db` (ADR-045).
- Secrets möglichst **nicht** in die Datei, sondern als Umgebungsvariablen:
  `MAILDIGEST_IMAP_PASSWORD`, `MAILDIGEST_LLM_API_KEY`, `MAILDIGEST_TELEGRAM_TOKEN`.

## 2. systemd-Unit (empfohlen)

`/etc/systemd/system/maildigest.service` — der Dienst läuft unter einem eigenen,
unprivilegierten Benutzer:

```ini
[Unit]
Description=MailDigest — E-Mail-Zusammenfassungen an den Messenger
Documentation=https://example.invalid/maildigest
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=maildigest
Group=maildigest
WorkingDirectory=/var/lib/maildigest
Environment=PYTHONUNBUFFERED=1
# Secrets aus einer Datei mit Modus 0600, die nur root und der Dienst lesen dürfen:
EnvironmentFile=/etc/maildigest/secrets.env
ExecStart=/opt/maildigest/.venv/bin/maildigest run --config /var/lib/maildigest/config.toml

# Sauberer Shutdown: SIGTERM beendet den laufenden Zyklus und stoppt danach (ADR-051).
KillSignal=SIGTERM
TimeoutStopSec=120
Restart=on-failure
RestartSec=30s

# Härtung (der Dienst braucht nur sein Datenverzeichnis und ausgehendes Netz):
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/maildigest
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
```

`AF_UNIX` wird nur für den optionalen Signal-Adapter (`signal-cli`-Socket) gebraucht und
kann sonst entfallen.

Inbetriebnahme:

```bash
sudo useradd --system --home /var/lib/maildigest --shell /usr/sbin/nologin maildigest
sudo install -d -o maildigest -g maildigest -m 0700 /var/lib/maildigest
sudo install -o maildigest -g maildigest -m 0600 config.toml /var/lib/maildigest/config.toml
sudo systemctl daemon-reload
sudo systemctl enable --now maildigest.service
```

Logs lesen (JSON-Zeilen, ADR-046):

```bash
journalctl -u maildigest -f -o cat | jq .
journalctl -u maildigest -o cat | jq 'select(.level=="ERROR")'
```

## 3. Cron-Alternative

Wer keinen Dauerprozess will (kleiner Server, Laptop), ruft den Einmal-Lauf per Cron auf.
`run --once` arbeitet die Zustell-Warteschlange ab, holt neue Mails, arbeitet die
Warteschlange erneut ab und prüft den Sammel-Digest — und beendet sich.

```cron
# Alle 10 Minuten neue Mails verarbeiten; Ausgabe geht ins Syslog.
*/10 * * * * maildigest /opt/maildigest/.venv/bin/maildigest run --once \
    --config /var/lib/maildigest/config.toml 2>&1 | /usr/bin/logger -t maildigest
```

Hinweise zum Cron-Betrieb:

- Das Intervall sollte **kleiner** als eine Stunde sein: Die Zustell-Warteschlange gibt eine
  nicht zustellbare Nachricht nach spätestens einer Stunde auf (ADR-048); zwischen den
  Läufen finden keine Wiederholversuche statt.
- `[general] low_digest_time` wird beim ersten Lauf **ab** dieser Uhrzeit bedient — ein
  10-Minuten-Raster trifft sie immer. Läuft die Maschine zu dieser Zeit gar nicht, wird der
  Digest beim nächsten Lauf desselben Tages nachgeholt, danach nicht mehr.
- Überlappende Läufe vermeiden (`flock`), damit nicht zwei Prozesse auf dieselbe
  State-Datenbank schreiben:
  `*/10 * * * * maildigest /usr/bin/flock -n /var/lib/maildigest/run.lock /opt/…/maildigest run --once …`
- `[imap] poll_interval_seconds` ist im Cron-Betrieb wirkungslos (kein Loop).
- Der Befehlskanal (`[messenger.telegram] accept_commands`, ab Werk an) wird auch im
  Cron-Betrieb bedient, aber nur **einmal am Ende** jedes Laufs: `/status` wird
  beantwortet, `/digest` ist dort wirkungslos und wird lediglich konsumiert (Logzeile
  `command_ignored_once`). Die Antwort kommt also mit bis zu einem Cron-Intervall
  Verzögerung. Wer sofortige Reaktion will, nimmt den Dauerbetrieb — dort liegt die
  Latenz bei höchstens zehn Sekunden (ADR-080).
- Ein Postfach-Ausfall bricht den Lauf mit Exit-Code 1 ab, aber erst, nachdem die
  Zustell-Warteschlange und der fällige Sammel-Digest abgearbeitet sind: Beide brauchen
  kein IMAP (ADR-049 Nachtrag).
- **`run --once` läuft nicht unter den Signal-Handlern.** Den sauberen Shutdown aus ADR-051
  installiert nur der Dauerbetrieb. Wird ein Einmal-Lauf hart abgebrochen — Ctrl+C, ein
  Cron-Timeout, ein `systemd`-Kill —, kann eine gerade verarbeitete Mail im Zustand
  `sanitized` liegen bleiben. Sie ist nicht verloren und in der Datenbank abfragbar
  (`SELECT status, COUNT(*) FROM seen_mails GROUP BY status;`), wird beim nächsten Lauf aber
  als Duplikat erkannt und **nicht** erneut verarbeitet (ADR-019 hält diese Folge als
  akzeptiert fest). Wer das nicht will, gibt dem Cron-Eintrag ein großzügiges Timeout oder
  nimmt den Dauerbetrieb.

## 4. Wartung

| Aufgabe | Vorgehen |
|---------|----------|
| Konfiguration ändern | Datei bearbeiten, `systemctl restart maildigest` |
| Zustand ansehen | `sqlite3 state.db "SELECT status, COUNT(*) FROM seen_mails GROUP BY status;"` |
| Wartende Zustellungen | `sqlite3 state.db "SELECT kind, attempts, next_attempt_at FROM outbox;"` |
| Sammel-Digest-Rückstand | `sqlite3 state.db "SELECT COUNT(*) FROM low_digest_queue;"` |
| Schema-Migration | passiert von allein, siehe unten |
| Backup | `config.toml` sichern; `state.db` ist reproduzierbarer Betriebszustand — geht sie verloren, werden ungelesene Mails im Mirror-Postfach erneut verarbeitet (nie doppelt zugestellt, solange sie als gelesen markiert sind) |
| Fehlersuche | vorübergehend `[general] log_level = "DEBUG"` |

**Schema-Version der Zustandsdatenbank.** Seit ADR-079 ist sie **3**. Eine Datei der Version 1
oder 2 wird beim ersten Öffnen still gehoben: fehlende Tabellen über `CREATE TABLE IF NOT
EXISTS`, die neue Spalte `seen_mails.content_hash` über `ALTER TABLE … ADD COLUMN`. Es gibt
kein Migrationswerkzeug und keinen Eingriff; Daten gehen nicht verloren. Erkennen lässt sich
der Stand mit

```bash
sqlite3 state.db "PRAGMA user_version;"          # 3 nach der Migration
sqlite3 state.db "PRAGMA table_info(seen_mails);" | grep content_hash
```

Bestehende Zeilen haben `content_hash = NULL`; sie gelten als „Inhalt unbekannt" und lösen nie
eine Kollision aus — das zweite Dedupe-Merkmal wirkt erst für Mails, die nach der Migration
abgerufen werden. **Es gibt keinen Rückweg:** Eine gehobene Datei lehnt eine ältere
MailDigest-Version mit einem `StateError` ab. Wer zurück muss, legt die Datei beiseite und
lässt eine neue anlegen — ungelesene Mails im Mirror-Postfach werden dann erneut verarbeitet.

**Achtung bei DEBUG:** Auf diesem Level werden Tracebacks mitgeschrieben, die
Mail-Inhalte enthalten können (ADR-047). DEBUG-Logs sind so vertraulich wie das Postfach —
nach der Fehlersuche wieder auf `INFO` stellen und die Journal-Einträge ggf. löschen.

## 5. Betriebssignale im Log

| `event` | Bedeutung |
|---------|-----------|
| `runner_started` / `runner_stopped` | Dauerbetrieb aufgenommen/beendet (mit Zyklen, verarbeiteten Mails, wartenden Zustellungen) |
| `mail_processed` | Mail fertig; Feld `status` ist der **tatsächlich gespeicherte** Stand (`delivered`/`checked`/`skipped_low`/`failed`) — `checked` heißt: verarbeitet, Zustellung liegt noch in der Warteschlange |
| `imap_postprocess_failed` | Ein Nachbehandlungs-Kommando wurde abgelehnt (fast immer: `move_processed_to` zeigt auf einen Ordner, den es nicht gibt, oder der Server kann kein `MOVE`). Die Mail ist verarbeitet, sie bleibt nur im Ausgangsordner liegen; der Zyklus läuft weiter (ADR-065) |
| `mail_mime_depth_capped` | Der MIME-Baum einer Mail war tiefer als 32 Ebenen und wurde vor der Auswertung abgeschnitten (Teilbäume darunter sind leer). Kein Grund zur Sorge, aber auch kein Zufall: Reale Mails haben zwei bis vier Ebenen. Ohne den Deckel scheiterte die Rück-Serialisierung mit `RecursionError` (O-1, ADR-020-Nachtrag). Ohne Felder — die Zeile nennt bewusst keine Mail (I5) |
| `mail_ingest_failed` | **ERROR** (Felder `mail`, `error` = Exception-Klasse). Der Ingest konnte eine Mail nicht auswerten. Sie wird trotzdem beansprucht, als `failed`/`ingest_error` gebucht, als Metadaten-Notiz zugestellt und als gelesen markiert — der Zyklus läuft weiter und die Mail blockiert das Postfach nicht (O-1). Taucht das wiederholt auf, gehört die Mail einem Menschen gezeigt |
| `mail_unreadable` | Buchung einer solchen Mail als `failed` (Feld `mail`); die Notiz ist raus |
| `mail_failed_notice` | Fail-closed: Metadaten-Notiz statt Inhalt (Felder `stage`, `reason`) |
| `mail_delivery_queued` | Zustellung liegt in der Warteschlange, Mail bleibt auf `checked` |
| `delivery_deferred` / `delivery_abandoned` | Zustellversuch verschoben bzw. nach 5 Versuchen/1 h aufgegeben. `delivery_deferred` trägt zusätzlich `clock_skew`: `true` heißt, das gemessene Alter der Nachricht war unbrauchbar und die Stundenfrist wurde für diesen Versuch ignoriert (HC-25). Die Zusage lautet damit genau: **fünf Versuche immer, „über höchstens eine Stunde" nur, solange die Systemuhr nicht springt** |
| `mail_id_collision` | **WARNING.** Zwei inhaltlich verschiedene Mails trugen dieselbe `Message-ID`; die zweite wurde trotzdem verarbeitet und zugestellt, unter einem abgeleiteten Schlüssel (ADR-079). Felder `mail` und `collision_mail` sind 12-stellige Hashes. Harmlose Ursache: ein Mailprogramm, das IDs wiederverwendet. Unharmlose Ursache: jemand kopiert die `Message-ID` einer erwarteten Mail, um sie zu unterdrücken — die zugestellte Nachricht trägt dann den Hinweis „Message-ID collides with an earlier mail" |
| `outbox_clock_skew_corrected` | **WARNING** (Feld `rows`). So viele Zeilen der Zustell-Warteschlange hatten eine unplausibel ferne Fälligkeit (> 2 h in der Zukunft) und wurden auf „jetzt" gesetzt. Typische Ursache: NTP-Erstsynchronisation auf einem Gerät ohne Echtzeituhr oder ein VM-Resume. Ohne diese Korrektur bliebe die Nachricht dauerhaft liegen (HC-25) |
| `low_digest_sent` | Sammel-Digest erzeugt (Feld `mails`) |
| `low_digest_failed` | Der Sammel-Digest ist in der ausnahmefesten Zone gescheitert (Feld `error` = Exception-Klasse). Der Lauf geht weiter; die Einträge bleiben liegen und gehen beim nächsten Versuch desselben Tages raus |
| `command_ignored_once` | Bei `run --once` wurde ein `/digest` gelesen und verworfen — im Cron-Betrieb ist es wirkungslos, der Abruf lief gerade (ADR-080) |
| `command_poll_failed` / `command_handling_failed` | Der Befehlskanal war nicht erreichbar bzw. ein Befehl scheiterte. Folgenlos: Die Zustellung ist die Hauptaufgabe, die Fernauslösung nur Bequemlichkeit |
| `imap_reconnect_scheduled` / `ingest_failed` | IMAP-Problem, Reconnect mit Backoff |
| `shutdown_requested` | SIGINT/SIGTERM empfangen, laufender Zyklus wird beendet |
