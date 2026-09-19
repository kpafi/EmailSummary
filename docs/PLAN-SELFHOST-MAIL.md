# Plan: a self-hosted mirror mailbox — `maildigest selfhost-mail`

Status — **built 2026-09-19; see [CHANGELOG.md](../CHANGELOG.md)** (S1–S5, the findings of
the first real run in §7a; operation in [OPERATIONS.md](OPERATIONS.md) §6). Goal: people
who run MailDigest on a server of their own anyway should be able to host the mirror
mailbox on that same server, without renting a Posteo or mailbox.org account. The command
must end where `maildigest connect-mail` begins: with an IMAPS host, a username and a
forwarding address that are known to work.

This plan describes the deliberately **lean** variant: the command *generates and checks*,
it never installs, configures or restarts anything as root. What it produces is a set of
files and a checklist; the person runs the few privileged steps themselves. The decision
and its reasoning are drafted as ADR-089 in §10 and go into [DECISIONS.md](DECISIONS.md)
(German, like the rest of that record) with the first work package.

Read first: PLAN.md §1–§2 (principles, invariants I1–I8), [SECURITY.md](SECURITY.md) §2
(zero-privilege principle), [SPEC-CLI.md](SPEC-CLI.md) §4 (`connect-mail`), and
[OPERATIONS.md](OPERATIONS.md) §1–§2 (how the service runs). On conflict:
SECURITY.md > REQUIREMENTS.md > this plan.

## 1. What the user ends up typing

On the server that will host the mailbox (Debian 13 or newer; see §4 for the reach):

```bash
maildigest selfhost-mail --domain mirror.example.org
```

The command asks for nothing else. It writes a directory `selfhost-mail/` next to the
configuration file, prints the checklist and stops. The checklist is short:

```
1. DNS — create these records at your DNS provider (selfhost-mail/dns.txt):
     mirror.example.org.   A     203.0.113.7
     mirror.example.org.   AAAA  2001:db8::7          (only if the server has IPv6)
     mirror.example.org.   MX 10 mirror.example.org.
2. Packages and certificate, once, as root:
     sudo apt install postfix dovecot-imapd dovecot-lmtpd certbot
     sudo certbot certonly --standalone -d mirror.example.org
3. Apply the generated configuration, as root (it prompts for the mailbox password):
     sudo sh selfhost-mail/apply.sh
4. Check from this machine:
     maildigest selfhost-mail --check
5. Prove the whole chain: forward one mail from your real mailbox to
     mirror-7f3a9c1d@mirror.example.org
   and let the command wait for it:
     maildigest selfhost-mail --check --wait-for-mail
```

When step 5 passes, the command prints the block that `connect-mail` needs and offers the
exact call:

```
Mirror mailbox ready.
  IMAP host:   mirror.example.org
  Port:        993
  Username:    mirror-7f3a9c1d@mirror.example.org
  Forward to:  mirror-7f3a9c1d@mirror.example.org
  Password:    the one you entered in apply.sh (not stored anywhere by MailDigest)

Next: maildigest connect-mail --host mirror.example.org --username mirror-7f3a9c1d@mirror.example.org
```

That is the whole gain: the person who already administers a server gets a mirror mailbox
in five steps, and every step is visible, reversible and runs under their own `sudo` — not
under MailDigest's.

## 2. Why not install it ourselves

The version that was first imagined — "MailDigest sets up Postfix and Dovecot for me" — was
rejected for four reasons, in order of weight:

1. **Zero-privilege stays zero-privilege.** MailDigest today needs nothing but its working
   directory and outbound network (SECURITY.md §2, OPERATIONS.md §2 hardening). A command
   that writes into `/etc/postfix` and restarts system services would be the only part of
   the program that needs root, and the distribution packages (ADR-088) would have to
   carry Postfix and Dovecot as dependencies or configure services they do not own —
   which Debian policy rightly frowns upon.
2. **Mail servers are the classic support sink.** Every layer between "forwarded from
   Gmail" and "visible over IMAPS" can fail silently: blocked port 25, a provider that
   refuses to forward to a host without a valid certificate, a reverse-DNS lookup that
   times out. Generated files a person can read beat magic they cannot debug.
