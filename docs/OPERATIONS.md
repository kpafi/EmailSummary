# MailDigest — Operations

> How MailDigest runs permanently: as a systemd service (recommended) or from cron.
> As of WP9: the commands used here (`maildigest run`, `maildigest run --once`,
> `--config`) exist and are described normatively in [SPEC-CLI.md](SPEC-CLI.md) — where the
> two documents disagree, SPEC-CLI.md wins. The environment variables for secrets are
> listed there in §5.

## 1. Basic assumptions

- **One process, one instance, one mailbox.** Two instances running concurrently against
  the same state database are not supported (SQLite locks, duplicate delivery). That is why
  the systemd service is not a template unit.
- Files in the service's working directory:
  - `config.toml` — configuration **including secrets**, mode `0600`.
  - `state.db` (+ `-wal`/`-shm`) — state, collected-digest and delivery queues, mode
    `0600`. The path can be changed with `[general] state_db` (ADR-045).
- Keep secrets **out** of the file where possible and pass them as environment variables:
  `MAILDIGEST_IMAP_PASSWORD`, `MAILDIGEST_LLM_API_KEY`, `MAILDIGEST_TELEGRAM_TOKEN`.

## 2. systemd unit (recommended)

`/etc/systemd/system/maildigest.service` — the service runs under its own unprivileged
user:

```ini
[Unit]
Description=MailDigest — email summaries to your messenger
Documentation=https://example.invalid/maildigest
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=maildigest
Group=maildigest
WorkingDirectory=/var/lib/maildigest
Environment=PYTHONUNBUFFERED=1
# Secrets from a file with mode 0600 that only root and the service may read:
EnvironmentFile=/etc/maildigest/secrets.env
ExecStart=/opt/maildigest/.venv/bin/maildigest run --config /var/lib/maildigest/config.toml

# Clean shutdown: SIGTERM finishes the running cycle and then stops (ADR-051).
KillSignal=SIGTERM
TimeoutStopSec=120
Restart=on-failure
RestartSec=30s

# Hardening (the service only needs its data directory and outbound network):
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

`AF_UNIX` is only needed for the optional Signal adapter (`signal-cli` socket) and can be
dropped otherwise.

Bringing it up:

```bash
sudo useradd --system --home /var/lib/maildigest --shell /usr/sbin/nologin maildigest
sudo install -d -o maildigest -g maildigest -m 0700 /var/lib/maildigest
sudo install -o maildigest -g maildigest -m 0600 config.toml /var/lib/maildigest/config.toml
sudo systemctl daemon-reload
sudo systemctl enable --now maildigest.service
```

Reading the logs (JSON lines, ADR-046):

```bash
journalctl -u maildigest -f -o cat | jq .
journalctl -u maildigest -o cat | jq 'select(.level=="ERROR")'
```

## 3. The cron alternative

If you do not want a long-lived process (small server, laptop), call the one-shot run from
cron. `run --once` works off the delivery queue, fetches new mail, works the queue off
again and checks the collected digest — then exits.

```cron
# Process new mail every 10 minutes; output goes to syslog.
*/10 * * * * maildigest /opt/maildigest/.venv/bin/maildigest run --once \
    --config /var/lib/maildigest/config.toml 2>&1 | /usr/bin/logger -t maildigest
