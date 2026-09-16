# MailDigest — CLI and configuration specification

> **This document is the contract.** Everything written here the program must do exactly so;
> what is not written here does not exist for the cold tester (docs/TESTING.md §3).
> As of WP9. Changes only together with the code and with an ADR in docs/DECISIONS.md.

## 1. Installation and invocation

MailDigest needs Python ≥ 3.11. The recommended installation is `pipx install .` — only that
puts the `maildigest` command on the path. An installation into a virtual environment
(`pip install -e .`) instead places it only under `.venv/bin/maildigest`; without an activated
venv the shell then reports `command not found`. The module invocation works in both cases.

After installing the package there are two equivalent invocation forms:

```
maildigest <COMMAND> [OPTIONS]
python -m maildigest <COMMAND> [OPTIONS]
```

Seven commands: `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test`, `run`,
`instructions`.

An invocation without a command prints the help on **stdout** and exits with code 2.
`maildigest --help` and `maildigest <COMMAND> --help` print help and exit with code 0. The
help of every command contains a description, the options and examples (ADR-087).
`maildigest --man` prints the complete manual page in troff format on stdout (exit code 0),
readable with `maildigest --man | man -l -`; the file `man/maildigest.1` in the repository is
generated from it and can be copied to `~/.local/share/man/man1/`, after which
`man maildigest` suffices.

## 2. Exit codes

| Code | Meaning |
|------|-----------|
| 0 | Success |
| 1 | Runtime or configuration error (connection failed, file missing, configuration invalid, self-test fail-closed) |
| 2 | Usage error (unknown command, unknown option, disallowed option value — including a number outside the permitted range, e.g. `--port 0` / `--port 99999` —, a missing mandatory value in non-interactive mode, too many invalid entries, aborted input/EOF on stdin) |

A value that is a number within the permitted range but is invalid as **configuration**
remains exit code 1 — for instance `--port 143` (cleartext IMAP) or
`--low-digest-time 25:99` (ADR-069).

Every error message goes to **stderr** and starts with `Error: `. Progress and result
messages go to **stdout**. Warnings (notices that do not abort the flow) go to stderr,
without the `Error: ` prefix.

**Language of the output:** every text the program prints — prompts, step and summary lines,
error and warning messages as well as the whole frame of every delivered message — is
**English**, independent of any setting and any locale. `[general] language` steers only the
language of the text fields produced by the language model (summary, headline, reasons,
category); without a language model those fields do not exist. This document is therefore the
literal contract of the **English** output (ADR-083).

A `SIGINT` (Ctrl-C) during a prompt ends the command with `Error: Aborted.` on stderr and
**exit code 1**; nothing is written.

In addition there is the **log** of the inner layers (for instance a retry of a model call).
It always consists of JSON lines in the format from §4 `run`. With `run` they go to
**stdout** — that is where service operation reads along. With every other command they go to
**stderr** from level `WARNING` upwards, so that stdout contains only the output specified in
§4.

Error messages **never** contain passwords, API keys, bot tokens or webhook URLs. If a
counterpart quotes the API key that was sent in its error response, it is replaced by `***`
before output (also as a prefix from eight characters onwards) — the promise does not depend
on the provider's good behaviour.

Every error line passes through a **character allowlist** before output (printable ASCII,
umlauts/ß, the usual punctuation; everything else becomes `·`, ADR-055). That way a text
originating from a counterpart — the model provider's error text, the IMAP server's folder
name — cannot bring an ANSI/control sequence onto the terminal and set the screen, colour,
window title or an invented program message there.

## 3. Global options

These options apply to every command and may appear **before or after** the command name
(`maildigest --config x init` and `maildigest init --config x` are equivalent).

| Option | Value | Meaning |
|---|---|---|
| `--config` | PATH | Path of the configuration file. Without it the environment variable `MAILDIGEST_CONFIG` applies, otherwise `config.toml` in the current directory. |
| `--non-interactive` | – | Asks nothing. The command-line options, the file's existing values and the defaults apply. If a mandatory value is then missing, the command exits with code 2 and names the responsible option. |
| `--help` | – | Show help and exit with code 0. |
| `--man` | – | Print the manual page (troff, section 1) on stdout and exit with code 0. Only meaningful without a command name; a command after it is not executed. |

## 4. Commands

### `maildigest init`

Creates a new configuration file. If the file already exists, the command aborts with exit
code 1 and changes nothing (unless `--force` is given).

Interactive prompts in this order; in brackets the default that applies on empty input:

1. `Language of the summaries (de/en) [de]: `
2. `Length of the summaries (short/medium/long) [medium]: `
3. `Deliver individually from importance (low/normal/high) [normal]: `
4. `Time of the daily digest (HH:MM) [18:00]: `
5. `Custom-Instructions: ` (one line; empty = none; truncated to 2000 characters); in
   interactive mode it is preceded by the explanation
   `Custom instructions: one line about what matters to you (leave empty for none).`

A disallowed entry at 1–3 is rejected (`Invalid value. Allowed: …` on stderr) and asked
again; after three failed attempts the command ends with
`Error: Too many invalid entries — aborted.` and exit code 2.