3. **The audience is small.** Everyone else is better served by the €1/month provider
   table in the README. A lean command keeps the maintenance proportional to that.
4. **Root-less is testable.** Templates plus a checker can be exercised in CI in a
   container (§7); an installer that needs `systemctl restart postfix` on the developer's
   machine cannot.

What the lean variant keeps from the original idea: at the end there is a mailbox, and its
credentials are known-good, because `--check --wait-for-mail` proved the chain end to end.

## 3. Target design

```
maildigest selfhost-mail --domain D
    │
    ├─ selfhost-mail/dns.txt              records to create (A/AAAA/MX)
    ├─ selfhost-mail/postfix.sh           postconf -e … (idempotent, only these keys)
    ├─ selfhost-mail/dovecot.conf         → /etc/dovecot/conf.d/99-maildigest.conf
    ├─ selfhost-mail/apply.sh             copies the two above into place, creates the
    │                                     vmail user, asks for the mailbox password once,
    │                                     writes the Dovecot passwd-file (hash only),
    │                                     reloads both services
    ├─ selfhost-mail/checklist.txt        the five steps of §1, filled in
    └─ selfhost-mail/state.json           domain, address, generated at, no secrets

maildigest selfhost-mail --check [--wait-for-mail]
    │  (reads state.json, talks only over the network — never reads /etc)
    ├─ DNS      A/AAAA of D resolve to a public address; MX of D points at D
    ├─ SMTP     127.0.0.1:25 answers with a 220 banner naming D;
    │           RCPT TO an outside address is refused (no open relay);
    │           RCPT TO the mirror address is accepted
    ├─ IMAPS    D:993 — certificate chain and hostname valid (create_default_context),
    │           expiry ≥ 14 days, login succeeds, INBOX selectable, 143 is closed
    └─ mail     with --wait-for-mail: polls INBOX up to 10 minutes for a new mail and
                prints its From/Subject line (proves the forwarding end to end)
```

**One address, one virtual user, no system account.** The mailbox is a Dovecot
*passwd-file* user `mirror-<8 hex>@D`, mail owned by a dedicated system user `vmail` under
`/var/mail/vmail/D/`. Postfix accepts **exactly this one recipient** and hands it to
Dovecot over LMTP. Consequences that matter:

- The IMAP username *is* the mail address — the same rule the README already teaches
  ("the username is nearly always the full mail address").
- The random local part is the spam defence: nobody guesses `mirror-7f3a9c1d`, and every
  other recipient at `D` is refused at `RCPT TO`, so no backscatter and no mailbox for
  `root@D` or `postmaster@D` (the latter is deliberate and documented; see §4).
- No password is ever written by MailDigest. `apply.sh` prompts once and stores only the
  `doveadm pw -s BLF-CRYPT` hash in `/etc/dovecot/users` (mode 0600). Invariant I5 holds
  by construction: `selfhost-mail/` contains no secret and can be committed or mailed.

**Generated Postfix settings** (`postfix.sh`, all via `postconf -e`, nothing else touched):

```
myhostname = D
mydestination =                      # nothing local; everything goes through virtual_*
mynetworks_style = host
virtual_mailbox_domains = D
virtual_mailbox_maps = inline:{ mirror-7f3a9c1d@D=1 }
virtual_transport = lmtp:unix:private/dovecot-lmtp
smtpd_relay_restrictions = reject_unauth_destination
smtpd_recipient_restrictions = reject_unauth_destination, reject_unlisted_recipient
smtpd_helo_required = yes
disable_vrfy_command = yes
smtpd_tls_cert_file = /etc/letsencrypt/live/D/fullchain.pem
smtpd_tls_key_file = /etc/letsencrypt/live/D/privkey.pem
smtpd_tls_security_level = may       # inbound must stay opportunistic; see §4
message_size_limit = 26214400        # = [limits] max_mail_bytes, so oversize mail
                                     #   bounces to the forwarder instead of vanishing
inet_interfaces = all
```

