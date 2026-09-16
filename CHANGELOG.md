# Changelog

All notable changes to MailDigest. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the version numbers follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Distribution
- **Our own package repositories for apt and dnf** (ADR-088): MailDigest will become
  installable from a signed apt repository on GitHub Pages (`apt install maildigest`) and
  from a COPR project (`dnf install maildigest`). Both hang off the release tag:
  `.github/workflows/release.yml` builds the `.deb`, test-installs it on Debian, Kali and
  Ubuntu, signs it and triggers the COPR build. The plan with all steps is in
  [docs/PLAN-PACKAGING.md](docs/PLAN-PACKAGING.md); the instructions in the README will
  only be switched over once the repository really carries packages.
- Package users get the manual page under `/usr/share/man/man1` — `man maildigest` works
  without the copy step from the README.

### Changed
- **Lower bounds on two dependencies**: `pydantic>=2` (the code uses `ConfigDict` and
  `model_validator`, which do not exist in pydantic 1) and `imap-tools>=1.0` (only the 1.x
  line is tested). Without them the distribution package inherits unversioned dependencies
  and installs on systems where it cannot start — Ubuntu 24.04 LTS ships pydantic 1.10 and
  is therefore not supported; apt now refuses the installation there instead of producing a
  broken one.

## [0.2.0] — 2026-09-14

The first public release. Since 0.1.0, MailDigest has run against real counterparts for the
first time (a mirror mailbox at web.de, a model via OpenRouter, a Telegram bot) and has gone
through two documented test rounds (docs/TESTRUNDE-HOT-COLD.md and docs/TESTRUNDE-2.md,
acceptance in docs/ABNAHME-FIXRUNDE.md — all German). What stayed open is in
docs/TESTING.md §7 and below under "Known limitations".

### Features
- **Operation without a language model** as the default (`[llm] provider = "none"`,
  ADR-076): MailDigest runs without signing up with any provider and delivers a labelled
  excerpt together with all deterministic warnings. `connect-llm` offers the operating modes
  as a list to pick from, among them three providers with a free tier and the local variant.
- **Remote triggering via Telegram** (ADR-077, the default changed by ADR-078):
  `maildigest run` reacts to `/digest` (an immediate fetch) and `/status` (a short report) —
  **only** to those two words and **only** from the configured chat. Any other text is
  discarded and never reaches a language model. The switch is
  `[messenger.telegram] accept_commands`, **on** by default since ADR-078; set it to `false`
  for group chats.
- **A provider knowledge base** for the setup (ADR-075): `connect-mail` explains the term
  IMAP host, translates a typed mail address into the host, and aborts immediately with a
  reason for providers without password login (Outlook.com, Proton).
- **`maildigest instructions`** (ADR-086): shows the custom instructions for the summarizer
  or changes them with `--set`, `--add`, `--edit` (in `$VISUAL`/`$EDITOR`) and `--clear` —
  without hunting for the place in the configuration file. Multi-line, up to 2000
  characters; control characters are rejected. The critic still never sees the text.
- **Help and manual** (ADR-087): `maildigest <command> --help` explains every command with a
  description and examples, `maildigest --help` the typical flow. `maildigest --man` prints a
  manual page in troff format (`| man -l -`); `man/maildigest.1` is generated from it, and a
  test keeps it in sync with the parser.

### Fixed

From the final test round (38 findings, docs/TESTING.md §7; report in
docs/TESTRUNDE-HOT-COLD.md):

- **A single mail could stall the service.** A 16 KB mail with 250 nested MIME levels made
  the conversion fail with `RecursionError`: continuous operation died, `maildigest run
  --once` failed on every run, and because the mail was never marked as read, every mail
  arriving after it stayed unprocessed until someone removed it from the mailbox by hand. The
  nesting depth is now capped at 32 levels before any evaluation (log
  `mail_mime_depth_capped`), and if the conversion of a mail fails nonetheless, the user gets
  the metadata note, the mail gets status `failed`, and the fetch continues with the next
  mail (O-1, ADR-020 addendum).