Afterwards the file is created with **file mode 0600** and the complete field set from
section 5: every field with a default is present with that default. Mandatory fields without
a default (`[imap] host`, `[imap] username`), all secrets and all remaining fields without a
default — which includes all four overrides in `[llm.critic]` — are present as commented-out
example lines; after `init` the file is therefore valid TOML but not yet a complete
configuration. `[llm] model` is **not** among them: per section 5 it has the default `""` and
only becomes mandatory once `provider` is not `none` (ADR-076). The explanatory line for the
custom instructions appears in interactive mode only.

The first line is `Setting up MailDigest — configuration: <path>`, and after writing
`Configuration created: <path> (file mode 0600).` follows. After that comes a paragraph
placing what MailDigest delivers without a language model; **at the end** stands the list of
next steps. The output ends with that list:

```
Next steps:
  1) maildigest connect-mail        (mirror mailbox)
  2) maildigest connect-messenger   (Telegram/Discord/Signal)
  3) maildigest connect-llm         (optional: real summaries)
  4) maildigest test                (self-test)
  5) maildigest run                 (continuous operation)
```

An invalid value (e.g. `--low-digest-time 25:99`) leads to exit code 1 with a field-specific
message; the file is then **not** created.

**Options**

| Option | Value | Meaning |
|---|---|---|
| `--force` | – | Overwrite an existing file (the previous content is lost) |
| `--language` | `de` \| `en` | Answer to question 1 |
| `--summary-length` | `short` \| `medium` \| `long` | Answer to question 2 |
| `--min-importance` | `low` \| `normal` \| `high` | Answer to question 3 |
| `--low-digest-time` | HH:MM | Answer to question 4 |
| `--instructions` | TEXT | Answer to question 5 |

### `maildigest connect-mail`

Enters the mirror mailbox credentials, tests the connection, lets you choose the folder and
finally prints the instructions for setting up forwarding in the real mailbox. Requires an
existing configuration file (otherwise exit code 1 with a pointer to `maildigest init`).

Before the first question the command prints an explanation of what an IMAP host is, along
with an example list of the large providers. If no host is stored in the configuration yet
and the command runs interactively, that is preceded by a recommendation of which provider a
mirror mailbox can be created with for the least effort.

**Provider detection.** After question 1 the entered value is checked against a built-in
provider list (data as of 2026-09-09):

* A **mail address or bare domain** of a known provider is translated into that provider's
  IMAP host; the translation is shown as the line `  -> IMAP host for <provider>: <host>`. An
  unknown value stays unchanged — nothing is guessed.
* For a **recognised provider** instructions follow: what kind of password the server
  requires (account or app password), the necessary steps, a direct link where applicable,
  and a note on the most common pitfall.
* For a provider where password login is **disabled** server-side — currently
  Outlook.com/Hotmail/Live (OAuth2 mandatory, `LOGINDISABLED`) and Proton Mail (no open
  IMAP) — the command ends with exit code 2 **before** the password question, naming the
  reason and the way out (create the mirror mailbox elsewhere and forward to it). Nothing is
  saved. This applies to the bare domain **and** to a mail address of that provider
  (`me@outlook.com`) alike, including together with `--no-test`.

The default for question 2 is the port of the recognised provider (993 for all known
providers), provided the configuration does not contain a port yet. Before question 3 an
example is printed showing the full mail address as the username; before question 4, with a
recognised provider, it says what kind of password is expected.

Prompts in this order:

1. `IMAP host [<previous value>]: ` — mandatory
2. `Port [993]: ` — an integer from 1 to 65535
3. `Username [<previous value>]: ` — mandatory
4. `Password (leave empty to use MAILDIGEST_IMAP_PASSWORD instead): ` — without screen echo,
   provided a terminal is present. If the environment variable `MAILDIGEST_IMAP_PASSWORD` is
   set, this question is skipped; **no** password is then written into the file. If there is
   neither an entered nor a stored nor an environment password, the command ends with exit
   code 2.
5. After a successful connection: the line `Which folder should MailDigest read?`, the
   server's numbered folder list, and `Folder [<number of the current folder>]: `

Port 143 (cleartext IMAP) is always rejected (exit code 1). If MailDigest does not connect,
the command ends with exit code 1 and writes **nothing** into the file. The message names
host, port, folder and the **error class**; the server's response text is deliberately **not**
printed — it can quote the username that was sent (I5, docs/SECURITY.md §6). After that comes
an explanation matching the error class:

* **Login error** (the server rejected the credentials): the provider-specific hint on what
  logins typically fail on — usually a separately generated app password (a generic hint with
  an unknown provider).
* **Transport error** (connection refused, timeout, TLS): the hint that no login was even
  attempted and that host, port and network connection should be checked.

At the end, in both cases, stands the pointer to `--no-test`
(`With --no-test the values can also be saved without testing them.`). If the folder list
cannot be fetched, it stays at the warning
`Folder list unavailable (<class>); keeping the current setting.` on stderr and at the
previous folder. If the configured folder does not exist, stderr shows
`The configured folder "<name>" does not exist on the server.`; when the command runs
non-interactively, the numbered folder list is then printed on stdout and stderr afterwards
points to it (`Keeping it unchanged — pick one of the folders listed on stdout with --folder,
otherwise the next run will fail.`); the configured folder stays unchanged.

Further literal lines of this command on stdout:
`Connecting the mirror mailbox (IMAPS only, certificate check always on)`,
`Detected: <provider>`, `Username = <example>`, `Password = <kind of password>.`,
`Password: from MAILDIGEST_IMAP_PASSWORD (not written to the file)`, `Connecting ...`,
`Connection established.`, `Connection test skipped (--no-test).` and
`Saved to <path> (file mode 0600).`