`permit_mynetworks` is deliberately absent from the relay restrictions: nothing on that box
should relay through this Postfix, and its absence is what lets `--check` test for an open
relay from localhost. Local system mail (cron, logwatch) enters through `sendmail(1)` and
the pickup service, which these restrictions do not touch.

**Generated Dovecot settings** (`dovecot.conf`, Dovecot 2.4 syntax — Debian 13 ships 2.4;
the 2.3 equivalents are listed in the file as comments for people on older systems):

```
protocols = imap lmtp
mail_driver = maildir
mail_path = /var/mail/vmail/%{user | domain}/%{user | username}
mail_inbox_path = /var/mail/vmail/%{user | domain}/%{user | username}
mail_uid = vmail
mail_gid = vmail
ssl = required
ssl_server_cert_file = /etc/letsencrypt/live/D/fullchain.pem
ssl_server_key_file = /etc/letsencrypt/live/D/privkey.pem
auth_mechanisms = plain
passdb passwd-file { passwd_file_path = /etc/dovecot/users }
userdb  passwd-file { passwd_file_path = /etc/dovecot/users }
service imap-login { inet_listener imap { port = 0 } }            # no 143
service lmtp { unix_listener /var/spool/postfix/private/dovecot-lmtp {
  mode = 0600  user = postfix  group = postfix } }
```

`/etc/dovecot/users` holds one line in the passwd-file format
`user:password:uid:gid::home::` — here
`mirror-7f3a9c1d@D:{BLF-CRYPT}$2y$…:vmail:vmail::/var/mail/vmail/D/mirror-7f3a9c1d::`;
`apply.sh` builds it from `doveadm pw -s BLF-CRYPT` and never echoes the password.

`mail_inbox_path` is set explicitly: left to the default, Dovecot 2.4 creates a separate
`.INBOX` folder and the root Maildir stays empty (found the hard way on 2026-09-18).

**`--check` reuses what exists.** The IMAPS probe is `ImapClient` with the same
`ssl.create_default_context()` the daemon uses (imap_client.py), so a certificate that
passes the check passes in operation. The SMTP dialogue is `smtplib` from the standard
library. The MX lookup has no standard-library answer; the decision is in §5.

## 4. Reach and limits — said plainly in the checklist

- **Port 25 must be reachable from the internet.** Home connections almost never qualify
  (blocked inbound 25, changing address); most VPS do. `--check` can only see that
  *something* listens on 25 locally; whether the world can reach it is proved by
  `--wait-for-mail`, nothing less. The checklist names a one-liner to run from any other
  machine in between: `openssl s_client -starttls smtp -connect D:25`.
- **A domain of your own is required**, and a dedicated subdomain is recommended
  (`mirror.example.org`), so the MX of the main domain and its existing mail stay
  untouched. The command refuses a bare second-level domain unless `--allow-apex` is given.
- **Inbound TLS is opportunistic** (`security_level = may`). Forcing encryption would make
  some forwarders give up silently; Gmail, Posteo, GMX and the other providers in the
  README table all use TLS when it is offered.
- **No `postmaster@D`, no `abuse@D`.** RFC 5321 expects a postmaster mailbox on every
  mail-receiving host; this host is a private sink for one person's forwarded mail and
  accepts one address on purpose. The checklist says so, and says what to do if an
  operator insists (add the alias to `virtual_mailbox_maps` by hand).
- **Debian 13 and newer, Fedora 43 and newer** — the same reach as the packages (ADR-088),
  and the reason is the Dovecot 2.4 syntax. Ubuntu 24.04 (Dovecot 2.3) gets the commented
  2.3 equivalents and a "not verified" note, no more.
- **Certificate renewal is certbot's job.** `apply.sh` installs a certbot deploy hook that
  reloads Postfix and Dovecot; `--check` warns below 14 days so a silent renewal failure
  shows up in the one command people will run when something looks off.