- **Followed up (second iteration): a 31 KB mail could still block the fetch permanently.**
  At around 1000 nested `message/rfc822` parts, the IMAP library already failed to parse —
  before MailDigest even saw the mail. The error was reported as a connection problem
  ("Mailbox unreachable", or endless reconnect attempts), the mail stayed unread and blocked
  every mail behind it. The fetch now retrieves every mail individually per UID; a mail that
  cannot be parsed is identified through a header fetch, delivered as a metadata note, booked
  as `failed` and marked as read — the mail behind it is processed, `run --once` ends with
  exit 0 and counts it as an error in the summary. New log event `mail_unparsable`; the
  number of IMAP commands per fetch stays the same (O-1, ADR-020 addendum second iteration,
  ADR-064 addendum).

- **Without a language model, a subject over 100 characters no longer produced a summary but
  only the metadata note** — for an everyday class of mail the shipped state was therefore
  non-functional. The subject is now truncated with `…` (HC-1).
- **Without a language model, attachment text that had been read disappeared without a
  trace**, and the message claimed "mail without displayable content". Every attachment that
  text could be read from now appears as a line of its own with a labelled excerpt (HC-2).
- **One mail could silently suppress another** by copying its `Message-ID`. Two mails with
  different content and the same header are now both delivered; the second one carries the
  note "Message-ID collides with an earlier mail" (HC-10).
- **When saving the configuration, the old file could be left truncated** (full disk, quota,
  power loss). It is now written atomically and stays byte-identical on an abort (HC-20).
- **A jump of the system clock cost a waiting message its delivery attempts**, or parked it
  in the queue permanently. Both directions are caught (HC-25).
- **The daily collected digest was skipped entirely during a mailbox outage**, even though it
  needs no IMAP. It now goes out then as well (HC-26).
- **A `/digest` from the chat waited until the end of the poll interval** (up to two minutes)
  and **swallowed every command that stood behind it in the same batch**. Both fixed: an
  answer within ten seconds at the latest, and every command is answered (HC-12, HC-13).
- **A model provider's error text could remote-control the terminal** (clear the screen, set
  the window title, place an invented program message); one's own API key quoted back by the
  counterpart now appears as `***` (HC-4).
- **Senders are shown with their real name** ("Jörg Müller" instead of
  `=?utf-8?Q?J=C3=B6rg_M=C3=BCller?=`), including with raw 8-bit headers. Only that makes
  hidden control characters in the display name detectable at all (HC-23).