Saving only happens after the test. The file keeps mode 0600.

**Options**

| Option | Value | Meaning |
|---|---|---|
| `--host` | HOST | Answer to question 1 |
| `--port` | PORT | Answer to question 2 |
| `--username` | NAME | Answer to question 3 |
| `--folder` | FOLDER | Set the folder without using the list |
| `--move-processed-to` | FOLDER | Move processed mail there (an empty value = only mark as read) |
| `--no-test` | – | Save the values without a connection test; the folder prompt is skipped |

For the password there is **deliberately no option**: it comes from the prompt or from
`MAILDIGEST_IMAP_PASSWORD` and therefore does not end up in the process list or the shell
history.

### `maildigest connect-llm`

Chooses the provider and model, stores the API key and makes a test call. Requires an existing
configuration file.

Prompts in this order:

1. A numbered **list of the operating modes** (interactive; with `--provider` or
   `--non-interactive` it is skipped and the option decides):

   ```
    1) No language model — works immediately, nothing to sign up for (default)
    2) Groq — free tier, no credit card (fast, OpenAI-compatible)
    3) OpenRouter — free models, no credit card
    4) Cerebras — free tier, no credit card
    5) Local model (Ollama, LM Studio, vLLM) — free and fully private
    6) Anthropic — paid, best summary quality
    7) Other OpenAI-compatible endpoint — enter the base URL yourself
   ```

   The selection itself reads `Option [1]: `.

   Instructions for the chosen option follow: where the key comes from, which pitfall is
   typical there and — for the free providers — where the current model IDs are listed. If the
   list is skipped with `--provider` or `--non-interactive`, the option value decides: `none`
   and `anthropic` lead to the respective instructions, `openai_compatible` to the **generic**
   instructions for OpenAI-compatible endpoints. A provider-specific explanation and a
   provider-specific `base_url` (Groq, OpenRouter, Cerebras, Ollama) then do **not** come into
   play: `base_url` stays at the previous file value or empty, unless `--base-url` says
   otherwise.

   With choice 1 the command ends immediately with exit code 0: there is neither a model name
   nor a key nor a test call, `[llm] model` and `base_url` are cleared and any stored key is
   removed.

2. `Model name (exact model ID used by the provider) [<previous value>]: ` — mandatory, there
   is deliberately no default
3. only with `openai_compatible`:
   `Base URL of the endpoint (e.g. http://localhost:11434/v1) [<previous value>]: `
4. `API key (leave empty to use MAILDIGEST_LLM_API_KEY, or if the endpoint needs no key): `
   — without screen echo. If `MAILDIGEST_LLM_API_KEY` is set, the question is skipped,
   `API key: from MAILDIGEST_LLM_API_KEY (not written to the file)` appears, and no key is
   written into the file.
5. `Response token limit per call (empty = no limit) [<previous value>]: ` — preceded by the
   recommendation from the provider knowledge base (a block with the heading
   `Response token limit (max_tokens) — optional`). An empty entry, `0`, `none`, `no`,
   `unlimited` or `-` mean **no limit**: the field `[llm] max_tokens` is removed from the file.
   An integer ≥ 1 is stored as the limit. Out of the box no limit applies (ADR-085); reasoning
   models subtract their thinking tokens from the budget, and a small limit truncates the
   answer there. On any other entry the question is repeated up to three times, then exit
   code 2.

If the base URL is neither `https://` nor `http://localhost`/`http://127.`, a warning appears
on stderr; the command continues.

The test call (`Test call ...` on stdout, `Test call skipped (--no-test).` with `--no-test`)
sends a short prompt formulated in the program (no mail content) with `max_tokens = 16`. The
model's answer is **not** displayed — only its length and whether it contains the expected word
are reported: `Response received (N characters, expected reply).` or `… unexpected reply).`
If the call fails, the command ends with exit code 1
(`Error: Test call failed (<error class>): …` plus `Check the model name, API key and base
URL.`) and writes nothing. With choice 1 the last two lines are
`Saved to <path> (file mode 0600).` and `MailDigest now runs without a language model. Run
this command again at any time to connect one.`

**Options**

| Option | Value | Meaning |
|---|---|---|
| `--provider` | `none` \| `anthropic` \| `openai_compatible` | Sets `[llm] provider`; skips the selection list. Does **not** preset a provider-specific `base_url` — that is what `--base-url` is for |
| `--model` | ID | Answer to question 2 |
| `--base-url` | URL | Answer to question 3 |
| `--max-tokens` | N | Answer to question 5: an integer ≥ 1 as the limit, `0` = no limit. Without the option, non-interactively the file value stays. Negative or not a number: exit code 2 |
| `--no-test` | – | Save without a test call |

For the API key there is no option; it comes from the prompt or from
`MAILDIGEST_LLM_API_KEY`.

### `maildigest connect-messenger`

Sets up the delivery route and sends a test message. Requires an existing configuration file.

The first line is `Connecting the messenger`, followed always by the prompt
`Messenger (telegram/discord/signal) [telegram]: `

