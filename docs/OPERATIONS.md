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

## 6. Self-hosted mirror mailbox

> For people who run MailDigest on a server of their own and would rather host the mirror
> mailbox there than rent one for €1 a month. Everyone else is better served by the
> provider table in the README — a mail server is a thing you keep, not a thing you set up
> once. The normative description of the command is
> [SPEC-CLI.md](SPEC-CLI.md) §4 (`selfhost-mail`); the reasoning is
> [PLAN-SELFHOST-MAIL.md](PLAN-SELFHOST-MAIL.md) and ADR-089.

`maildigest selfhost-mail` **generates and checks, it never installs.** It writes a
directory of files — DNS records, Postfix settings, a Dovecot drop-in, an apply script, a
checklist — and with `--check` it verifies the finished setup over the network. Everything
privileged is a step you run yourself, visibly, with your own `sudo`; MailDigest keeps the
zero-privilege promise of [SECURITY.md](SECURITY.md) §2, and it never writes the mailbox
password anywhere (I5).

### 6.1 Before you start

Two hard requirements, and neither can be worked around:

- **A domain of your own**, and a **subdomain** for this purpose — `mirror.example.org`,
  not `example.org`. The MX of the apex domain and the mail already running there stay
  untouched that way. The command refuses an apex domain unless you pass `--allow-apex`.
- **Port 25 reachable from the internet**, inbound. Most VPS qualify; home connections
  almost never do, because providers block inbound 25 and the address changes. Test it
  from **another machine** once the server is up:

  ```bash
  openssl s_client -starttls smtp -connect mirror.example.org:25
  ```

  `--check` can only see that something answers on 25 *locally*. That the world reaches it
  is proved by `--check --wait-for-mail` and by nothing else.

Run the command **on the server that will host the mailbox**, as the ordinary user
MailDigest runs as — not as root. MailDigest has to be installed there (README
"Installation") and `maildigest init` has to have run once, because without a configuration
file the command does not know where to put its directory; `--out DIR` is the way around
that if you want to prepare the files before there is a configuration.

Supported are **Debian 13 and newer and Fedora 43 and newer** — the same reach as the
packages (ADR-088), because the generated Dovecot configuration uses the 2.4 syntax. On
Ubuntu 24.04 (Dovecot 2.3) the commented 2.3 equivalents inside `dovecot.conf` apply;
nobody has verified them. Everything below was run end to end on Debian 13 (Postfix 3.10,
Dovecot 2.4) on 2026-09-19.

### 6.2 The five steps

```bash
maildigest selfhost-mail --domain mirror.example.org
```

That writes `selfhost-mail/` **next to the configuration file** — the working directory of
§1 unless `--config` or `MAILDIGEST_CONFIG` points elsewhere — with mode 0700 and six
files inside, and prints the checklist, which is also in `selfhost-mail/checklist.txt`.
`--out DIR` puts the directory somewhere else, and `--check` then needs the same `--out`,
because that is where it reads `state.json` — with `--out` the printed steps 4 and 5 carry
that option already, so the lines can be typed as they stand. The directory has to be new
or empty: an existing directory with anything in it is refused rather than adopted, so a
mistyped `--out ~` cannot scatter six files into your home directory. Every path in the
printed steps names the file that was really written, so run the command from the
directory you mean:

1. **DNS** — create the records from `selfhost-mail/dns.txt` at your DNS provider: an `A`
   record for the subdomain, an `AAAA` record if the server has a global IPv6 address, and
   `MX 10` pointing at the subdomain itself. The command fills in the addresses the host
   found for itself; if it found no global IPv4 address it says so on stderr and leaves a
   placeholder in the `A` line for you to replace with the server's public address.
2. **Packages and certificate**, once, as root:

   ```bash
   sudo apt install postfix dovecot-imapd dovecot-lmtpd certbot
   sudo certbot certonly --standalone -d mirror.example.org
   ```

   On Fedora the same step reads
   `sudo dnf install postfix dovecot certbot` — Fedora ships IMAP and LMTP in the one
   `dovecot` package. Postfix's own Debian installer asks for a configuration type;
   "Internet Site" with the subdomain as the system mail name is right, and `postfix.sh`
   overwrites the settings that matter anyway. `certonly --standalone` needs port 80 free
   for a moment. Only Debian 13 has been walked through end to end; on Fedora the one step
   that can differ is the `dovecot` group that `apply.sh` looks for when it sets the
   permissions of `/etc/dovecot/users`.
3. **Apply**, as root — this is the only step that changes the system:

   ```bash
   sudo sh selfhost-mail/apply.sh
   ```

   It asks for the mailbox password once. Choose a long random one; you will need it
   again in step 5 and in `connect-mail`, and no part of MailDigest stores it.
