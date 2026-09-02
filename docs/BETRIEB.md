# MailDigest — Betrieb (Entwurf, WP8)

> Wie MailDigest dauerhaft läuft: als systemd-Dienst (empfohlen) oder per Cron. Der
> CLI-Vertrag entsteht in WP9 (docs/SPEC-CLI.md); dieser Entwurf beschreibt den Betrieb des
> in WP8 fertiggestellten Runners und wird in WP9/WP12 gegen die endgültige CLI abgeglichen.

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

## 4. Wartung

| Aufgabe | Vorgehen |
|---------|----------|
| Konfiguration ändern | Datei bearbeiten, `systemctl restart maildigest` |
| Zustand ansehen | `sqlite3 state.db "SELECT status, COUNT(*) FROM seen_mails GROUP BY status;"` |
| Wartende Zustellungen | `sqlite3 state.db "SELECT kind, attempts, next_attempt_at FROM outbox;"` |
| Sammel-Digest-Rückstand | `sqlite3 state.db "SELECT COUNT(*) FROM low_digest_queue;"` |
| Backup | `config.toml` sichern; `state.db` ist reproduzierbarer Betriebszustand — geht sie verloren, werden ungelesene Mails im Mirror-Postfach erneut verarbeitet (nie doppelt zugestellt, solange sie als gelesen markiert sind) |
| Fehlersuche | vorübergehend `[general] log_level = "DEBUG"` |

**Achtung bei DEBUG:** Auf diesem Level werden Tracebacks mitgeschrieben, die
Mail-Inhalte enthalten können (ADR-047). DEBUG-Logs sind so vertraulich wie das Postfach —
nach der Fehlersuche wieder auf `INFO` stellen und die Journal-Einträge ggf. löschen.

## 5. Betriebssignale im Log

| `event` | Bedeutung |
|---------|-----------|
| `runner_started` / `runner_stopped` | Dauerbetrieb aufgenommen/beendet (mit Zyklen, verarbeiteten Mails, wartenden Zustellungen) |
| `mail_processed` | Mail fertig (Feld `status`: `delivered`/`skipped_low`/`failed`) |
| `mail_failed_notice` | Fail-closed: Metadaten-Notiz statt Inhalt (Felder `stage`, `reason`) |
| `mail_delivery_queued` | Zustellung liegt in der Warteschlange, Mail bleibt auf `checked` |
| `delivery_deferred` / `delivery_abandoned` | Zustellversuch verschoben bzw. nach 5 Versuchen/1 h aufgegeben |
| `low_digest_sent` | Sammel-Digest erzeugt (Feld `mails`) |
| `imap_reconnect_scheduled` / `ingest_failed` | IMAP-Problem, Reconnect mit Backoff |
| `shutdown_requested` | SIGINT/SIGTERM empfangen, laufender Zyklus wird beendet |