- **An encrypted mail now says that it is encrypted** ("encrypted (PGP/S-MIME) — content not
  readable by design") instead of looking like a mail with no content (HC-33).
- **Hardening of the delivered message** (F-SEC-3/F-SEC-5): an exactly forged data-block
  marker and the most common takeover formulas ("Ignoriere deine bisherigen Anweisungen",
  "Forget all previous instructions") now trigger the warning line (HC-5, HC-21); a message
  split can no longer put a forged program line at the start of a part (HC-6); the link
  footnote no longer contains Markdown (HC-7); a raw control character no longer reaches the
  message (HC-8); bare IP addresses and very long domain labels are now broken in every
  neighbourhood (HC-9, HC-24); the `/status` answer defangs the folder name (HC-28). Followed
  up (S-4): a dot chain of more than four number groups (`1.1.1.1.1.1.1.`) was broken only in
  the last four-octet window and the first four octets stayed as a clickable address; now
  every dot of the chain is broken (ADR-036, addendum S-4).
- **No more mail or model text in the operator's log**: a field name invented by the model is
  replaced by `<extra field>` (HC-11).
- **Overlong attachment filenames** are visibly truncated in the middle and keep their
  extension — for a blocked attachment the security-relevant piece of information (HC-22).
- **Setup**: `connect-llm --provider openai_compatible` no longer enters a foreign provider
  URL and shows the matching instructions (HC-3, HC-36); `connect-mail` rejects Outlook.com
  and Proton as a mail address as well (HC-15) and explains a failure in line with its cause
  (HC-32); `init` again ends with the list of next steps, now including `connect-llm`
  (HC-18); the hints about `/digest` and `/status` also appear after
  `connect-messenger --chat-id` (HC-19); `run` reports a missing IMAP password as a
  configuration error instead of "mailbox unreachable" (HC-34); `test` ends the step sequence
  with a `5/5` line even when the message stays in the queue (HC-35), and the self-test
  preamble names the file passed with `--eml` (HC-17).
- **Robustness**: an unusable `Retry-After` (such as `nan`) no longer throws MailDigest out
  of the error taxonomy (HC-30); a whitespace-free model field ran quadratically and now takes
  milliseconds instead of seconds (HC-29).

From the second test round (docs/TESTING.md §7 "Follow-up fix round NF-1"; report in
docs/TESTRUNDE-2.md):

- **A single mail could stall MailDigest for minutes.** An HTML part with very deeply nested
  elements made the conversion to text grow quadratically — 68 KB of attack HTML cost five
  seconds, a megabyte would compute to minutes, and during that time neither was mail fetched
  nor were `/digest` or `/status` answered. The conversion is now linear (16,000 levels:
  28.6 s → under 0.1 s) and additionally has a hard bound: an HTML part with more than
  `[limits] max_html_elements` elements (default 50,000) or more than 2000 nesting levels is
  **not** converted. The mail still goes out — with the plain-text part if present, and with
  the note `HTML part too complex, not converted`, so that nobody mistakes an incomplete
  summary for a complete one (HC2-1, ADR-084).
- **A forged sender display name could determine the sender domain shown.** Whoever sent
  their name encoded as `Bank <info@bank.example>,` appeared in the delivery line as
  `bank.example`, even though the mail came from `attacker@evil.example` — and the two
  warnings about a diverging reply address and a diverging return path went silent in the
  process. The sender address and domain are now read from the unmodified header value; only
  the name is decoded (HC2-2). The readable plain name available since HC-23 is retained.
- **Followed up (second iteration):** the bound for HTML parts only bit after the parser had
  read the entire input — a 24 MB HTML part stalled the service for 47 seconds even though it
  was discarded afterwards; and it applied per part, so 34 inconspicuous HTML parts together
  cost 29 seconds without anything being displayed. New is a byte cap **before** the
  conversion (`[limits] max_html_bytes`, default 1 MB) which, together with the element
  bound, applies as a budget for the **whole mail**; at most four HTML parts per mail are
  converted at all. The same attack mails now cost 0.1 s and 0.7 s respectively, and no mail
  can occupy the conversion for longer than about two seconds. Real newsletters are unaffected
  (HC2-1, ADR-084).
- **Followed up (second iteration):** an encoded display name could still forge the sender
  domain when it carried `@` and `,` unencoded (`=?utf-8?Q?info@bank.example,?=`). Encoded
  words are now replaced by a neutral placeholder before the address is read, and only put
  back as the name afterwards — a name can therefore neither replace nor delete the sender
  domain (HC2-2, ADR-020).
- **Followed up (third iteration):** a single mail could still block the fetch for minutes —
  not through the HTML but through the number of links: removing links grew disproportionately
  more expensive with every further address (a mail with 41,000 links cost 29 seconds even
  though it stayed within every bound), and for plain text there was no bound at all (21 MB of
  text = 4.5 seconds). Removal now runs in a single pass, at most 2000 links are listed
  individually per mail (further ones are still removed and shown as `[Link removed]`; the
  message says so with "too many links, further links removed unlisted"), and raw mail and
  attachment text is pre-cut to a multiple of the text budget before checking. The same mails
  now cost 1.1 s and 0.4 s; the most expensive mail possible at all is around two to three
  seconds (HC2-1, ADR-028/ADR-084).
- **Followed up (third iteration):** the sender forgery through an encoded display name was
  possible again with one missing character (`=??Q?…?=` without a charset). The detection of
  encoded words now follows exactly the form the standard library's decoder uses (HC2-2,
  ADR-020).