4. **Check** from the same machine:

   ```bash
   maildigest selfhost-mail --check
   ```

   Eight lines, each `ok`, `FAIL` or `skipped`, and every `FAIL` carries the one sentence
   that says what to do. Exit code 1 if anything failed.
5. **Prove the chain.** Forward one mail from your real mailbox to the generated address
   and let the command wait for it:

   ```bash
   maildigest selfhost-mail --check --wait-for-mail
   ```

   It waits up to ten minutes (`--timeout`, 10…3600 s), prints the sender and subject of
   the mail that arrived, and then the block `connect-mail` needs: host, port 993,
   username (= the address), and the `maildigest connect-mail …` call to run next. Only
   after this step is the setup proved — up to here you have a server that answers
   locally, not one the internet can deliver to.

`--check` asks for the mailbox password (or takes it from `MAILDIGEST_IMAP_PASSWORD`),
because the login test is a real IMAPS login. There is deliberately no `--password`
option: that is how it stays out of the process list and the shell history.

From there, `maildigest connect-mail` is the ordinary path of the README, with the
self-hosted mailbox in place of a provider's.

### 6.3 What `apply.sh` changes — and how to undo it

Seven changes, all of them in named places:

| What | Where | Undo |
|---|---|---|
| System group and user `vmail` (no login shell) | `/etc/passwd`, `/etc/group` | `sudo userdel vmail && sudo groupdel vmail` |
| The mail directory | `/var/mail/vmail/<domain>/<local part>/` | `sudo rm -rf /var/mail/vmail` — that deletes the mailbox contents |
| Dovecot drop-in | `/etc/dovecot/conf.d/99-maildigest.conf` | `sudo rm` it, then reload Dovecot; the distribution's own configuration is untouched and takes over again |
| The mailbox user and its password hash | `/etc/dovecot/users` (`root:dovecot` 0640 where that group exists, `root:root` 0600 otherwise) | `sudo rm` it |
| Postfix settings | `main.cf`, the keys listed in `selfhost-mail/postfix.sh` | `sudo postconf -X <key>` per key resets it to the built-in default; `postconf -n` shows what deviates from it |
| certbot deploy hook | `/etc/letsencrypt/renewal-hooks/deploy/maildigest-reload.sh` | `sudo rm` it |
| Restart of Postfix, reload of Dovecot | — | nothing to undo |

Postfix is **restarted**, not reloaded: `postfix.sh` sets `inet_interfaces`, and Postfix
reads that key only at start-up. On a host that was installed as "Local only" a reload
would leave smtpd listening on loopback alone — and `--check` probes exactly 127.0.0.1:25,
so every SMTP line would say `ok` while no forward from the internet could ever arrive.

Nothing else is touched. Re-running `apply.sh` is safe: every step either checks first or
overwrites its own result. Before it reloads, it runs `doveconf -n` and stops if Dovecot
refuses the configuration — without that check a drop-in Dovecot cannot read leaves the
old configuration running while `systemctl reload` still reports success, which is the
most confusing failure this feature has.

Removing everything, in one go:

```bash
sudo rm -f /etc/dovecot/conf.d/99-maildigest.conf /etc/dovecot/users \
           /etc/letsencrypt/renewal-hooks/deploy/maildigest-reload.sh
sudo systemctl reload dovecot
# Postfix: reset the keys from selfhost-mail/postfix.sh, e.g.
sudo postconf -X virtual_mailbox_domains virtual_mailbox_maps virtual_transport
sudo systemctl reload postfix
sudo rm -rf /var/mail/vmail            # deletes the stored mail
sudo userdel vmail; sudo groupdel vmail
```

### 6.4 Certificate renewal

The certificate is certbot's, and renewal is certbot's job — the systemd timer or cron
entry that the certbot package installs. What `apply.sh` adds is the missing half:
`/etc/letsencrypt/renewal-hooks/deploy/maildigest-reload.sh`, which reloads Postfix and
Dovecot after a successful renewal. Without it both services keep serving the expired
certificate in memory until someone restarts them.

`maildigest selfhost-mail --check` warns on stderr when fewer than 14 days are left
(`Certificate for <domain> expires in <N> days — check the certbot timer.`) while the
`IMAPS cert` line still says `ok`. That is how a silently failed renewal surfaces in the
one command people run when something looks off. A dry run tests the whole path:

```bash
sudo certbot renew --dry-run
```

**One trap worth knowing.** If you point `--cert-dir` at a directory that systemd hides
from `dovecot.service` — anything below `/tmp` when the unit has `PrivateTmp=yes` — then
`apply.sh` succeeds, `doveconf -n` succeeds (it runs as root, outside the unit's
namespace), the reload succeeds, and only `--check` reports that the certificate is wrong.
`/etc/letsencrypt/live/…`, the default, has no such problem. `--cert-dir` exists for the
container probe; in operation, leave it alone.

### 6.5 Rotating the address

