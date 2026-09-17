# MailDigest

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
Mirror mailbox
      │  IMAP (read only, seen flag, optional move)
      ▼
 [1] Ingest ─────► [2] Sanitizer ────► [3] Summarizer ───► [4] Critic ──────► [5] Output ────► [6] Messenger
     code               code                LLM                 LLM               sanitizer        Telegram/
     dedupe             MIME → plain text   no privileges       no privileges     code             Discord/
                        HTML/attachments    JSON output         phishing check    final check      Signal
                        stripped                                                        │
                             ▲                                                          │
                             └── past this point the raw mail no longer exists          └── no links,
                                                                                            no markup
```

The two LLM stages are the only ones that *interpret* foreign text — and the only ones
that can do nothing: no tool, no network, no file, no access to the mailbox. Deterministic
code stands in front of them and behind them.

Details: [docs/SECURITY.md](docs/SECURITY.md). The full CLI and config contract is in
[docs/SPEC-CLI.md](docs/SPEC-CLI.md), running it as a service in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Requirements

* Python ≥ 3.11 (runs on a small VPS or a Raspberry Pi)
* a second, empty IMAP mailbox (the "mirror mailbox") — ideally with a different provider
  than your main mailbox, with its own single-purpose password or app password (which
  providers work: see [The mirror mailbox](#the-mirror-mailbox))
* **optional**: access to a language model — MailDigest runs without one as well, but then
  delivers excerpts instead of summaries (see
  [With or without a language model](#with-or-without-a-language-model))
* a Telegram bot, a Discord webhook or a running `signal-cli`

## The mirror mailbox

MailDigest **never** reads your real mailbox. It reads a second, empty mailbox that you
forward your mail to. That mailbox is pure storage: you never read it yourself, it does not
need a nice name, and a freshly created account is better than an existing one.

### Which provider?

What matters is whether IMAP can be used with a password. The table below was verified
against the real servers on 2026-09-09:

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
| **Outlook.com / Hotmail** | — | **does not work.** Microsoft has disabled password login for IMAP (the server reports `LOGINDISABLED`) and requires OAuth2, which MailDigest does not support |
| **Proton Mail** | — | **does not work.** No open IMAP; the Proton Bridge speaks unencrypted STARTTLS on a local port, MailDigest only connects over IMAPS |

An Outlook or Proton account is still not a dealbreaker: create the mirror mailbox with one
of the other providers and have Outlook or Proton **forward to it**. Your main account
stays untouched.

`maildigest connect-mail` knows this table. Enter a host and it tells you the matching
instructions; enter a provider that cannot work and it says so right away instead of
letting you run into login errors. You can also simply type the mail address there — the
host is derived from it.

### Why the account password usually is not enough

Almost all large providers reject the ordinary account password for IMAP and require a
purpose-generated **app password** — a long string that is valid for this one program only
and can be revoked individually. By far the most common reason for "login failed" even
though host, username and password appear to be correct. The username is nearly always the
**full mail address**, not just the part in front of the `@`.

## With or without a language model

MailDigest runs **without any language model out of the box**. After `maildigest init`,
`[llm] provider = "none"` is set and you can start immediately — no account, no credit
card, no API key.

**What you get in this mode:** subject (truncated with `…` beyond 100 characters), sender,
an explicitly labelled excerpt of the mail text, an equally labelled excerpt per attachment
that text could be read from (`— file.txt: Excerpt: …`), the list of blocked attachments —
and **all warnings**. Phishing protection does not depend on the language model at all:
failed SPF/DKIM checks, diverging reply addresses, punycode domains, hidden text in HTML
and stripped links are all computed by the program itself, in code. What is missing without
a model is the summarising text and the "important or not" judgement — not the protection.

**What a model adds:** real summaries instead of excerpts, an importance rating (and with
it a meaningful collected digest for unimportant mail), plus the critic that checks the
summary against the mail text.

`maildigest connect-llm` presents the modes as a list to pick from:

| Option | Cost | Effort |
|---|---|---|
| **no language model** (default) | none | none |
| **Groq**, **OpenRouter**, **Cerebras** | free tier, no credit card | sign up + paste key |
| **local model** (Ollama, LM Studio, vLLM) | none | install Ollama, download a model (a few GB) — in exchange no mail content leaves your machine |
| **Anthropic** | paid | sign up + credit |

**Response budget (`max_tokens`):** out of the box there is **no limit** — the model's own
ceiling applies. `connect-llm` asks at the end whether you want to set one and suggests
sensible values. The reason for the default: many current models are "reasoning models"
(DeepSeek, Qwen-Thinking, OpenAI o-series, Gemini-Thinking) and subtract their thinking
tokens from the response budget. With a small limit the JSON gets truncated, and instead of
a summary every mail comes back with the fail-closed note "could not be processed safely".
A limit is only worth it if you deliberately want to cap the cost per call — 4096 is a safe
value for classic models in that case.

### Why is no key included?

Because this program is open source. A bundled key would sit in the source for everyone to
read, would be scraped and revoked within days — and the bill would go to someone other
than you. Hence the honest route: without signing up anywhere it works without a model, and
whoever wants summaries connects their own (free options included) in two minutes.

## Installation

**Recommended on Debian 13+ and Kali — with `apt`.** MailDigest has its own signed package
repository, so installation *and updates* run through the tool you already use. Add the key
and the repository once:

```bash
curl -fsSL https://kpafi.github.io/maildigest/apt/maildigest-archive-keyring.gpg \
  | sudo tee /usr/share/keyrings/maildigest-archive-keyring.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/maildigest-archive-keyring.gpg] https://kpafi.github.io/maildigest/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/maildigest.list