- **Followed up (fourth iteration):** the correction to sender detection from the third
  iteration had earned itself a stall: a mail with a broken, very long sender header stalled
  the fetch for minutes (a 156 KB header = 18 seconds), and that already on reading, before
  any size check. Headers are now cut to 4096 characters before any processing, and the
  detection of encoded words runs in a single pass — the same mail now costs no measurable
  time. The same cap protects the subject (HC2-2, ADR-020).
- **Followed up (fourth iteration):** an encoded word **after** the sender address made the
  address and domain disappear entirely — the message showed "(unknown sender)", and the
  warnings about a diverging reply address and a diverging return path went silent. The
  address is now read from the header's first angle bracket if need be, and an **unknown**
  sender domain alongside a known return path now counts as a warning case rather than a calm
  one (HC2-2, ADR-020).
- **Followed up (fourth iteration):** a single mail could still block the fetch for around ten
  seconds: the plain-text pre-cut applied per text chunk, so twenty attachments simply
  multiplied it, and for the number of MIME parts there was no bound at all. Both are now a
  budget for the whole mail (at most 500 parts; further ones are counted, not read). The most
  expensive mail possible at all therefore costs 2.7 instead of 9.2 seconds (HC2-1, ADR-084).
- **Followed up (fourth iteration):** the time limit of PDF processing applied per attachment
  — twenty PDF attachments would have stalled the fetch for around 400 seconds, far beyond the
  fetch period. New is `[limits] pdf_time_budget_seconds` (default 30 s) as a time budget
  across **all** PDFs of one mail; once it is used up, the remaining attachments count as
  unprocessed and are named as such in the message. Measured: three PDF attachments 60 s →
  30 s (ADR-029).
- **Followed up (fifth iteration):** the header cap from the fourth iteration applied only to
  the sender, reply address and subject. A mail with a huge `To:` header (20 MB) stalled the
  fetch for 18 seconds, and the internal re-serialisation of the mail cost up to 14 seconds
  with very many or very long headers. Now **every** header read is cut to 4096 characters in
  a single place, the headers of the whole mail are capped as a total budget, and `to_addrs`
  carries at most 200 recipients. The same mails now cost no measurable time; ordinary mail
  stays byte-identical (HC2-1, ADR-020).
- **Followed up (fifth iteration):** the repair from the fourth iteration could take a bank
  address out of a comment or quotation marks in the sender header as the sender domain, and
  switch both warnings off in the process — above all when the 4096-character cut severed a
  comment. Comments and quotation marks are now removed before the search (with nesting and
  escapes), an incomplete header counts as "sender unknown" and triggers the return-path
  warning. In addition, a reply address with appended text could silence the warning
  "diverging reply address"; a `Reply-To` that is present but unreadable now counts as a
  warning case (HC2-2, ADR-020).
- **Followed up (follow-up fix round O-3, six attempts):** the sender detection from
  iterations two to five could still be deceived with irregularly encoded words
  (`=?utf-8?Q?Support?(?=`): the tool showed `bank.example` without a warning where the mail
  program shows `real@evil.example`. The sender and reply address are now read by the standard
  library's RFC 5322 parser; the mask, bracket fallback and comment scanner are removed. The
  rule, checked in six skeptic passes across more than 60,000 header forms against
  `email.policy.default`: never a domain the mail program does not show, never "unknown"
  without a warning, no exception, no stall. Mail programs reply to **all** Reply-To
  addresses, so every one counts; an unreadable reply address alongside a known sender
  warns; HTML entities in the display name are resolved before the check (O-3, ADR-020
  addenda).
- **Followed up (fifth iteration):** the most expensive permitted mail costs not 2.7 but 3.6
  to 5.1 seconds of CPU in sanitisation — more than half of that in the standard library's
  MIME parser, which no budget of our own reaches. The figure is corrected in the
  documentation; nothing changes about the ten-second promise for the command channel
  (HC2-1, ADR-084).

### Changed
- All user-visible texts are English; docstrings stay German. **Since ADR-083 that is a
  decision**: the program interface and the message frame are English independently of any
  language setting, and `[general] language` now steers only the text fields produced by the
  language model. SPEC-CLI §2/§4/§6 is the literal contract of that output and is checked
  mechanically against it by `tests/unit/test_hc14_spec_literals.py`.