**Telegram.** Instructions for creating the bot through @BotFather — including the step of
first writing a message to your own bot, without which the chat discovery in the next step can
find nothing. Then `Bot token (leave empty to use MAILDIGEST_TELEGRAM_TOKEN instead): `
(without echo; skipped when the environment variable is set — then
`Bot token: from MAILDIGEST_TELEGRAM_TOKEN (not written to the file)` appears and no token is
in the file). Without `--chat-id` the getUpdates flow follows: the output asks with
`Now send your bot a message in Telegram (e.g. /start).` to write the bot a message now, and
polls Telegram up to 10 times at 3-second intervals
(`No message received yet — waiting (n/10) ...`). The chat ID is taken from the first matching
message (`Chat ID found: <id>`); with several chats a numbered selection (`Chat [1]: `)
appears after `Several chats found — which one should it be?`, showing **only** the numeric
chat ID and the chat type (`private`, `group`, `supergroup`, `channel`, `unknown`) — never a
name from the chat. If nothing is found, the command ends with exit code 1
(`Error: No message to the bot found. …`); if a chat ID is already in the file, it stays and
there is only the warning `No new message found — keeping the existing chat ID.` A rejected
token leads to exit code 1 with `Error: Telegram request failed: … / Is the bot token
correct?`.

After every successful Telegram setup the output names the optional commands `/digest` and
`/status` **exactly once**, together with the switch `accept_commands` (ADR-077), so that the
user learns of their existence — on the getUpdates route just as with `--chat-id`; the
delivered test message carries the same information. If `[messenger.telegram]
accept_commands` is missing from an older file, `connect-messenger` adds the default so that
the field set from section 5 stays complete.

**Discord.** A note on creating the webhook, then `Webhook URL: ` (without echo, because the
URL itself is the secret). Without a URL: exit code 2.

**Signal.** The note
`Prerequisite: \`signal-cli --daemon --socket <path>\` is already running.`, then
`Path of the signal-cli socket [<previous value>]: ` (mandatory). The command sets
`[messenger.signal] enabled = true`. Delivery goes to "Note to Self".

After that (except with `--no-test`; otherwise `Test message skipped (--no-test).`) a
reachability test and **one test message** follow:

```
✅ MailDigest test message
Delivery works — your mail summaries will arrive here from now on
```

With Telegram — and only there — the command paragraph is appended to it:

```

This chat can also trigger MailDigest:
/digest — fetch and summarise right now
/status — short report on what is waiting
```

If the service is unreachable or delivery fails, the command ends with exit code 1 and writes
nothing into the file.

**Options**

| Option | Value | Meaning |
|---|---|---|
| `--messenger` | `telegram` \| `discord` \| `signal` | Answer to the first question |
| `--chat-id` | ID | Set the Telegram chat ID directly; the getUpdates flow is skipped |
| `--webhook-url` | URL | Discord webhook URL |
| `--signal-socket` | PATH | Path of the signal-cli socket |
| `--no-test` | – | Save without a reachability test and without a test message |

For the bot token there is no option; it comes from the prompt or from
`MAILDIGEST_TELEGRAM_TOKEN`.

### `maildigest test`

Without `--dry-run` the actual message is preceded by a short preamble (marking it as a
self-test with the note that the mail does not come from the mailbox). Without it the
delivered summary would be indistinguishable from a real one, and the user would search the
mailbox for a mail that never existed. The preamble names the **origin of the test mail** in
two variants — the bundled example mail or the file passed with `--eml` — but never the file
path: dots in the path would be visibly defanged by the output sanitizer. Wording:

```
🧪 MailDigest self-test
The next message is built from the bundled example mail, not from your mailbox — there is no such mail to look for
```

or `… built from the file you supplied, not from your mailbox — …`. If the preamble cannot be
delivered, stderr shows `Note: the self-test marker could not be delivered.`; the self-test
continues.

End-to-end self-test: processes **one `.eml` file** through the same pipeline as in production
(sanitizer → summarizer → critic → output sanitizer → messenger) and delivers the result. The
mailbox is not touched in the process.

Without `--eml` the bundled example mail is used (a harmless German appointment mail with a
link and no attachment). With `--eml <path>` your own file in RFC 822 format is fed in — that
is the **feeding procedure** for your own test mail (attack mail included): write the file,
call `maildigest test --eml file.eml`, inspect the result in the messenger or, with
`--dry-run`, on stdout.

Two properties matter for the test:

* The self-test uses its **own, temporary state database**. It changes neither the dedupe
  state nor the delivery queue of production; the same file can be fed in as often as you
  like.
* It ignores `[general] deliver_min_importance` (it delivers from `low` upwards), so that a
  test mail classified as unimportant becomes visible instead of landing in the collected
  digest.

The output has five numbered steps:

```
1/5 Configuration loaded: <path>
2/5 Test mail read: <path|bundled example mail> (N bytes)
3/5 Pipeline running (sanitizer -> summarizer -> critic -> delivery) ...
4/5 Sanitizer: N characters of text, N attachments (N processed), N links removed, N control characters removed
  Summarizer: importance=<high|normal|low>, injection suspected=<yes|no>
  Critic: phishing risk=<none|low|high>, summary accurate=<yes|no>
5/5 Delivered (N parts). Check your messenger.
```

With exactly one part the parenthesis reads `(1 part)` — here and likewise in
`5/5 Message created (N parts) — dry run, not sent:`. Line 4/5 uses the singular forms when a
counter is 1 as well: `1 attachment`, `1 link removed`, `1 control character removed`.