The random local part (`mirror-7f3a9c1d@…`) is the spam defence: nobody guesses it, and
every other recipient at the domain is refused at `RCPT TO`. If it ever leaks — you
posted a log, a forwarding service was breached — rotate it:

```bash
rm -rf selfhost-mail                   # next to config.toml, or wherever --out put it
maildigest selfhost-mail --domain mirror.example.org
sudo sh selfhost-mail/apply.sh         # writes /etc/dovecot/users anew: one user, the new one
maildigest connect-mail                # the username changed
```

The new address is live as soon as `apply.sh` has run, and the old one stops being
accepted at the same moment; change the forwarding rule in your real mailbox **first**, or
mail bounces in between. The old mail directory under `/var/mail/vmail/<domain>/` stays
behind and can be deleted. DNS and the certificate do not change — the domain is the same.

### 6.6 Limits you are signing up for

- **Port 25 inbound, and a domain of your own.** See §6.1; these two are not negotiable
  and no amount of configuration replaces them.
- **Inbound TLS is opportunistic** (`smtpd_tls_security_level = may`). Forcing encryption
  would make some forwarders give up silently, and a forward that vanishes is worse than a
  forward that could theoretically be read in transit. The mail is a copy of mail that
  already travelled the internet once.
- **One recipient, no `postmaster@`, no `abuse@`.** Postfix accepts exactly the generated
  address; everything else is refused at `RCPT TO`, so there is no backscatter and no
  mailbox for `root@`. RFC 5321 expects a postmaster mailbox on a mail-receiving host, and
  this host deliberately does not have one: it is a private sink for one person's
  forwarded mail. If your hoster's policy insists, add the alias by hand to
  `virtual_mailbox_maps` in `selfhost-mail/postfix.sh` and apply it again — and then read
  that mailbox, or you have created the backscatter you avoided.
- **Mail larger than the configured limit bounces** (`message_size_limit`, generated from
  `[limits] max_mail_bytes` of your configuration file — 26214400, i.e. 25 MiB, when there
  is none; change the limit and generate the files again if you want another value): the forwarder gets a bounce it can show you,
  instead of the mail vanishing into a mailbox where MailDigest would reduce it to a
  metadata note anyway.
- **Cleartext IMAP on 143 is closed** and stays closed; MailDigest connects over IMAPS
  only. `--check` tests that the port really is shut.
- **No spam filter, no virus scanner, no outbound mail.** The box receives your own
  forwards and nothing else; the random address is the whole defence. If that address ever
  becomes public, rotate it (§6.5) rather than bolt Rspamd onto it.
- **Deliverability is not your problem here, but reputation is.** Some forwarders check
  that the receiving host has sensible reverse DNS; if your hoster lets you set a PTR
  record for the IP, set it to the subdomain.
- **This is now a mail server you own.** Keep the machine patched, and remember that a
  `--check` run costs ten seconds — it is the right first move whenever summaries stop
  arriving.

### 6.7 When a check fails

Each `FAIL` line already names its fix; three that deserve a sentence more:

- **Read the `SMTP banner` line first.** If the dialogue with `127.0.0.1:25` does not come
  about at all — Postfix not running, nothing listening — then `SMTP relay` and
  `SMTP recipient` fail with it, and the relay line's text ("that is an open relay") then
  describes the case the check could not rule out, not one it observed. Get the banner
  line to `ok` and read the other two again.
- **`IMAPS login` fails right after `IMAPS cert` failed.** Read the certificate line
  first: if the TLS handshake was rejected, no login was attempted at all, and the advice
  to rerun `apply.sh` with a new password would change nothing. Fix the certificate, then
  run `--check` again.
- **`DNS MX` says `skipped`.** The MX lookup needs `dnspython`, which MailDigest does not
  depend on (ADR-089): `sudo apt install python3-dnspython` (Debian) or
  `sudo dnf install python3-dns` (Fedora), in the environment MailDigest runs from. A
  `skipped` line never changes the exit code — it is a check you did not run, not a check
  that failed. One setup where it has to stay `skipped`: a domain faked with an
  `/etc/hosts` entry (the probe setup of PLAN-SELFHOST-MAIL §7). A hosts file cannot
  express an MX record, so with dnspython installed that line is permanently `FAIL` and
  the exit code permanently 1 — leave dnspython out of such an environment.
- **`Mail` times out** although everything else is `ok`. That is nearly always inbound
  port 25: run the `openssl s_client` one-liner of §6.1 from another machine. If that
  works, look at `/var/log/mail.log` on the server while you forward the mail — Postfix
  logs every rejected recipient with the reason.

### 6.8 The alternative

If you would rather run a container than a checklist,
[docker-mailserver](https://docker-mailserver.github.io/) is the established full package
(Postfix, Dovecot, Rspamd, ClamAV) — more machinery than one person's forwards need, no
checked result at the end, and Docker on a box that otherwise needs nothing, but a
maintained and much-travelled path.