## 5. Decisions taken in this plan

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | Command name | `selfhost-mail` | Mirrors `connect-mail`; "mirror" is implied everywhere else in the CLI too. `selfhost-mirrormail` stays an accepted alias only if the owner insists. |
| D2 | Generate vs. install | Generate + check, never install | §2. Keeps MailDigest root-less and the packages dependency-free. |
| D3 | Mailbox identity | One virtual passwd-file user `mirror-<hex>@D`, `vmail` owner, Postfix→Dovecot LMTP | One accepted recipient, username = address, no system account, no plaintext password anywhere (I5). |
| D4 | MX lookup | `dnspython` as an **optional** import; without it the MX step prints "skipped — install python3-dnspython (Debian) / python3-dns (Fedora) for the MX check" and the check continues | No new hard runtime dependency (PLAN.md §5 rule 3); a hand-written DNS parser would be new attack surface for one optional step. Recorded in ADR-089. Packages list it under `Suggests`/weak deps. |
| D5 | Where the files go | `selfhost-mail/` next to the configuration file; `--out DIR` overrides | Same neighbourhood as `config.toml` and `state.db`; found again by `--check` through `state.json`. |
| D6 | Secrets | The command writes none. `apply.sh` prompts, hashes, stores only the hash | I5; the generated directory is safe to keep and share. |
| D7 | `--check` reads no system files | Everything observed over the network or via `state.json` | Works without root, and it tests what the outside world sees rather than what a file says. |
| D8 | Failure semantics | Each check line ends `ok` / `FAIL` / `skipped` with a one-sentence fix; exit code 1 on any FAIL, 0 otherwise; `--wait-for-mail` timeout is a FAIL | Matches the `connect-*` commands and the exit-code table in SPEC-CLI.md §2. |

## 6. What is added to the repository

| Path | Content |
|---|---|
| `src/maildigest/selfhost.py` | Pure module: address generation, domain validation (ASCII hostnames only, no IDN, `--allow-apex` rule), template rendering for the five files, `state.json` read/write. No network, no I/O beyond the output directory. |
| `src/maildigest/selfhost_check.py` | The checks of §3: DNS (stdlib + optional dnspython), SMTP dialogue, IMAPS via `ImapClient`, wait-for-mail. Returns a list of `CheckResult(name, status, detail, fix)`; printing is the CLI's job. |
| `src/maildigest/cli.py` | `cmd_selfhost_mail`, options `--domain`, `--out`, `--allow-apex`, `--check`, `--wait-for-mail`, `--timeout`; honours the global `--non-interactive`. Registered in `build_parser()`, so `--man` and `man/maildigest.1` pick it up. |
| `src/maildigest/data/selfhost/*.tmpl` | The templates, one file each, with `{{domain}}`, `{{address}}`, `{{cert_dir}}` placeholders; kept out of Python strings so they can be read and diffed as what they are. |
| `tests/unit/test_selfhost.py` | Rendering is deterministic and contains no secret; domain rules; the generated `postfix.sh` contains only `postconf -e` lines; `dovecot.conf` never contains `ssl = no`/`yes` or an open 143 listener; `state.json` round-trip. |
| `tests/unit/test_selfhost_check.py` | Each check against fakes: a scripted SMTP socket (banner, relay refusal, recipient accepted), the existing IMAP test double from `test_ingest_client.py`, DNS answers monkeypatched; exit-code table of D8. |
| `tests/integration/test_selfhost_probe.sh` + `.github/workflows/selfhost-probe.yml` | The container proof of §7. |
| `docs/SPEC-CLI.md` §4 | The command, its prompts (none), options, output, exit codes — written in WP-S1 from §1/§3/§5 of this plan. |
| `docs/REQUIREMENTS.md` | **F-ING-4**: "A self-hosted mirror mailbox can be prepared and verified through a CLI command that generates the mail-server configuration and checks the result over the network; the command never needs privileges and never stores a secret." |
| `docs/DECISIONS.md` | ADR-089 (§10). |
| `docs/OPERATIONS.md` §6 | "Self-hosted mirror mailbox": the five steps, what `apply.sh` changes and how to undo it, renewal, the postmaster note, how to rotate the address. |
| `README.md` | One paragraph under "The mirror mailbox": for whom, the one command, the two hard requirements (domain, reachable port 25), link to OPERATIONS.md. |
| `debian/control`, `packaging/rpm/maildigest.spec` | `Suggests: postfix, dovecot-imapd, dovecot-lmtpd, certbot, python3-dnspython` (Fedora: `dovecot`, `postfix`, `certbot`, `python3-dns` as weak `Suggests`). No `Depends`/`Requires` change. |
| `CHANGELOG.md` | "Unreleased → Added". |
| `man/maildigest.1` | Regenerated. |