Neither the mail text nor the model output appears on the terminal; only the finished,
sanitised message with `--dry-run`. With `--dry-run` the line
`    Dry run: nothing is sent to the messenger (--dry-run).` additionally appears between
steps 2 and 3, and step 5 reads `5/5 Message created (N parts) — dry run, not sent:`,
followed by the message itself.

If the message was created but not delivered (the messenger does not accept it and it waits in
the queue), step 5 reads `5/5 Not delivered (N parts) — queued for retry.`; the explanation is
on stderr and the exit code is 1. The step sequence therefore has a closing 5/5 line in all
three outcomes — delivered, not delivered, fail-closed.

Exit codes: 0 when the message was created **and** delivered (or, with `--dry-run`, created
and printed). 1 when the pipeline ended fail-closed (line 4/5 then reads
`4/5 Sanitizer: failed.` and step 5 `5/5 Fail-closed: stage <stage>, reason <reason>.`, and
the messenger receives the metadata note from section 6) or when delivery was not confirmed.
1 as well with a missing, unreadable or incomplete configuration and with an unreadable
`--eml` file.

If the pipeline ends fail-closed with `--dry-run`, **nothing** goes to the messenger. Step 5
then reads

```
5/5 Fail-closed: stage <stage>, reason <reason>.
    Metadata notice created — dry run, not sent:
```

followed by the note itself on stdout; stderr carries `Self-test failed — only the metadata
notice was created (delivered: no — dry run).` Without `--dry-run` it says `(delivered: yes)`
only when the note actually reached the messenger; if it stayed in the queue, it says
`(delivered: no)` (ADR-071).

**Options**

| Option | Value | Meaning |
|---|---|---|
| `--eml` | PATH | Your own `.eml` file instead of the example mail |
| `--dry-run` | – | Only print the message on stdout, deliver nothing |

### `maildigest run`

Production. Without options MailDigest runs in the foreground and polls the mailbox every
`[imap] poll_interval_seconds` seconds; `SIGINT` (Ctrl-C) and `SIGTERM` finish the running
cycle cleanly and then the process (exit code 0).

With `--once` exactly one cycle is executed (drain the queue → poll once → drain the queue →
check the collected digest → serve the command channel), then the process ends. That is the
cron-friendly form.

On the command channel (`[messenger.telegram] accept_commands`, ADR-077/ADR-080): in
continuous operation it is polled every 10 seconds **during** the waiting time — a `/digest`
waits at most those 10 seconds, not a full `poll_interval_seconds`. With `--once` it is served
**once at the end** of the run: `/status` is answered, `/digest` has no effect there (the fetch
just happened) and is only consumed so that it does not pile up. Under cron the answer
therefore arrives on the next run, delayed by at most the cron interval.

The collected digest and the delivery queue do **not** depend on the mailbox being reachable:
if the mailbox is down, both are worked off anyway before the run ends with exit code 1
(ADR-049 addendum).

Output: structured **JSON lines on stdout** (one object per event, fields `ts`, `level`,
`logger`, `event` and event-dependent extra fields). The threshold comes from
`[general] log_level`. Mail content, subject lines and secrets never appear there — only
shortened hashes, sender domains, status values and counters. With `--once` a summary line on
**stderr** is added:

```
Run finished: N mails fetched, N processed, N duplicates, N errors, N messages delivered, N queued.
```

`N messages delivered` counts **all** messages delivered in that run: directly delivered
individual messages, delivered metadata notes, the collected digest and messages delivered
late from the queue (ADR-070). Messages remaining in the queue only count in the run in which
they get through; until then they appear under `N queued`.

Exit codes: 0 on a clean end, 1 on an incomplete configuration, an unusable state database or
— only with `--once` — an unreachable mailbox (`Error: Mailbox unreachable: …`). A missing
IMAP password (neither `[imap] password` nor `MAILDIGEST_IMAP_PASSWORD`) belongs to the
**first** case: it is detected before the connection is established and reported as a
configuration error
(`Error: Invalid configuration (<path>):` / `  - [imap] password: required value missing. …`),
not as a reachability problem. A failed move is **not** such a case: it concerns a single mail,
is logged as `imap_postprocess_failed` and does not abort the run (ADR-065). In continuous
operation a mailbox outage is not an abort either: it reconnects with growing intervals (5 s,
10 s, 20 s … at most 10 minutes).

**Options**

| Option | Value | Meaning |
|---|---|---|
| `--once` | – | Run one cycle and exit |

### `maildigest instructions`

Shows or changes the custom instructions (`[summarizer] instructions`, F-SUM-3) without having
to hunt for the file by hand (ADR-086). Requires an existing configuration file. The text goes
into the summarizer's system prompt as a clearly labelled block (I8); the critic never sees it
(ADR-042), and the security rules cannot be switched off with it.

Without an option the current text is shown: `Custom instructions (<N> characters):`, followed
by the text (each line indented by two spaces), or `Custom instructions: (none)`. After that
the two lines `They reach the summarizer as a labelled block; the critic never sees them.` and
`Change them with: maildigest instructions --set "..." | --add "..." | --edit | --clear`.

With an option the text is changed and saved: `Saved to <path> (file mode 0600).`, then the
new text as above (without the two note lines). If the text is unchanged, the only output is
`Custom instructions unchanged.` and nothing is written. Normalisation before saving: Windows
line endings become `\n`, trailing and surrounding whitespace is removed. More than 2000
characters, or control characters other than newline and tab: exit code 2, nothing saved.