sudo apt update && sudo apt install maildigest
```

From then on `apt upgrade` carries new versions along, and `man maildigest` works without
the copy step below. The packages are signed with the repository key; `signed-by` binds
that key to this one repository and to nothing else on your system.

**Which systems this covers.** Verified on every release by installing the package and
running it: **Debian 13 (trixie) and newer**, **Kali Rolling**, and **Ubuntu 26.04 LTS and
newer**. Older Ubuntu is not covered — 24.04 LTS ships pydantic 1.10 where the code needs
pydantic 2, and 25.04 dropped `python3-imap-tools` altogether; the package says so in its
dependencies, so apt there refuses the installation instead of creating one that cannot
start. On those, and on anything not Debian-based, use `pipx` below; it works the same and
updates with one command.

**Recommended on Fedora — with `dnf`.** The Fedora packages live in a
[COPR project](https://copr.fedorainfracloud.org/coprs/kpafi/maildigest/), enabled once:

```bash
sudo dnf copr enable kpafi/maildigest
sudo dnf install maildigest
```

`dnf upgrade` carries new versions along from then on. Built for **Fedora 43, 44 and 45**
(x86_64) and for Rawhide. `imap-tools` is missing from Fedora itself and is built as a
second package in the same COPR project, which dnf resolves on its own.

**Everywhere else — with `pipx`.** This puts the `maildigest` command on your path so it can
be called from any directory:

```bash
pipx install .
```

If `pipx` is missing, your system package manager has it: `sudo apt install pipx`
(Debian/Ubuntu/Kali), `sudo dnf install pipx` (Fedora/RHEL), on macOS `brew install pipx`.
Then run `pipx ensurepath` once and open a new terminal.

**Careful when you change the source:** `pipx install .` creates a *copy* of the package.
Changes to the source then **do not** affect the installed command — you keep working with
an old state without noticing. If you work on the project itself or want to try out
changes, install it linked instead:

```bash
pipx install --force --editable .
```

Then `maildigest` always matches the current working tree. The same command switches an
existing copy installation over.

**Alternative — in a virtual environment.** Practical for development, but the command then
only lives in `.venv/bin` and is unavailable outside it:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Without an activated venv the command is `.venv/bin/maildigest`.

**`maildigest: command not found`?** Then it was installed into a virtual environment, not
onto your path. Either run `pipx install .` after the fact or — regardless of how it was
installed — call the package directly as a module, which always works:

```bash
python3 -m maildigest --help
```

### Help and manual

Every command explains itself: `maildigest --help` shows the typical flow,
`maildigest <command> --help` shows description, options and examples. The full manual page
is available without installing anything:

```bash
maildigest --man | man -l -
```

If you want to type `man maildigest`, copy the bundled page into your own man path:

```bash
mkdir -p ~/.local/share/man/man1 && cp man/maildigest.1 ~/.local/share/man/man1/
```

The page is generated from the same source as the help output (`maildigest --man`), and a
test keeps it in sync with the program. The contract for every prompt and every output line
remains [docs/SPEC-CLI.md](docs/SPEC-CLI.md).

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

Every command asks for what it needs and explains the next step. At the end `connect-mail`
prints instructions for setting up forwarding with Gmail, posteo, mailbox.org or any other
provider.

You supply secrets either at the prompt (they then end up in `config.toml` with file mode
0600) or through environment variables — in which case the file holds nothing:

```bash
export MAILDIGEST_IMAP_PASSWORD=…
export MAILDIGEST_LLM_API_KEY=…
export MAILDIGEST_TELEGRAM_TOKEN=…
```

For cron instead of continuous operation: `maildigest run --once`.

## What a message looks like

```
📧 Heating meter reading on Thursday
From: Meier Property Management (meier-property[.]example) · 12.03. 09:14
The property management announces a reading of the radiators. Access required between 9 am and 1 pm.
```

The frame of the message (`From:`, `📎 Not processed:`, `🔍 Notes:` …) is always English;
the summary itself is written by the language model in the language from
`[general] language` (ADR-083).

The line `🔍 Notes: …` only appears when there is something to report — a failed sender
check, a punycode domain, hidden text in HTML, truncated text. Stripped links are not worth
a note: they appear as `[Link #1: example[.]com]` in place, inside the text.

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

## Triggering it from your phone (optional)

While `maildigest run` is running, it reacts to exactly two words from **your** chat:

| Command | Effect |
|---|---|
| `/digest` | Fetches now instead of waiting out the poll interval — within ten seconds at the latest |
| `/status` | Short report: folder being read, pending deliveries, collected mail |

Case does not matter, nor does surrounding whitespace; an appended `@yourbotname` (which
Telegram adds in groups) is stripped, and anything after the command is ignored — only the
first word is read.

If you run via cron with `maildigest run --once` there is no waiting time to cut short: the
commands are served there at the end of the **next** run. `/status` answers then, `/digest`
has no effect — that run has just fetched.

**What this channel explicitly cannot do:** there is no dialogue. If you write anything else
— including "summarise yesterday's mail for me" — it is discarded without being read,
answered or passed to the language model. That is deliberate: arbitrary text into a language
model whose answer then drives actions is exactly the coupling this tool avoids.

This is on by default (ADR-078). Whoever can write into that chat can trigger fetches with
it and thus cause cost at the model provider — with a private bot chat that is only you. If
`chat_id` is a **group** in which not everyone should be able to, turn it off:

```toml
[messenger.telegram]
accept_commands = false
```

**The alternative without any back channel:** run MailDigest from a systemd timer or cron
(see [docs/OPERATIONS.md](docs/OPERATIONS.md)). You then have the summaries on your phone
anyway, without anything reaching in from outside.

## Customising

In `config.toml`:

```toml
[general]
deliver_min_importance = "normal"   # low = everything individually, high = urgent only
low_digest_time = "18:00"           # when the collected digest arrives

[summarizer]
instructions = "Invoices and appointments are always important. Advertising never is."

[llm]
# max_tokens = 4096   # absent = no limit; only set it if you want to cap the cost per call
```

The custom instructions steer style, focus and importance. They cannot switch off the
security rules — the critic never even sees them.

You do not have to go hunting in the file for this. The command `maildigest instructions`
shows the current text, and this is how you change it:

```bash
maildigest instructions --add "Anything from my university is important."  # append a line
maildigest instructions --edit                                             # edit in your editor
maildigest instructions --set "Only invoices and appointments count."      # replace entirely
maildigest instructions --clear                                            # delete
```

The text takes effect from the next run; up to 2000 characters, multiple lines allowed.

## FAQ

**Why do I not get links?**
Because the link is the weapon. Phishing works by getting you to click — and in a messenger
clicking is especially easy. MailDigest replaces every link with `[Link #1: example[.]com]`
and breaks all the dots so your messenger does not linkify any of it. If you really want to
go there, you open your mailbox deliberately. If you want the full (defanged) addresses
delivered along with the message, set `[links] footnote = true` — they are then appended as
a list at the bottom, with broken dots as well.

**Why is my `.docx` invoice not summarised?**
Because MailDigest only opens what it can open safely. Only plain text files and PDFs are
processed for content — and those only if the first bytes of the file match the declared
file type and extraction runs through in a sandboxed subprocess. Office files can execute
macros, archives smuggle content past filters, `.html` attachments are an attack path of
their own. All of those show up as a line "📎 Not processed: invoice[.]docx (34 KB)" — so
you know it is there and decide for yourself.

**What about phishing as an image?**
That is the best-known gap: MailDigest does not read image content (no OCR). An attacker can
send their text as a screenshot; the summary then says something like "mail without text
with one image attachment". That is conspicuous, but it is not protection — in that case you
have to make the judgement yourself. Encrypted mail (PGP/S-MIME) is likewise not decrypted
and therefore not summarised — the message says so explicitly ("encrypted (PGP/S-MIME) —
content not readable by design").

**Can the AI be taken over by a mail?**
It cannot take over anything, because it is allowed nothing: no tool, no network, no file. A
successful prompt injection can at most produce a wrong summary — against which a second
instance checks with its own prompt and with facts computed in code (sender domains,
authentication results, suspicious attachments), and the suspicion shows up as a note line
in your message.

**Where is my data?**
The mail text only exists in memory while the mail is being processed. The SQLite file holds
state and metadata, plus two exceptions containing already checked, defanged text: the lines
for the collected digest and a delivery not yet confirmed; both are deleted after sending.
The logs contain no mail content — unless you set `log_level = "DEBUG"`, in which case
tracebacks may contain content. The sanitised mail text goes to the language model; choose
your provider accordingly or use a local model.

**Why does a mail arrive twice?**
Because in case of doubt MailDigest would rather deliver twice than lose something: the
state "checked" is stored before sending. If the process crashes exactly in between, the
same message can arrive a second time. For long summaries split across several messages this
only affects the one part whose confirmation did not arrive — the parts before it are not
sent again.

**Does the warning also appear when the language model slacks off?**
Yes. The note "mail contained instructions to the AI (ignored)" no longer hangs on the
model's judgement alone: forged program markers, conspicuously many invisible characters and
literal instructions to a language model are detected by MailDigest itself, in code, before
any model is asked. The same goes for the phishing warning — when several independent
forgery signals coincide, the program sets the warning even against a silent model.

**What happens in the mirror mailbox?**
What is unread gets read; afterwards the mail is marked as seen and — if you set
`move_processed_to` — moved into the given folder (for which your server needs the MOVE
extension; all common ones have it — otherwise the mail stays put as read and you get a note
in the log). Nothing is ever deleted; there is no code path for it — not even an `EXPUNGE`,
which would carry out other programs' delete flags.

## Limitations of this version

What follows is not a list of minor details but the list of places where you should **not**
rely on MailDigest.

**Barely any field experience.** Version 0.2.0 ran against real counterparts for the first
time in September 2026: a mirror mailbox at web.de, a model via OpenRouter, a Telegram bot.
That is one environment, one user, a few days. Everything else is tested against mocks (1800+
tests, an attack corpus, two documented test rounds with black-box testers). Whether your
IMAP server, your model and your messenger behave like the ones tested is unproven. Expect
surprises on the first run and start with `maildigest test --dry-run`.

**Open findings.** The second test round (docs/TESTRUNDE-2.md, German, black-box) and the
acceptance review (docs/ABNAHME-FIXRUNDE.md, German) closed the severe findings. What is
open is listed with severity and fix direction in docs/TESTING.md §7 — among them a mail
made of millions of empty MIME parts that extends a fetch cycle by about half a minute, and
the follow-up fix packages NF-2 to NF-7 from the acceptance review.

**Image phishing remains open.** No OCR, no image analysis. Whoever sends their text as a
screenshot gets a summary like "mail without text with one image attachment" — conspicuous,
but not a check.

**Encrypted mail is not read.** PGP and S/MIME are not decrypted. You still get a regular
message — header, sender, the list of unprocessed parts (`📎 Not processed: …`) and the note
line `🔍 Notes: encrypted (PGP/S-MIME) — content not readable by design`. To read it you have
to go into the real mailbox. Signed but unencrypted mail is unaffected.

**Signal only as a note to self.** The Signal adapter writes into "Note to Self" and requires
a running `signal-cli --daemon`. Other recipients are not supported.

**New warning heuristics are uncalibrated.** The detection of AI instructions in the mail
text, the threshold for "HTML part differs from text part" and the rule for when several
forgery signals add up to a phishing warning are tuned against test mail, not against your
inbox. Too many warnings are more likely than too few — and a warning that always fires is
no warning at all.

**Moving can only fail in production.** `connect-mail` does not check in advance whether
your server supports the MOVE extension and whether the target folder exists. Both only show
up on the first run; nothing is lost in the process — the mail stays put as read.

**One mailbox, one process, almost no back channel.** No multi-mailbox operation, no access
to your real mailbox, no dialogue with the bot — the only exception is the fixed command list
`/digest` and `/status` from your chat, see
[Triggering it from your phone](#triggering-it-from-your-phone-optional).

Two deliberate quirks that can look like bugs:

* Delivered text carries **no formatting**: bullet lists appear as `•`, headings and italics
  disappear. Formatting in the name of a sender is a trust signal, and MailDigest leaves that
  to no one.
* For mail with both a text **and** an HTML version, MailDigest summarises the text version —
  your mail program shows you the HTML version. If the two differ noticeably, that appears as
  a note in the message; MailDigest cannot compare them by content.

## Documentation

In short: the README explains the tool, `docs/` explains the program.

| File | Content |
|---|---|
| [docs/SPEC-CLI.md](docs/SPEC-CLI.md) | The contract: every command, every prompt, every output line, every config field, all exit codes |
| [docs/SECURITY.md](docs/SECURITY.md) | Attacker model, sanitizer rules, prompt hardening, invariants I1–I8 including review |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data model, pipeline contract, error and retry policy |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | systemd unit, cron, maintenance, log events |
| [docs/TESTING.md](docs/TESTING.md) | Test protocol and all findings logs, including the open findings (§7) |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Requirements with status |
| [docs/DECISIONS.md](docs/DECISIONS.md) | All design decisions as ADRs (ADR-001 to ADR-088) — *German* |
| docs/TESTRUNDE-*.md, docs/ABNAHME-FIXRUNDE.md | Records of the test rounds and the acceptance review, unchanged — *German* |
| [docs/PLAN-PACKAGING.md](docs/PLAN-PACKAGING.md) | How MailDigest reaches apt and dnf: repository layout, signature, release flow, and the steps only the copyright holder can take |
| [PLAN.md](PLAN.md), [docs/PLAN-FIXRUNDE.md](docs/PLAN-FIXRUNDE.md) | How the project came about: work packages and the fix round, worked through by AI agents and decided by humans — *German* |

The documents marked *German* are historical records of how the project was built and
decided; they are kept in their original language rather than retranslated after the fact.

## Licence and status

Version 0.2.1 (see [CHANGELOG.md](CHANGELOG.md)) — a distribution release on top of 0.2.0,
the first public one: same program, now installable and updatable through `apt`. Still
little field experience and an honest list of open points (above and in docs/TESTING.md §7). The
binding requirements are in [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), the security model
including the invariant review in [docs/SECURITY.md](docs/SECURITY.md). Please report bugs
and findings as a GitHub issue with reproduction steps, ideally in the format from
docs/TESTING.md §3.

Licence: MIT, see [LICENSE](LICENSE).