```

Notes on cron operation:

- The interval should be **shorter** than an hour: the delivery queue gives up on an
  undeliverable message after at most an hour (ADR-048), and no retries happen between
  runs.
- `[general] low_digest_time` is served on the first run **at or after** that time — a
  10-minute grid always hits it. If the machine is not running at that time at all, the
  digest is caught up on the next run of the same day, and not after that.
- Avoid overlapping runs (`flock`) so that two processes never write to the same state
  database:
  `*/10 * * * * maildigest /usr/bin/flock -n /var/lib/maildigest/run.lock /opt/…/maildigest run --once …`
- `[imap] poll_interval_seconds` has no effect under cron (there is no loop).
- The command channel (`[messenger.telegram] accept_commands`, on by default) is served
  under cron as well, but only **once at the end** of each run: `/status` is answered,
  `/digest` has no effect there and is merely consumed (log line `command_ignored_once`).
  The answer therefore arrives with a delay of up to one cron interval. If you want an
  immediate reaction, use continuous operation — latency there is at most ten seconds
  (ADR-080).
- A mailbox outage aborts the run with exit code 1, but only after the delivery queue and
  any due collected digest have been worked off: neither needs IMAP (ADR-049 addendum).
- **`run --once` does not run under the signal handlers.** Only continuous operation
  installs the clean shutdown from ADR-051. If a one-shot run is aborted hard — Ctrl+C, a
  cron timeout, a `systemd` kill — a mail currently being processed can be left in state
  `sanitized`. It is not lost and can be queried in the database
  (`SELECT status, COUNT(*) FROM seen_mails GROUP BY status;`), but on the next run it is
  recognised as a duplicate and **not** processed again (ADR-019 records this consequence
  as accepted). If you do not want that, give the cron entry a generous timeout or use
  continuous operation.

## 4. Maintenance

| Task | How |
|---------|----------|
| Change the configuration | edit the file, `systemctl restart maildigest` |
| Inspect state | `sqlite3 state.db "SELECT status, COUNT(*) FROM seen_mails GROUP BY status;"` |
| Pending deliveries | `sqlite3 state.db "SELECT kind, attempts, next_attempt_at FROM outbox;"` |
| Collected-digest backlog | `sqlite3 state.db "SELECT COUNT(*) FROM low_digest_queue;"` |
| Schema migration | happens by itself, see below |
| Backup | back up `config.toml`; `state.db` is reproducible operational state — if it is lost, unread mail in the mirror mailbox is processed again (never delivered twice as long as it is marked as read) |
| Troubleshooting | temporarily set `[general] log_level = "DEBUG"` |

**Schema version of the state database.** Since ADR-079 it is **3**. A file of version 1 or
2 is upgraded silently when first opened: missing tables via `CREATE TABLE IF NOT EXISTS`,
the new column `seen_mails.content_hash` via `ALTER TABLE … ADD COLUMN`. There is no
migration tool and no manual step; no data is lost. You can check the state with

```bash
sqlite3 state.db "PRAGMA user_version;"          # 3 after the migration
sqlite3 state.db "PRAGMA table_info(seen_mails);" | grep content_hash
```

Existing rows have `content_hash = NULL`; they count as "content unknown" and never trigger
a collision — the second dedupe criterion only takes effect for mail fetched after the
migration. **There is no way back:** an older MailDigest version rejects an upgraded file
with a `StateError`. If you have to go back, move the file aside and let a new one be
created — unread mail in the mirror mailbox is then processed again.

**Careful with DEBUG:** at this level tracebacks are written out that can contain mail
content (ADR-047). DEBUG logs are as confidential as the mailbox — set it back to `INFO`
after troubleshooting and delete the journal entries if appropriate.

## 5. Operational signals in the log

| `event` | Meaning |
|---------|-----------|
| `runner_started` / `runner_stopped` | Continuous operation started/stopped (with cycles, mail processed, pending deliveries) |
| `mail_processed` | Mail done; the field `status` is the state **actually stored** (`delivered`/`checked`/`skipped_low`/`failed`) — `checked` means: processed, delivery still in the queue |
| `imap_postprocess_failed` | A post-processing command was rejected (nearly always: `move_processed_to` points at a folder that does not exist, or the server cannot do `MOVE`). The mail is processed, it just stays in the source folder; the cycle continues (ADR-065) |
| `mail_mime_depth_capped` | A mail's MIME tree was deeper than 32 levels and was truncated before evaluation (subtrees below that are empty). No cause for concern, but no accident either: real mail has two to four levels. Without the cap, re-serialisation failed with `RecursionError` (O-1, ADR-020 addendum). No fields — the line deliberately names no mail (I5) |
| `mail_ingest_failed` | **ERROR** (fields `mail`, `error` = exception class). Ingest could not evaluate a mail. It is claimed anyway, booked as `failed`/`ingest_error`, delivered as a metadata note and marked as read — the cycle continues and the mail does not block the mailbox (O-1). If this appears repeatedly, the mail deserves a human's eyes |
| `mail_unreadable` | Booking such a mail as `failed` (field `mail`); the note has gone out |
| `mail_unparsable` | **ERROR** (fields `mail` = shortened dedupe hash, `error` = exception class, e.g. `RecursionError`). Even the IMAP library could not parse the mail (for instance around 1000 nested `message/rfc822` parts). MailDigest isolated it per UID, fetched the headers separately and treated it like an unreadable mail: note, `failed`/`ingest_error`, marked as read — the cycle continues, a restart does not help and is not needed. **Not** a connection problem: if `ingest_failed` with `ImapConnectionError` appears instead, the cause is the network or the server (O-1, ADR-020 addendum, second iteration) |
| `mail_header_fetch_failed` | WARNING (field `error`). The header fetch for an unparsable mail was rejected or failed; the note then comes without sender/subject and the dedupe key rests on the UID alone. If a connection error follows immediately, the connection was gone |
| `mail_failed_notice` | Fail-closed: metadata note instead of content (fields `stage`, `reason`) |
| `mail_delivery_queued` | Delivery is in the queue, the mail stays at `checked` |
| `delivery_deferred` / `delivery_abandoned` | Delivery attempt postponed, or given up after 5 attempts / 1 h. `delivery_deferred` additionally carries `clock_skew`: `true` means the measured age of the message was unusable and the one-hour limit was ignored for this attempt (HC-25). The promise is therefore exactly: **five attempts always, "over at most one hour" only as long as the system clock does not jump** |
| `mail_id_collision` | **WARNING.** Two mails with different content carried the same `Message-ID`; the second one was processed and delivered anyway, under a derived key (ADR-079). The fields `mail` and `collision_mail` are 12-character hashes. Harmless cause: a mail program that reuses IDs. Less harmless cause: someone copies the `Message-ID` of an expected mail in order to suppress it — the delivered message then carries the note "Message-ID collides with an earlier mail" |
| `outbox_clock_skew_corrected` | **WARNING** (field `rows`). That many rows of the delivery queue had an implausibly distant due time (> 2 h in the future) and were reset to "now". Typical cause: a first NTP sync on a device without a real-time clock, or a VM resume. Without this correction the message would stay queued forever (HC-25) |
| `low_digest_sent` | Collected digest produced (field `mails`) |
| `low_digest_failed` | The collected digest failed inside the exception-proof zone (field `error` = exception class). The run continues; the entries stay queued and go out on the next attempt the same day |
| `command_ignored_once` | Under `run --once` a `/digest` was read and discarded — under cron it has no effect, the fetch has just happened (ADR-080) |
| `command_poll_failed` / `command_handling_failed` | The command channel was unreachable, or a command failed. Inconsequential: delivery is the main job, remote triggering only a convenience |
| `imap_reconnect_scheduled` / `ingest_failed` | IMAP problem, reconnect with backoff |
| `shutdown_requested` | SIGINT/SIGTERM received, the running cycle is being finished |