Nothing in the daemon path (`runner.py`, `pipeline.py`, `ingest/`) changes. The new
modules are imported only by the CLI command.

## 7. Proof — the container probe

Templates that were never applied are guesses. The probe applies them for real, in CI,
without a public domain:

1. `debian:trixie` container, `apt install postfix dovecot-imapd dovecot-lmtpd` plus the
   freshly built `.deb` (the release workflow already builds it — the probe reuses that
   artefact, or builds it the same way on pull requests).
2. `maildigest selfhost-mail --domain mirror.test --out /tmp/sh --non-interactive`.
3. Fake the world: `mirror.test` in `/etc/hosts` → 127.0.0.1; a throw-away CA and a
   certificate for `mirror.test` in place of the Let's Encrypt paths (the templates take
   `{{cert_dir}}` for exactly this reason); the CA installed with `update-ca-certificates`
   so `create_default_context()` accepts it — the same trick the local test VM uses.
4. `sh /tmp/sh/apply.sh` with the password piped in; start Postfix and Dovecot in the
   foreground (no systemd in a container: `postfix start-fg &`, `dovecot -F &`).
5. Deliver one mail with `smtplib` to `127.0.0.1:25`, `RCPT TO` the generated address.
6. `maildigest selfhost-mail --check --wait-for-mail --timeout 60` must exit 0 and print
   the delivered mail's subject; a second run with `RCPT TO:<x@example.com>` must have been
   refused (the check asserts it).
7. Negative case: remove the `port = 0` line from the applied Dovecot file, reload, run
   `--check` again — it must FAIL on "143 closed". That is the test that the check tests
   something.

The MX step is `skipped` in the container (no resolver to fake cheaply) and asserted as
such; the DNS unit test covers the logic. The probe runs on pull requests that touch
`selfhost*` or the templates, and on every release before publishing (same gate as the
install probe of ADR-088).

Before CI ever runs it, the same seven steps are done by hand once in a throw-away
Debian 13 VM — one exists locally from the 2026-09-18 test session, with Dovecot already
present. That run is what corrects the templates; CI then keeps them corrected.

## 7a. Findings from the first run

The seven steps of §7 were run by hand in a throw-away Debian 13 VM (Postfix 3.10.13,
Dovecot 2.4.1) on 2026-09-19, and the templates came back changed. Four findings, kept
here because they are the reason the templates look the way they do — each one now has a
unit regression test, and `tests/integration/test_selfhost_probe.sh` runs the seven steps
end to end in both its modes (systemd and container):

1. **`/etc/dovecot/users` may not be root-only.** Written as `0600 root:root`, it is
   unreadable for Dovecot 2.4's unprivileged auth process, and every IMAPS login failed
   with `passwd-file: open(...) Permission denied`. `apply.sh` now writes `root:dovecot`
   mode 0640 where that group exists and keeps 0600 `root:root` only where it does not.
   SPEC-CLI.md §4 says the same.
2. **Debian's own `20-lmtp.conf` throws the domain away.** It sets
   `auth_username_format = %{user | username | lower}` inside `protocol lmtp`, so LMTP
   looked up `mirror-<hex>` instead of `mirror-<hex>@<domain>` and Postfix bounced every
   delivered mail with `550 5.1.1 User doesn't exist` — the mailbox stayed empty while
   every other check said `ok`. The drop-in now sets the format back inside
   `protocol lmtp`.
3. **A reload can succeed over a configuration nobody read.** A drop-in Dovecot cannot
   read leaves the running configuration in place while `systemctl reload` still reports
   success, and `apply.sh` printed "Done." over a setup that was never applied. It now
   runs `doveconf -n` first and stops on refusal.
4. **The recommended AAAA record was a site-local address.** `_local_address` proposed the
   VM's `fec0::/10` address, because `ipaddress` still calls that RFC-3879-abolished range
   global. Such addresses are filtered out now.