`--edit` opens the text in the editor from `$VISUAL`, otherwise `$EDITOR`, otherwise `nano` or
`vi` from the path (`Opening <editor> ... (save and close the editor to apply)`). The edit file
lives in the configuration's directory for that time (mode 0600) and begins with comment lines
(`#`) that are discarded on apply. If the editor exits with a status other than 0, nothing is
saved (exit code 1). Without a terminal (`--non-interactive`) or without a findable editor:
exit code 2 with a pointer to `--set` or `--add`.

**Options** (at most one)

| Option | Value | Meaning |
|---|---|---|
| `--set` | TEXT | Replaces the text with TEXT |
| `--add` | TEXT | Appends TEXT as a new line |
| `--edit` | – | Opens the text in the editor |
| `--clear` | – | Removes the text |

## 5. Configuration file

Format TOML, UTF-8. File mode **0600** — every command that writes sets it again. Unknown
fields are an error (typo protection): on load MailDigest reports
`[sektion] feld: Unbekanntes Feld — Tippfehler?` and exits with code 1.

All fields with their defaults:

| Section / field | Type / values | Default | Meaning |
|---|---|---|---|
| `[general] language` | text | `"de"` | Language of the summaries |
| `[general] summary_length` | `short`/`medium`/`long` | `"medium"` | Level of detail |
| `[general] deliver_min_importance` | `low`/`normal`/`high` | `"normal"` | From this importance upwards mail is delivered individually; below it goes into the collected digest |
| `[general] low_digest_time` | `HH:MM` | `"18:00"` | Time of the collected digest (the server's local time) |
| `[general] state_db` | path | `""` | Empty = `state.db` next to the configuration file; relative paths are relative to its directory |
| `[general] log_level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `"INFO"` | Log threshold. `DEBUG` enables tracebacks that can contain mail content — such logs are confidential |
| `[imap] host` | text | — | **Mandatory.** Hostname of the mirror mailbox |
| `[imap] port` | 1–65535 | `993` | IMAPS only; 143 is rejected |
| `[imap] username` | text | — | **Mandatory.** |
| `[imap] password` | text | — | Alternatively `MAILDIGEST_IMAP_PASSWORD` |
| `[imap] folder` | text | `"INBOX"` | The folder being read |
| `[imap] poll_interval_seconds` | ≥ 5 | `120` | Poll interval in continuous operation |
| `[imap] move_processed_to` | text | `""` | Empty = only set the seen flag. The folder must exist on the server, and the server must support the MOVE extension. If either is missing, the mail stays as read in the source folder and the run logs `imap_postprocess_failed` with the reason — the cycle continues (ADR-064/ADR-065) |
| `[llm] provider` | `none`/`anthropic`/`openai_compatible` | `"none"` | The provider. `none` = operation without a language model (ADR-076): what is delivered is a labelled excerpt instead of a summary, and all deterministic warnings remain |
| `[llm] model` | text | `""` | **Mandatory as soon as `provider` is not `none`.** The exact model ID; deliberately no default |
| `[llm] api_key` | text | — | Alternatively `MAILDIGEST_LLM_API_KEY`. Required for `anthropic`, usually not for local servers |
| `[llm] base_url` | URL | `""` | Endpoint for `openai_compatible` |
| `[llm] max_tokens` | ≥ 1 | — | Upper bound per answer and call. **If the field is absent, no limit applies** — the ceiling of the provider or model (ADR-085; with `anthropic` the API's mandatory ceiling, 32,000). `connect-llm` asks about it and suggests sensible values |
| `[llm.critic] provider` | as `[llm]` | inherits | Override for the critic |
| `[llm.critic] model` | text | inherits | Override |
| `[llm.critic] base_url` | URL | inherits | Override |
| `[llm.critic] max_tokens` | ≥ 1 | inherits | Override; can set a limit where `[llm]` has none |
| `[summarizer] instructions` | text | `""` | Custom instructions: what matters, what to look out for. Steers style and importance, cannot switch off the security rules |
| `[links] footnote` | `true`/`false` | `false` | Append the defanged link list to the message as a footnote |
| `[messenger] active` | `telegram`/`discord`/`signal` | `"telegram"` | The active delivery route |
| `[messenger.telegram] token` | text | — | Alternatively `MAILDIGEST_TELEGRAM_TOKEN` |
| `[messenger.telegram] chat_id` | text | `""` | Target chat; `connect-messenger` discovers it |
| `[messenger.telegram] accept_commands` | true/false | `true` | Whether MailDigest accepts commands from the chat (ADR-077, the default `true` since ADR-078). When on, `run` reacts to `/digest` (a fetch cycle, waiting at most 10 s) and `/status` (a short report), **only** from `chat_id`; with `run --once` the channel is served once at the end of the run and `/digest` has no effect there (ADR-080). The two words are recognised tolerantly: case does not matter, surrounding whitespace and an `@botname` suffix are stripped, and extra text after the command is ignored. Any other text is discarded; not a single character from the chat — neither extra text nor chat title nor sender name — ever reaches a language model or a delivered message |
| `[messenger.discord] webhook_url` | URL | — | The channel's webhook (itself a secret) |
| `[messenger.signal] enabled` | `true`/`false` | `false` | Enable the Signal adapter |
| `[messenger.signal] signal_cli_socket` | path | `""` | Socket of `signal-cli --daemon` |
| `[limits] max_mail_bytes` | ≥ 1 | `26214400` | Largest processed mail (25 MB); above it only a metadata note |
| `[limits] max_text_chars` | ≥ 1 | `30000` | Plain-text budget across body and attachments. Raw mail and attachment text is capped **before** sanitisation already: sixteen times this value, as a **remaining budget across the whole mail** (body and all attachment texts together, ADR-084 addendum). Once it is exhausted, a further attachment text does not go through sanitisation at all and the attachment counts as unprocessed; only this budget is visible anyway |
| `[limits] pdf_max_input_bytes` | ≥ 1 | `10485760` | Largest processed PDF (10 MB) |
| `[limits] pdf_max_output_chars` | ≥ 1 | `50000` | Text yield per PDF |
| `[limits] pdf_timeout_seconds` | ≥ 1 | `20` | Time limit of **one** PDF extraction |
| `[limits] pdf_time_budget_seconds` | ≥ 1 | `30` | Time budget of **all** PDF extractions of one mail (ADR-029 addendum). Each extraction gets `min(pdf_timeout_seconds, remaining budget)`; once the budget is used up, the attachment counts as unprocessed just as on a timeout and no child process is started. Without this budget, 20 PDF attachments held the fetch up for around 400 s |
| `[limits] max_mime_depth` | ≥ 1 | `10` | Maximum MIME nesting |
| `[limits] max_attachments_processed` | ≥ 0 | `20` | Attachments processed for content per mail |
| `[limits] max_html_elements` | ≥ 1 | `50000` | Elements of the HTML conversion, as a **remaining budget per mail** across all HTML parts; above it the part is not converted (ADR-084). Raising it extends the conversion linearly — the promise "at most 10 s" applies to the default |
| `[limits] max_html_bytes` | ≥ 1024 | `1048576` | Byte budget of the HTML conversion per mail (1 MB), checked **before** parsing; together with at most four converted `text/html` parts it caps the conversion time of one mail at roughly two to three seconds depending on machine load — well below the promise "at most 10 s" (ADR-084 addendum). Real newsletters are far below that; a larger value extends the conversion disproportionately |

**What the promise "at most 10 s" means.** It concerns the **command latency in the wait path**
(ADR-080): a `/digest` or `/status` from the chat waits at most ten seconds for a reaction. It
is not a promise about the duration of a fetch cycle. The pure sanitisation of the most
expensive mail that all the bounds above permit at all costs a measured 3.6 to 5.1 s of CPU,
about half of it in the standard library's MIME parser; end to end, including the fetch parse
and raw serialisation, around 8 s (ADR-084 addendum, fifth iteration); a PDF-heavy mail can
extend the cycle by up to a further `pdf_time_budget_seconds`, because a foreign parser runs in
a subprocess there (ADR-029 addendum). The command channel stays within its ten seconds in both
cases, because it is served between the segments of the waiting time.

**Environment variables**

| Variable | Effect |
|---|---|
| `MAILDIGEST_CONFIG` | Path of the configuration file when `--config` is missing |
| `MAILDIGEST_IMAP_PASSWORD` | Overrides `[imap] password` |
| `MAILDIGEST_LLM_API_KEY` | Overrides `[llm] api_key` |
| `MAILDIGEST_TELEGRAM_TOKEN` | Overrides `[messenger.telegram] token` |

A set variable always beats the file value; an empty value is ignored.

## 6. Message format

Every delivery is **plain text**. It never contains a clickable link, never an attachment,
never HTML or Markdown. Markdown constructs are neutralised, including those that only work at
the start of a line (headings, lists, quotes, Discord subtext) and underscores at word edges;
bullet lists appear as `•`, numbered lines as `12 · …`. `@everyone` and `@here` appear as
`(at)everyone`/`(at)here`. The structural line starts (`⚠️`, `📧`, `📎`, `🔍 Notes:`, `From:`)
are produced exclusively by the program; identical starts in model text are neutralised —
**in both languages**, so also `Von:`, `Betreff:`, `Hinweise:`, `Stufe:`, `Grund:`,
`PHISHING-VERDACHT:` (ADR-062, ADR-083: the output is English, but model text must not be able
to forge a German-looking header line either). All domains and filenames appear with a broken
dot (`example[.]com`, `invoice[.]pdf`), because messengers autolink bare domains. If the
message is longer than the target system's limit (Telegram 4096, Discord 2000, Signal 2000
characters), it is split at line boundaries into several messages. If a single line has to be
cut in the process, every continuation begins with `… `; the prefix makes the continuation
visible and prevents a cut from pushing one of the reserved line starts to the beginning of a
message.

**Normal delivery**

```
⚠️ SUSPECTED PHISHING: <reasons, comma-separated, max. 5>  ← only at phishing risk high
📧 <headline> [important]                                  ← tag only at importance high
From: <display name> (<domain>) · <DD.MM. HH:MM>           ← without a Date header: `date unknown`
<summary>
— <file>: <1–2 sentences per processed attachment>
📎 Not processed: <file (size)>, … [and N more]
🔍 Notes: <injection suspicion; encrypted mail; Message-ID collision; auth failures;
           punycode; mixed writing systems; hidden text removed from the HTML;
           HTML part differs from the text part; HTML part too complex (not converted);
           Reply-To/Return-Path divergence;
           text truncated; link budget capped; critic reasons at risk low>
<link footnote (defanged), one address per line>           ← only with [links] footnote = true
```

The notes after `🔍 Notes: ` are comma-separated and read literally, in exactly this order:
`the mail contained instructions aimed at the AI (ignored)`,
`encrypted (PGP/S-MIME) — content not readable by design`,
`Message-ID collides with an earlier mail`, `sender checks failed: <SPF=…, DKIM=…>`,
`punycode domain(s): <…>`, `mixed writing systems: <…>`,
`HTML part differs from the text part`, `HTML part too complex, not converted`,
`hidden text removed from the HTML`,
`reply address differs from the sender`, `return-path domain differs`, `text truncated`,
`too many links, further links removed unlisted`, `critic: <reasons>`. If no note applies, the
line is omitted.

`too many links, further links removed unlisted` — the mail carried more links than a single
mail gets evaluated individually (2000, `sanitize.links.MAX_LINKS_PER_MAIL`). All of them are
removed; the surplus ones appear in the text as `[Link removed]` without a number and without
a domain and therefore do not appear in the footnote either (ADR-028 addendum).

Substitute texts when a field is empty: `📧 (no summary)` for the headline, `(no summary)` or
`(file)` in the attachment line, `unknown` for a missing sender, `(unnamed)` for an attachment
without a filename. A link without a recognisable host appears as `[Link #n: unknown]`, a
`mailto:` address without a domain as `[Mail #n: unknown]`. If the sanitizer truncates the mail
text, it ends with `[truncated]`.

Two notes explain why a mail looks different from what was expected, and therefore have fixed
wording:

- `encrypted (PGP/S-MIME) — content not readable by design` — the mail was end-to-end
  encrypted (`multipart/encrypted`, `application/pgp-encrypted`, `application/pkcs7-mime`).
  MailDigest does not decrypt; headline, sender and the list of unprocessed parts still arrive
  (ADR-082). Signed but unencrypted mail (`multipart/signed`) does not trigger the note.
- `Message-ID collides with an earlier mail` — this mail's `Message-ID` was already taken by a
  mail with **different** content. It is delivered anyway (ADR-079); the note explains why a
  transaction can appear twice.

Without a language model (`[llm] provider = "none"`) the attachment line carries a labelled
excerpt of the attachment text that was read (`— file.txt: Excerpt: …`) instead of a summary —
just as the summary line there is an excerpt.

**Number and date formats** are not literals and stay independent of the output language:
sizes appear as `34 KB` or, with a decimal comma, `1,2 MB`, and the date of the sender line as
`DD.MM. HH:MM` (ADR-083).

Lines without content are omitted. Individual limits: headline 120, summary 3000, attachment
summary 400, critic reason 200, display name 80, domain 100, filename 80 characters; at most
10 attachments named and 5 banner reasons. Truncation uses `…`. Filenames are truncated in the
**middle** so that the extension is preserved (`aaa…aaa.exe`) — for an unprocessed attachment
it is the most important piece of information.

**Metadata note (fail-closed)** — always exactly these five lines:

```
⚠️ This mail could not be processed safely — no content delivered.
From: <domain>
Subject: <subject>
Stage: <sanitize|summarize|critic|compose|deliver> · Reason: <error class>
Open your real mailbox to read it.
```

If the domain is missing, it says `unknown` there; if the subject is missing, `(no subject)`.
If a stage or error class cannot be rendered as a label, it says `unknown`.

Error classes: `sanitize_error`, `llm_timeout`, `llm_rate_limited`, `llm_invalid_response`,
`llm_transport_error`, `schema_invalid`, `summary_inaccurate`, `delivery_error`,
`state_error`, `delivery_failed`, plus `<stage>_error` for anything unknown.

**Daily collected digest** — one message from `[general] low_digest_time` onwards, grouped by
category (largest group first), one line per mail; from 60 mails onwards the rest is counted.
An empty queue produces no message.

```
🗂 12 low-priority mails: 8 newsletter, 3 benachrichtigung, 1 other

newsletter (8):
• Wochenrückblick KW 36 (news[.]example[.]org)
…
... and N more
```

The category names come from the model output and therefore follow `[general] language`; an
empty category is called `other`. If a headline is missing, it says `(no subject)`; if the
domain is missing, `unknown`.

## 7. Assurances given by the program

These points are part of the contract and can be looked up in REQUIREMENTS.md as F-SEC-*:

1. **Nothing is deleted** in the mirror mailbox. What is written is only the seen flag and —
   if configured — the move into `move_processed_to`. Technically those are exactly two IMAP
   commands: `UID STORE +FLAGS (\Seen)` and `UID MOVE`. An `EXPUNGE` is never sent — it would
   also permanently delete foreign messages marked `\Deleted` by other programs (ADR-064).
2. No language model sees raw HTML, raw MIME parts or attachment binaries.
3. Attachments are never delivered. Only `text/plain` files and PDFs are processed for content
   (after a magic-byte check); everything else appears only in the line `📎 Not processed:`.
4. If any stage fails, the metadata note arrives — never unchecked content and never a silent
   disappearance.
5. Secrets live exclusively in the configuration file (0600) or in environment variables —
   never in prompts, logs, error messages or the state database.