- The message delivered by `maildigest test` is marked as a self-test.
- **Schema version 3 of the state database** (ADR-079). An existing file is upgraded silently
  and without data loss when first opened — no intervention needed, no migration tool. There
  is no way back: an upgraded file can no longer be opened with an older MailDigest version.
- **The configuration file is written atomically** (ADR-081). The configuration directory has
  to be writable for that; during the write a file `.<name>.<pid>.tmp` with mode `0600` lives
  there briefly.
- **Commands are polled every ≤ 10 s in continuous operation** instead of once per poll cycle
  (ADR-080). MailDigest therefore calls `getUpdates` up to `poll_interval_seconds / 10` times
  per cycle.
- **`maildigest run --once` (cron) serves the command channel too**, once at the end of the
  run: `/status` is answered, `/digest` has no effect there and is merely consumed (ADR-080).
- Continuations of a hard line cut begin visibly with `… `; they count towards the messenger's
  part limit (ADR-062/ADR-040, addenda).
- New log events: `mail_id_collision`, `outbox_clock_skew_corrected`, `low_digest_failed`,
  `command_ignored_once`, `command_handling_failed` (docs/OPERATIONS.md §5).
- **The response budget is unlimited out of the box** (ADR-085): `[llm] max_tokens` no longer
  has a default; if the field is absent, the model's own ceiling applies. The reason:
  reasoning models subtract their thinking tokens from the budget — with the old default of
  1024, nearly every mail from a real mailbox ended as a fail-closed note. `connect-llm` now
  asks about a limit as its fifth question and shows recommendations beforehand; the new
  option is `--max-tokens` (`0` = no limit). Whoever wants a limit sets it deliberately.

### Known limitations
- The statement "no replies from the messenger" from 0.1.0 still holds with a restriction: no
  dialogue, no actions — except for the fixed command list above.