Two limits of that round, both named in §4 and in OPERATIONS.md §6: the Dovecot 2.3 lines
remain written from documentation and applied by nobody, and only Debian was exercised —
Fedora 43+ is in the stated reach but has neither a VM nor a CI job here, and the
`dovecot`-group fallback in `apply.sh` is the one place where the two could differ.

## 8. Work packages

One agent per package, `claude-opus-5`, `effort: 'medium'`, started through the Workflow
tool exactly as PLAN.md §5 prescribes; S2 ∥ S3 may run in the same workflow, everything
else in order. Each package ends with `pytest` green, `ruff check src tests` and
`mypy src` clean, the documents of §6 it owns updated, and the sentence
"Invarianten geprüft: keine Verletzung" in its report (or a justified deviation).

| WP | Deliverable | Acceptance |
|---|---|---|
| **S1 — Spec and decisions** | SPEC-CLI.md §4 entry for `selfhost-mail`; F-ING-4; ADR-089 from §10 (final wording); `debian/control` and spec `Suggests` | The spec fixes every output line and exit code of D8; a reviewer can write the tests from it alone. |
| **S2 — Generator** | `selfhost.py`, the templates, `cmd_selfhost_mail` without `--check`, `test_selfhost.py` | `selfhost-mail --domain mirror.test --non-interactive` writes the six files; the rendered files match §3 byte for byte modulo placeholders; no secret anywhere; man page regenerated. |
| **S3 — Checker** | `selfhost_check.py`, `--check`, `--wait-for-mail`, `--timeout`, `test_selfhost_check.py` | Every check has a passing and a failing unit test; exit codes per D8; the IMAPS probe goes through `ImapClient`, not a second IMAP implementation; dnspython absent → `skipped`, present → tested with a monkeypatched resolver. |
| **S4 — Probe** | The manual VM run of §7 with the template fixes it yields (fed back into S2's templates), then `test_selfhost_probe.sh` and the workflow | The seven steps of §7 pass in CI on `debian:trixie`; the negative case fails as designed. |
| **S5 — Documentation** | OPERATIONS.md §6, README paragraph, CHANGELOG, a final read-through of SPEC-CLI.md against the implemented behaviour | Someone with a VPS and a domain can go from zero to `connect-mail` using OPERATIONS.md alone; every limit of §4 appears there in plain words. |

Estimated size: S1 half a day, S2 one day, S3 one day, S4 one to two days (the VM round
is where the surprises live), S5 half a day. Four to five agent-days plus review; the
first real-world run by a person with a public domain is not in this estimate and should
happen before the feature is announced in a release.

## 9. Alternatives rejected

- **Full installer** (`selfhost-mail` runs `apt install` and edits `/etc`) — §2.
- **A system user per mailbox** (`useradd mirror`, PAM auth) — the test VM was built this
  way and it works, but the username is then not the address, the local delivery accepts
  every system user as a recipient unless further restricted, and a system account with a
  login password exists for no reason. The virtual user costs one extra file and removes
  all three.
- **Docker: recommend `docker-mailserver` and stop** — a valid answer for people who like
  containers, and OPERATIONS.md may mention it in one line. It does not give a checked
  result, needs Docker on a box that otherwise needs nothing, and a full mail server with
  Rspamd and ClamAV to receive one person's forwards is the wrong size.
- **Hand-written DNS/MX parser** instead of optional dnspython — D4.
- **STARTTLS on 143 as an option** — MailDigest connects over IMAPS only (README, ADR-021
  reasoning in providers.py); the generated server offers the same and nothing else.
- **Letting `--check` read `postconf -n` and the Dovecot files** — needs root or group
  membership, and tests the configuration instead of the behaviour. D7.

## 10. ADR-089 — draft (German, for DECISIONS.md)

```
## ADR-089: Selbst gehostetes Spiegelpostfach — erzeugen und prüfen, nicht installieren
- Status: proposed
- WP / Datum: selfhost-mail, 2026-09-18
- Kontext: Wer MailDigest ohnehin auf einem eigenen Server betreibt, möchte das
  Spiegelpostfach dort haben statt bei einem Anbieter. Ein Spiegelpostfach muss
  Weiterleitungen per SMTP aus dem Internet annehmen (Domain, MX, Port 25, Zertifikat)
  und sie per IMAPS mit prüfbarem Zertifikat bereitstellen. Ein Installer, der Postfix
  und Dovecot konfiguriert, wäre der einzige Teil des Programms mit Root-Bedarf und
  würde Systemdienste verändern, die dem Paket nicht gehören.
- Entscheidung: Ein Kommando `maildigest selfhost-mail`, das (a) aus einer Domain die
  Konfigurationsdateien für Postfix und Dovecot, eine DNS-Liste, ein Anwendeskript und
  eine Prüfliste **erzeugt** und (b) mit `--check` das Ergebnis **über das Netz prüft**
  (DNS, SMTP-Banner und Relay-Verweigerung, IMAPS mit Zertifikatskette, geschlossener
  Port 143, mit `--wait-for-mail` der Eingang einer echten Weiterleitung). Es installiert
  nichts, liest keine Systemdateien und schreibt kein Geheimnis; das Passwort fragt das
  erzeugte Skript einmal ab und legt nur den Hash ab. Das Postfach ist ein virtueller
  Dovecot-Nutzer `mirror-<hex>@Domain` mit zufälligem lokalen Teil; Postfix nimmt genau
  diese eine Adresse an und liefert per LMTP. Für die MX-Abfrage wird `dnspython`
  optional importiert; fehlt es, wird der Schritt als übersprungen gemeldet.
- Alternativen: vollständiger Installer (Root, Support-Senke, Paketpolitik);
  Systembenutzer mit PAM (Nutzername ≠ Adresse, jeder Systembenutzer wäre Empfänger);
  docker-mailserver empfehlen (kein geprüftes Ergebnis, falsche Größe); eigener
  DNS-Parser (Angriffsfläche für einen optionalen Schritt).
- Konsequenzen: MailDigest bleibt rechtelos; die Pakete bekommen nur `Suggests`. Die
  Vorlagen sind nur so gut wie ihre Probe — deshalb eine Container-Probe in CI, die die
  erzeugten Dateien wirklich anwendet, eine Mail zustellt und `--check` bestehen lässt,
  plus ein Negativfall. Getragen: Debian 13+ und Fedora 43+ (Dovecot 2.4); erreichbarer
  Port 25 und eigene Domain sind Voraussetzung und stehen so in der Prüfliste.
```

## 11. Workflow skeleton (Anhang)

Plain JavaScript for the Workflow tool, started only on the owner's say-so. `args.wp` ∈
`{all, S1, S2, S3, S4, S5}`.

```js
export const meta = {
  name: 'maildigest-selfhost-mail',
  description: 'selfhost-mail: spec, generator ∥ checker, container probe, docs (Opus 5 medium)',
  phases: [{ title: 'S1 Spec' }, { title: 'S2+S3 Code' }, { title: 'S4 Probe' }, { title: 'S5 Docs' }],
}
const M = { model: 'claude-opus-5', effort: 'medium' }
const brief = wp => `Read docs/PLAN-SELFHOST-MAIL.md fully, then PLAN.md §1–§2 and §5, docs/SECURITY.md §2.
You own work package ${wp} (§8). Deliver exactly its row of §6/§8, nothing from later packages.
Finish with pytest, ruff check src tests, mypy src all clean, and report:
{ wp, files, tests, adrs, invariants: "checked: no violation" | "<deviation and why>" }.`
const want = w => args.wp === 'all' || args.wp === w
if (want('S1')) await agent(brief('S1'), { ...M, label: 'S1', phase: 'S1 Spec' })
if (want('S2') || want('S3')) await parallel([
  () => agent(brief('S2'), { ...M, label: 'S2', phase: 'S2+S3 Code' }),
  () => agent(brief('S3'), { ...M, label: 'S3', phase: 'S2+S3 Code' }),
])
if (want('S4')) await agent(brief('S4'), { ...M, label: 'S4', phase: 'S4 Probe' })
if (want('S5')) await agent(brief('S5'), { ...M, label: 'S5', phase: 'S5 Docs' })
return { done: args.wp }
```
