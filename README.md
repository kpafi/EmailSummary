# MailDigest

[![CI](https://github.com/kpafi/maildigest/actions/workflows/ci.yml/badge.svg)](https://github.com/kpafi/maildigest/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/kpafi/maildigest)](https://github.com/kpafi/maildigest/releases)
[![License: MIT](https://img.shields.io/github/license/kpafi/maildigest)](LICENSE)

MailDigest reads a **mirror mailbox** that you forward your mail to, summarises every
message with a language model, has a second, independent instance check it for phishing,
and sends you the result as **plain text** on Telegram, Discord or Signal. You see what is
going on in your inbox without opening your inbox — and without anything clickable ever
reaching you.

```
Real mailbox ──forwarding──► Mirror mailbox ──► MailDigest ──► Messenger
```

## The security model in five sentences

1. MailDigest never gets access to your real mailbox, only to a dedicated mirror
   mailbox — and it deletes nothing there.
2. Before a language model sees anything, deterministic code reduces the mail to plain
   text: no HTML, no MIME parts, no attachment binaries.
3. The language models have no tools, no network access and no file access — the only
   thing they can produce is text in a validated JSON field.
4. What reaches you has been checked by code one last time: no clickable links, no
   attachments, no markup — domains appear defanged as `example[.]com`.
5. If anything goes wrong, you get a note ("This mail could not be processed safely — no
   content delivered.") instead of unchecked content — nothing disappears silently.

The path a mail takes — each stage only sees what the previous one let through:

```
[1] Ingest        code   IMAP read-only, seen flag, dedupe
[2] Sanitizer     code   MIME → plain text; HTML, links and attachments stripped
[3] Summarizer    LLM    no tools, no network, no files; JSON output only
[4] Critic        LLM    second instance with its own prompt: phishing check
[5] Output check  code   no links, no markup, domains defanged
[6] Messenger            Telegram, Discord or Signal
```

The two LLM stages are the only ones that *interpret* foreign text — and the only ones
that can do nothing. Deterministic code stands in front of them and behind them.
Details: [docs/SECURITY.md](docs/SECURITY.md).

## Installation

You need Python 3.11 or newer (a small VPS or a Raspberry Pi is enough), a second, empty
IMAP mailbox as the mirror, and a Telegram bot, a Discord webhook or a running
`signal-cli`. A language model is optional.

**Debian 13+, Kali and Ubuntu 26.04+ — with `apt`.** MailDigest has its own signed
package repository, so installation *and updates* run through the tool you already use:

```bash
curl -fsSL https://kpafi.github.io/maildigest/apt/maildigest-archive-keyring.gpg \
  | sudo tee /usr/share/keyrings/maildigest-archive-keyring.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/maildigest-archive-keyring.gpg] https://kpafi.github.io/maildigest/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/maildigest.list
sudo apt update && sudo apt install maildigest
```

**Fedora 43+ — with `dnf`**, from a
[COPR project](https://copr.fedorainfracloud.org/coprs/kpafi/maildigest/):

```bash
sudo dnf copr enable kpafi/maildigest
sudo dnf install maildigest
```

**Everywhere else — with `pipx`:**

```bash
pipx install git+https://github.com/kpafi/maildigest
```

<details>
<summary>Which systems the packages cover, and the other ways to install</summary>

**Coverage.** Every release is verified by installing the package and running it on
Debian 13 (trixie), Kali Rolling, Ubuntu 26.04 LTS, Fedora 43, 44, 45 and Rawhide. Older
Ubuntu is not covered: 24.04 LTS ships pydantic 1.10 where the code needs pydantic 2, and
25.04 dropped `python3-imap-tools`. The package says so in its dependencies, so apt there
refuses the installation instead of creating one that cannot start — use `pipx` on those.
`signed-by` binds the repository key to this one repository and to nothing else on your
system. On Fedora, `imap-tools` is built as a second package in the same COPR project.

**`pipx` missing?** `sudo apt install pipx`, `sudo dnf install pipx` or `brew install pipx`,
then `pipx ensurepath` once and open a new terminal.

**From a clone.** `pipx install .` creates a *copy*; changes to the source do not reach the
installed command. If you work on the project, install it linked instead — the same command
switches an existing copy over:

```bash
pipx install --force --editable .
```

**In a virtual environment**, practical for development; the command then only lives in
`.venv/bin`:

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

**`maildigest: command not found`?** Then it went into a virtual environment, not onto your
path. Either `pipx install .` after the fact, or call the module directly, which always
works: `python3 -m maildigest --help`.

**Manual page.** The packages install it; `man maildigest` works. With `pipx`, either read
it with `maildigest --man | man -l -` or copy it into your own man path:

```bash
mkdir -p ~/.local/share/man/man1 && cp man/maildigest.1 ~/.local/share/man/man1/
```

</details>

## Quickstart

```bash
maildigest init                # create the configuration (file gets mode 0600)
maildigest connect-mail        # mirror mailbox: test access, choose folder
maildigest connect-llm         # OPTIONAL: connect a language model (free ones too)
maildigest connect-messenger   # Telegram/Discord/Signal, test message
maildigest test                # end-to-end self-test with a sample mail
maildigest instructions --edit # what matters to you (changeable at any time)
maildigest run                 # continuous operation (Ctrl-C exits cleanly)
```

Every command asks for what it needs and explains the next step; `maildigest --help` shows
the flow, `maildigest <command> --help` the options. At the end, `connect-mail` prints the
instructions for setting up forwarding with Gmail, Posteo, mailbox.org or any other
provider.

Secrets go either in at the prompt (they then live in `config.toml`, mode 0600) or through
environment variables, in which case the file holds nothing:

```bash
export MAILDIGEST_IMAP_PASSWORD=…
export MAILDIGEST_LLM_API_KEY=…
export MAILDIGEST_TELEGRAM_TOKEN=…
```

For cron instead of continuous operation: `maildigest run --once`. Running it as a
service: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## What a message looks like

```
📧 Heating meter reading on Thursday
From: Meier Property Management (meier-property[.]example) · 12.03. 09:14
The property management announces a reading of the radiators. Access required between 9 am and 1 pm.
```

The frame of the message (`From:`, `📎 Not processed:`, `🔍 Notes:` …) is always English;
the summary itself is written in the language from `[general] language`. The line
`🔍 Notes: …` only appears when there is something to report — a failed sender check, a
punycode domain, hidden text in HTML. Stripped links appear in place as
`[Link #1: example[.]com]`.

Unimportant mail does not arrive one by one but collected once a day:

```
🗂 12 low-priority mails: 8 newsletter, 3 notification, 1 other
```

And when something smells wrong:

```
⚠️ SUSPECTED PHISHING: sender domain does not match the claimed sender, reply address differs
📧 Urgent payment request [important]
From: Boss (mail-security[.]example) · 12.03. 03:41
Alleged payment request from the boss, transfer to be made today.
🔍 Notes: reply address differs from the sender
```

## The mirror mailbox

MailDigest **never** reads your real mailbox. It reads a second, empty mailbox that you
forward your mail to — pure storage that you never read yourself, ideally with a different
provider than your main mailbox and with its own single-purpose password. What matters is
that IMAP can be used with a password; `maildigest connect-mail` knows which providers
can and tells you the matching instructions when you enter a host or mail address.

<details>
<summary>Which provider? The table, verified against the real servers on 2026-09-09</summary>

| Provider | IMAP host | Effort |
|---|---|---|
| **Posteo** | `posteo.de` | ~€1/month, **the easiest** — IMAP open out of the box, account password is enough |
| **mailbox.org** | `imap.mailbox.org` | ~€1/month, just as straightforward |
| GMX | `imap.gmx.net` | free, but IMAP has to be enabled in the settings first |
| WEB.DE | `imap.web.de` | same as GMX (same company) |
| Gmail | `imap.gmail.com` | app password required, which in turn requires two-factor authentication |
| iCloud | `imap.mail.me.com` | app-specific password, two-factor authentication mandatory |
| Yahoo | `imap.mail.yahoo.com` | app password required |
| Telekom/T-Online | `secureimap.t-online.de` | needs a separate "password for email programs" |
| IONOS/1&1 | `imap.ionos.de` | mailbox password is enough |
| **Outlook.com / Hotmail** | — | **does not work.** Microsoft requires OAuth2 for IMAP, which MailDigest does not support |
| **Proton Mail** | — | **does not work.** No open IMAP; the Proton Bridge speaks unencrypted STARTTLS, MailDigest only connects over IMAPS |

An Outlook or Proton account is still not a dealbreaker: create the mirror mailbox with one
of the other providers and have Outlook or Proton **forward to it**.

**"Login failed" although everything looks right?** Almost all large providers reject the
ordinary account password for IMAP and require an **app password** — a long string valid
for this one program only. And the username is nearly always the **full mail address**, not
just the part in front of the `@`.

</details>

**Hosting it yourself** — *not yet in a release; on `main` only.* If you run MailDigest on
a server of your own, `maildigest selfhost-mail --domain mirror.example.org` writes the
Postfix and Dovecot configuration, the DNS records, an apply script and a checklist; you
run the privileged steps yourself with `sudo`, and `--check --wait-for-mail` then proves
the whole chain down to a real forwarded mail arriving. It needs a domain of your own and
port 25 reachable from the internet, which rules out nearly every home connection. Step by
step: [docs/OPERATIONS.md §6](docs/OPERATIONS.md#6-self-hosted-mirror-mailbox). If that
sounds like more than you want to own, the €1/month providers in the table are the better
answer.

## With or without a language model

MailDigest runs **without any language model out of the box**: after `maildigest init`,
`[llm] provider = "none"` is set — no account, no credit card, no API key. In this mode you
get subject, sender, a labelled excerpt of the mail text and of every readable attachment,
the list of blocked attachments — and **all warnings**. Phishing protection does not depend
on the model: failed SPF/DKIM checks, diverging reply addresses, punycode domains, hidden
text in HTML and stripped links are all computed in code. What a model adds is real
summaries instead of excerpts, an importance rating (and with it a meaningful collected
digest), and the critic that checks the summary against the mail text.

`maildigest connect-llm` presents the options as a list to pick from:

| Option | Cost | Effort |
|---|---|---|
| **no language model** (default) | none | none |
| **Groq**, **OpenRouter**, **Cerebras** | free tier, no credit card | sign up + paste key |
| **local model** (Ollama, LM Studio, vLLM) | none | install Ollama, download a model (a few GB) — in exchange no mail content leaves your machine |
| **Anthropic** | paid | sign up + credit |

## Triggering it from your phone (optional)

While `maildigest run` is running, it reacts to exactly two words from **your** chat:

| Command | Effect |
|---|---|
| `/digest` | Fetches now instead of waiting out the poll interval — within ten seconds at the latest |
| `/status` | Short report: folder being read, pending deliveries, collected mail |

Anything else — including "summarise yesterday's mail for me" — is discarded without being
read, answered or passed to the language model. That is deliberate: arbitrary text into a
language model whose answer then drives actions is exactly the coupling this tool avoids.
With `run --once` under cron the commands are served at the end of the next run.

Whoever can write into that chat can trigger fetches and thus cause cost at the model
provider. With a private bot chat that is only you; if `chat_id` is a group, turn it off
with `accept_commands = false` under `[messenger.telegram]`. The alternative without any
back channel is a systemd timer or cron.

## Customising

In `config.toml`:

```toml
[general]
deliver_min_importance = "normal"   # low = everything individually, high = urgent only
low_digest_time = "18:00"           # when the collected digest arrives

[summarizer]
instructions = "Invoices and appointments are always important. Advertising never is."
```

The instructions steer style, focus and importance. They cannot switch off the security
rules — the critic never even sees them. You do not have to edit the file for this:

```bash
maildigest instructions                                                    # show
maildigest instructions --add "Anything from my university is important."  # append a line
maildigest instructions --edit                                             # edit in your editor
maildigest instructions --set "Only invoices and appointments count."      # replace entirely
maildigest instructions --clear                                            # delete
```

Up to 2000 characters, multiple lines allowed; takes effect from the next run.

## FAQ

**Why do I not get links?**
Because the link is the weapon. Phishing works by getting you to click, and in a messenger
clicking is especially easy. MailDigest replaces every link with `[Link #1: example[.]com]`
and breaks all the dots so your messenger does not linkify any of it. If you want the full
(defanged) addresses appended as a list, set `[links] footnote = true`.

**Why is my `.docx` invoice not summarised?**
Because MailDigest only opens what it can open safely: plain text files and PDFs, and those
only if the first bytes match the declared type and extraction runs through in a sandboxed
subprocess. Office files can execute macros, archives smuggle content past filters, `.html`
attachments are an attack path of their own. All of those show up as a line
"📎 Not processed: invoice[.]docx (34 KB)" — so you know it is there and decide yourself.

**What about phishing as an image?**
That is the best-known gap: no OCR, no image analysis. A screenshot with text yields a
summary like "mail without text with one image attachment" — conspicuous, but not
protection. Encrypted mail (PGP/S-MIME) is likewise not decrypted; the message says so.

**Can the AI be taken over by a mail?**
It cannot take over anything, because it is allowed nothing: no tool, no network, no file.
A successful prompt injection can at most produce a wrong summary — against which a second
instance checks with its own prompt and with facts computed in code, and the suspicion
shows up as a note line in your message. The note "mail contained instructions to the AI
(ignored)" does not hang on the model alone either: forged program markers, masses of
invisible characters and literal instructions to a language model are detected in code
before any model is asked.

**Where is my data?**
The mail text only exists in memory while the mail is being processed. The SQLite file
holds state and metadata, plus two short-lived exceptions of already checked, defanged
text: the lines for the collected digest and a delivery not yet confirmed. The logs contain
no mail content unless you set `log_level = "DEBUG"`. The sanitised mail text goes to the
language model — choose your provider accordingly or use a local model.

**Why does a mail arrive twice?**
Because in case of doubt MailDigest would rather deliver twice than lose something: the
state "checked" is stored before sending. If the process crashes exactly in between, the
same message can arrive a second time.

**Every mail comes back as "could not be processed safely"?**
Most likely a `max_tokens` limit that is too small. Many current models are reasoning
models and subtract their thinking tokens from the response budget; with a small limit the
JSON gets truncated and MailDigest fails closed. Out of the box there is no limit — only
set one if you deliberately want to cap the cost per call, then 4096 is safe for classic
models.

**Why is no API key included?**
Because this program is open source. A bundled key would be scraped and revoked within
days, and the bill would go to someone else. Without signing up anywhere it works without a
model; whoever wants summaries connects their own, free options included.

**What happens in the mirror mailbox?**
Unread mail gets read, marked as seen and, if you set `move_processed_to`, moved into that
folder (your server needs the MOVE extension; otherwise the mail stays put as read with a
note in the log). Nothing is ever deleted; there is no code path for it — not even an
`EXPUNGE`.

## Limitations of this version

The places where you should **not** rely on MailDigest:

* **Barely any field experience.** Version 0.2.0 ran against real counterparts for the
  first time in September 2026: one mirror mailbox at web.de, one model via OpenRouter, one
  Telegram bot — one environment, one user, a few days. Everything else is tested against
  mocks (2000+ tests, an attack corpus, two documented test rounds with black-box testers).
  Expect surprises on the first run and start with `maildigest test --dry-run`.
* **Open findings** are listed with severity and fix direction in docs/TESTING.md §7 —
  among them a mail made of millions of empty MIME parts that extends a fetch cycle by
  about half a minute.
* **Image phishing remains open**, see the FAQ. **Encrypted mail is not read**; you still
  get header, sender and the note line, and go into the real mailbox for the rest.
* **Signal only as a note to self.** The adapter writes into "Note to Self" and requires a
  running `signal-cli --daemon`.
* **New warning heuristics are uncalibrated**: the detection of AI instructions, the
  threshold for "HTML part differs from text part" and the rule for when several forgery
  signals add up to a phishing warning are tuned against test mail, not against your
  inbox. Too many warnings are more likely than too few.
* **Moving can only fail in production.** `connect-mail` does not check whether your
  server supports MOVE and whether the target folder exists; nothing is lost, the mail stays
  put as read.
* **One mailbox, one process, no dialogue.** No multi-mailbox operation, no access to your
  real mailbox, no chat with the bot beyond `/digest` and `/status`.

Two deliberate quirks that can look like bugs: delivered text carries **no formatting**
(bullets become `•`, headings and italics disappear — formatting in a sender's name is a
trust signal, and MailDigest leaves that to no one), and for mail with both a text **and**
an HTML version, MailDigest summarises the text version while your mail program shows you
the HTML one. If the two differ noticeably, that appears as a note.

## Documentation

In short: the README explains the tool, `docs/` explains the program.

| File | Content |
|---|---|
| [docs/SPEC-CLI.md](docs/SPEC-CLI.md) | The contract: every command, every prompt, every output line, every config field, all exit codes |
| [docs/SECURITY.md](docs/SECURITY.md) | Attacker model, sanitizer rules, prompt hardening, invariants I1–I8 including review |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data model, pipeline contract, error and retry policy |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | systemd unit, cron, maintenance, log events, the self-hosted mirror mailbox (§6) |
| [docs/TESTING.md](docs/TESTING.md) | Test protocol and all findings logs, including the open findings (§7) |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Requirements with status |
| [docs/DECISIONS.md](docs/DECISIONS.md) | All design decisions as ADRs (ADR-001 to ADR-089) — *German* |
| docs/TESTRUNDE-*.md, docs/ABNAHME-FIXRUNDE.md | Records of the test rounds and the acceptance review — *German* |
| [docs/PLAN-PACKAGING.md](docs/PLAN-PACKAGING.md) | How MailDigest reaches apt and dnf: repository layout, signature, release flow |
| [docs/PLAN-SELFHOST-MAIL.md](docs/PLAN-SELFHOST-MAIL.md) | The self-hosted mirror mailbox: why the command generates and checks instead of installing |
| [PLAN.md](PLAN.md), [docs/PLAN-FIXRUNDE.md](docs/PLAN-FIXRUNDE.md) | How the project came about: work packages and the fix round, worked through by AI agents and decided by humans — *German* |

The documents marked *German* are historical records of how the project was built and
decided; they are kept in their original language.

## Licence and status

Version 0.2.1 (see [CHANGELOG.md](CHANGELOG.md)): the first public program, 0.2.0, now
installable and updatable through `apt` and `dnf`. Still little field experience and an
honest list of open points, above and in docs/TESTING.md §7. Please report bugs and
findings as a GitHub issue with reproduction steps, ideally in the format from
docs/TESTING.md §3.

Licence: MIT, see [LICENSE](LICENSE).