- Open findings from the test rounds (docs/TESTING.md §7, table "Open at the end of the
  follow-up fix round"): a mail made of millions of empty MIME parts costs up to around 35 s
  per fetch cycle, because the standard library's parser runs before every bound (O-2). The
  placeholder of an unparsable mail can suppress a later genuine mail with the same Message-ID
  as a duplicate (O-6). If the server answers the fetch of a single mail with `NO`
  permanently, that mail blocks the fetch (O-7). Plus six low points (O-4, O-5, O-8 to O-11)
  and the follow-up fix packages NF-2 to NF-7 from docs/ABNAHME-FIXRUNDE.md §8. The two
  previously high findings O-1 (a poison mail stops the fetch) and O-3 (a forgeable sender
  domain) are fixed before this release.

## [0.1.0] — 2026-09-08

The first complete release. MailDigest reads a mirror mailbox, summarises every mail with a
language model, has a second instance check it for phishing and delivers plain text to
Telegram, Discord or Signal.

**This version has never run against real counterparts** — no real mailbox, no real LLM API,
no real messenger. See "Known limitations".

### Features

- **Mailbox.** IMAPS fetch of unread mail from a dedicated mirror mailbox, dedupe via the
  Message-ID with persistent state in SQLite. What gets written is only the seen flag and —
  if configured — a server-side `UID MOVE`.
- **Summary.** A summarizer model produces a headline, text, category and an importance
  (`high`/`normal`/`low`) in a configurable language and length. Custom instructions steer
  style, focus and the notion of importance.
- **Critic.** A second, independent model instance with its own prompt and optionally its own
  provider judges the phishing risk and the correctness of the summary. It deliberately does
  not get to see the custom instructions.
- **Attachments.** Text from `text/plain` and PDF is summarised along with the mail;
  everything else appears as a "not processed" line with a name and size. The file itself is
  never delivered.
- **Delivery.** Adapters for Telegram, Discord and Signal (`signal-cli`, note to self), a
  persistent delivery queue with retries, and a split of long messages at line boundaries to
  the target system's limit.
- **Collected digest.** Mail below the delivery threshold arrives once a day, collected and
  grouped by category.
- **Operation.** `maildigest run` as a long-lived process with a clean SIGINT/SIGTERM
  shutdown, `run --once` for cron. Structured JSON log lines, a systemd unit in
  docs/OPERATIONS.md.
- **Setup.** `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test` — every
  command with a connection test; `connect-mail` prints the instructions for forwarding with
  Gmail, posteo and mailbox.org. `maildigest test --dry-run` drives an `.eml` file through
  the real pipeline without sending.

### Security

- **The zero-privilege principle.** The two model stages are the only ones that interpret
  foreign text, and the only ones without any capability: no tools, no function calling, no
  network or file access. The request body of both providers consists of a closed field set.
- **Sanitizer before the model.** No model sees raw HTML, raw MIME parts or attachment
  binaries. Zero-width and bidi control characters are removed, punycode and homoglyph domains
  flagged, links replaced by `[Link #n: domain]`.
- **Sanitizer after the model.** The delivered message never contains a clickable link, never
  an attachment, never markup. Markdown is neutralised — including the forms that only work at
  the start of a line —, domains and filenames appear with a broken dot, `@everyone`/`@here`
  are defused. Telegram without `parse_mode`, Discord without embeds.
- **Model-free detection.** Forged data-block markers, clusters of invisible characters and
  literal instructions to a language model set the injection suspicion in code, before any
  model is asked. Several independent forgery signals raise the phishing risk to `high` even
  against a silent model.
- **Fail-closed.** Every error in the sanitizer, model, critic, delivery or state leads to the
  five-line metadata note instead of unchecked content. Nothing disappears silently.
- **Attachment extraction in a subprocess** with time, memory, input and output limits;
  `pdfminer` is never loaded in the parent process.
- **No delete path on the mailbox.** Neither `\Deleted` nor `EXPUNGE` exists in the code.
  Without the server's MOVE capability the mail stays put — falling back to copy + delete is
  deliberately not done.
- **Secrets** as `SecretStr`, the config file at `0600` on every write, no command-line
  options for passwords and tokens, no secrets in prompts, logs or the database.
- **Invariant review I1–I8** across the whole codebase, documented in docs/SECURITY.md §7 and
  pinned mechanically in `tests/unit/test_invarianten.py`.

### Quality assurance

- 1200+ tests, among them property-based tests over random model outputs, fault injection at
  every pipeline stage and an attack corpus.
- One white-box run (12 findings, docs/TESTING.md §5) and one black-box run by an agent
  without code access (16 findings, §6). All findings from `medium` upwards are fixed and
  covered by a regression test.
- Coverage: `sanitize/` and `output/` above 98 %, overall above 95 %.

### Known limitations

- **No run against real counterparts.** All evidence comes from mocks.
- **The second black-box round is missing.** Our own test protocol requires it after the two
  `high` findings of the first run; it has not taken place.
- **No OCR, no image analysis** — phishing in a screenshot is only reported as an unprocessed
  attachment.
- **No decryption of PGP/S-MIME.**
- **Signal only as "Note to Self"**, with a running `signal-cli --daemon`.
- **New warning heuristics are uncalibrated** (the phrase list of the injection detection, the
  HTML divergence threshold, the combination rule for `high`) — false alarms are more likely
  than missed cases.
- **`connect-mail` does not warn in advance** when the server cannot do `MOVE` or
  `move_processed_to` does not exist; the error only shows up in production, without data
  loss.
- **Processing latency never measured** (NF-4 stays open).
- One mailbox per installation, no replies from the messenger, no access to the real mailbox.

### Docs

- A README with a security model diagram, quickstart, FAQ and an honest list of limitations;
  the CHANGELOG created.
- Checked against the code and corrected in WP12: the message example in the README showed a
  note line ("1 Link entfernt") the program never produces; SPEC-CLI §2 now says where the log
  lines of the inner layers go; docs/OPERATIONS.md §5 knows `imap_postprocess_failed` and
  describes `mail_processed` correctly.
