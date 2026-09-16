# MailDigest — Architecture

> Components, data model and contracts between the pipeline stages. Maintained in every WP;
> discrepancies between the code and this document are bugs (in one of the two).

## 1. Overview

A single Python process runs a linear pipeline. Every stage has exactly one input and one
output data class; stages do not know each other (dependency injection in `pipeline.py`).

```
[1] Ingest        IMAP → RawMail                       (ingest/)
[2] Sanitize      RawMail → SanitizedMail              (sanitize/)   ← code only
[3] Summarize     SanitizedMail → Summary              (agents/summarizer, llm/)
[4] Critic        SanitizedMail + Summary → Verdict    (agents/critic, llm/)
[5] Compose+Guard Summary + Verdict → DigestMessage    (output/)     ← code only
[6] Deliver       DigestMessage → messenger            (messenger/)
```

An error in stages 2–5 ⇒ a `FailureNotice` (metadata note) instead of a summary; an error in
1 or 6 ⇒ retry with backoff, state stays consistent.

## 2. Components

### Ingest (`ingest/imap_client.py`)

**As of WP2 (ADR-016 to ADR-020):**

- **Transport:** IMAPS only, via `imap_tools.MailBox` with `ssl.create_default_context()`
  (certificate and hostname verification active). No code path to
  `MailBoxUnencrypted`/`MailBoxStartTls`, no switch to disable verification. Port from
  `[imap] port`; port 143 is rejected with an understandable error message.
- **Fetch per UID (O-1, ADR-020 addendum, second iteration):** first `uids(AND(seen=False))`
  (one `UID SEARCH UNSEEN`) in the folder `[imap] folder`, then per UID
  `fetch(uid_list=[uid], mark_seen=False, bulk=False)` (one `UID FETCH`). The same commands
  as the previous `fetch(bulk=False)` (1 + n per cycle), but the eager parse in
  `MailMessage.__init__` runs under its own guard per mail: if it fails (`RecursionError` from
  984 `message/rfc822` levels), a second, capped `UID FETCH
  (BODY.PEEK[HEADER]<0.262144> RFC822.SIZE INTERNALDATE)` fetches the headers only
  (`BytesHeaderParser`, which does not recurse), and the mail travels as an
  `UnparsableMailMessage` → substitute `RawMail` (`ingest_failed`) into the fail-closed path;
  log `mail_unparsable`. Transport/protocol errors (`ImapToolsError`, `OSError`,
  `imaplib.IMAP4.error`) remain `ImapConnectionError`. The `mark_seen=False` is
  security-relevant: the seen flag is the last action, not the first (see below).
- **Order per mail (F-ING-2, ADR-019):**
  1. build the `RawMail` (if that fails — or the parse in imap-tools already did — the
  substitute `RawMail` with `ingest_failed = True` takes its place, and the cycle never
  aborts, O-1), 2. `StateDB.claim(dedupe_key, content_hash=…)` (`INSERT OR IGNORE`,
  committed **before** processing), 3. pipeline callback, 4. write the final status,
  5. set `UID STORE +FLAGS (\Seen)` and, if configured, `UID MOVE` to
  `[imap] move_processed_to`. An already known key is skipped but still marked as read and
  moved, so it leaves the unseen set. `claim()` is three-valued (ADR-079): `claimed` ⇒
  process, `duplicate` ⇒ skip, `collision` ⇒ process under the derived key
  `sha256(message_id_hash + content_hash)`, set `RawMail.id_collision = True` and log
  `mail_id_collision` (WARNING).
- **Deleting: never** (F-ING-1, ADR-064). Post-processing issues **raw UID commands** via
  `mailbox.client.uid(...)` rather than imap-tools' convenience methods: `MailBox.flag()` and
  `MailBox.delete()` append an unconditional `EXPUNGE` to every STORE, and `MailBox.move()`
  falls back to `copy()` + `delete()` when the MOVE capability is missing. `\Deleted` and
  `EXPUNGE` occur in no path of the module. Moving happens exclusively server-side
  (`UID MOVE`, RFC 6851); if the server cannot do it, the mail stays put marked as read and
  the operation reports `MailboxPostProcessError`.
- **Post-processing errors (ADR-065):** `MailboxPostProcessError` (connection is up, the
  server answers `NO` — typically: `move_processed_to` points at a folder that does not
  exist) is caught per mail in `poll_once`, logged as `imap_postprocess_failed`, and does not
  stop the cycle. Only `ImapConnectionError` triggers reconnect/backoff.
- **Header cap (ADR-020 addendum, NF-1 fourth and fifth iteration):** every header value is
  read in **one** place (`_raw_header_values`) and cut there, before any processing, to 4096
  characters and 32 values per name; `to_addrs` carries at most `MAX_RECIPIENTS` (200)
  addresses. Before re-serialisation (`mime_bytes`), `_cap_message_headers` additionally caps
  the whole parsed tree (256 KiB and 4096 headers per mail; further parts are removed, log
  `mail_headers_capped`), because the standard library's folder otherwise pays per header and
  per whitespace run. Ordinary mail stays byte-identical. The address fallback (R-9) never
  reads comment or quoted-string content (a linear scanner per RFC 5322); an unbalanced header
  yields "address unknown".
- **Dedupe key:** `Message-ID`, otherwise `sha256:` + a hash over From + Date + Subject +
  body prefix (512 characters), fields separated by `\x00`. The prefix makes fallback keys
  distinguishable from real Message-IDs.
- **Second dedupe criterion (ADR-079):** `RawMail.content_hash` = `sha256(mime_bytes)`,
  stored in `seen_mails.content_hash`. The key alone comes from a freely choosable header;
  only the second criterion distinguishes "the same mail again" from "a foreign mail under
  the same name". Rows without a value (a database from before schema version 3) count as
  "content unknown" and never trigger a collision.
- **Display names (ADR-020 addenda, HC-23/HC2-2):** `From` and `Reply-To` are RFC 2047
  decoded like the subject before they land in `RawMail`; raw 8-bit names are read as UTF-8
  (falling back to Latin-1). The sanitizer therefore sees the name, not its encoding. **The
  order matters for security:** address and `from_domain` come from the **raw** value parsed
  with `getaddresses`, and only the name part is decoded. Otherwise an encoded display name
  smuggles a second address into the address list and determines the sender domain shown
  (HC2-2). The decoded name is stripped of `<`, `>`, `,`, `;`, `:`, `"`, `\`; `from_addr` is
  reassembled as `Name <address>`, and only in a form that `parseaddr` splits back into
  exactly those two parts.
- **Polling loop (`IngestService`):** `run_once()` for `run --once`; `run_forever()` with
  `[imap] poll_interval_seconds` (default 120 s) and shutdown through a `threading.Event`
  (`stop()`). A connection error ⇒ reconnect with exponential backoff 5 s, 10 s, 20 s …
  capped at 600 s (§6); after a successful poll the backoff is reset.
- **Error in a single mail (I6):** if the processing callback throws, the mail gets status
  `failed` with `error_class = "ingest_error"`, is marked as read, and the loop continues.
  The metadata note is produced by the pipeline itself (`FailedNotice`).
- **IMAP IDLE:** deliberately not implemented (ADR-007, confirmed in ADR-016).
- **Logging (NF-5/I5):** only a shortened dedupe hash, sender domain, status, exception class
  name, backoff duration. **As of WP8 (ADR-047):** the crash path logs
  `mail_processing_crashed` with the exception *class name*; the traceback appears only at
  `log_level = "DEBUG"` (`logging_setup.traceback_enabled`).
- **Status ownership (WP8, ADR-050):** `poll_once`/`IngestService` know the flag
  `write_result_status` (default `True`). The runner sets it to `False` and manages the
  status itself; the `failed` case of the crash path is always written regardless.
- **Size limit:** `limits.max_mail_bytes` deliberately has no enforcement point in ingest —
  per SECURITY §4 the size policy belongs to the sanitizer (implemented there since WP3,
  checked against `size_bytes` **and** `len(mime_bytes)`); ingest merely fills
  `RawMail.size_bytes` correctly from `RFC822.SIZE`.

### Sanitizer (`sanitize/`)

- Pure code, no LLM, no network access.
- Modules: `attachments.py` (allowlist + magic bytes), `html_to_text.py`, `links.py`,
  `unicode_clean.py`, `extract_pdf.py` (subprocess with limits), `sanitizer.py`
  (orchestration).
- Policy and limits: SECURITY.md §4 (binding there).

**As of WP3 (ADR-026 to ADR-030):**

- **Entry point:** `sanitize.MailSanitizer` implements the `Sanitizer` protocol from
  `pipeline.py` (`sanitize(raw: RawMail) -> SanitizedMail`). Constructor:
  `MailSanitizer(limits: LimitsConfig | None, *, link_footnote: bool = False)`; convenience:
  `MailSanitizer.from_config(config)` (uses `[limits]` and `links.footnote`).
- **Error path:** `sanitize.SanitizeError` (the message is a constant label such as
  `mail_zu_gross`/`mime_unparsbar`, never mail content — I5). The class name is mapped in
  `pipeline._ERROR_CLASSES` to `reason_class = "sanitize_error"` (ADR-012). A mail above
  `limits.max_mail_bytes` (checked against `size_bytes` **and** `len(mime_bytes)`) throws
  before any parsing — the enforcement point for the size policy left open in WP2 is
  therefore here.
- **Flow per mail:** size check → `email.message_from_bytes` (compat32, robust against broken
  headers) → manual MIME walk with a depth limit (`message/rfc822` is never entered, T13) →
  body aggregation (all inline `text/plain`, otherwise all inline `text/html` via
  `html_to_text`, with element and depth caps, see below) → per text:
  `unicode_clean.clean_text` → marker neutralisation (`neutralize_forged_markers`: a forged
  `<<<MAILDIGEST-…-UNTRUSTED-…>>>` block becomes the token
  `[forged data-block marker removed]` and is counted in `forged_markers` — **before** the tag
  strip, which would otherwise delete it without a trace, HC-5) → link scrub
  (`links.LinkCollector`, one instance per mail: `#n` runs through body, attachments, subject
  and display name) → tag strip for plain text as well → total plain-text budget
  (`[truncated]`).
- **Attachments:** `attachments.detect_kind(declared_mime, data)` yields
  `pdf|text|html|mismatch|unknown`; only `text` (file) and `pdf` (subprocess,
  `extract_pdf.extract_pdf_text(...) -> str | None`, `None` ⇒ unprocessed) are processed. The
  keys of `attachment_texts` are the sanitised (collision-freed) filenames and match
  `AttachmentInfo.filename_sanitized`. `blocked_attachments` = the number of entries with
  `processed=False`.
- **Report:** fully populated, including `reply_to_mismatch` (address comparison via
  `parseaddr`, a missing Reply-To ⇒ False), `return_path_mismatch` (only with two known
  domains), `auth_results` (regex parse `spf|dkim|dmarc=value`, first mention wins), and
  punycode/mixed-script flagging for the sender domain as well.
- **Caps on the HTML conversion (ADR-084, HC2-1):** `html_to_text` counts elements and
  nesting depth directly after parsing and aborts with `HtmlTooComplexError` above
  `[limits] max_html_elements` (default 50,000) or `html_to_text.MAX_HTML_DEPTH` (2000) —
  **before** the hidden-text heuristic and `get_text`. The sanitizer then discards exactly
  that part, sets `html_rejected` in the report (note line `HTML part too complex, not
  converted`) and continues fail-safe; an existing `text/plain` part is delivered normally,
  and raw HTML never leaves the stage (I1). The same cap applies in the divergence check
  `_html_diverges` (ADR-067) — which is the practically more important entry point, because it
  also runs when a plain-text part exists. In front of it sits (ADR-084 addendum, NF-1 second
  iteration) a pure **byte cap** `[limits] max_html_bytes` (default 1 MB): the element and
  depth caps only see the tree once the parser has built it — the byte length is checked
  before `clean_text` or BeautifulSoup touch the part at all. Byte and element caps run as a
  **remaining budget for a whole mail** (`HtmlBudget`, one object per `sanitize()` run, shared
  by the body path and the divergence check), and at most `MAX_HTML_PARTS` (4) `text/html`
  parts are converted at all; further ones count as not converted (`html_rejected`).
- **Caps on the plain-text and link path (ADR-084/ADR-028 addendum, NF-1 third and fourth
  iteration):** raw body and attachment text runs against a **remaining budget for the whole
  mail** of `_RAW_TEXT_FACTOR` (16) times `[limits] max_text_chars` characters — one object
  per `sanitize()` run, shared by the body and all attachment texts, modelled on `HtmlBudget`.
  The budget is debited **before** `clean_text`, marker neutralisation and the link scrub run
  (and before the divergence check); it sets `truncated`, and an attachment text with nothing
  left for it does not go through the expensive passes at all and counts as unprocessed.
  Alongside it stands `sanitizer.MAX_MIME_PARTS` (500): parts beyond that are not entered but
  counted as a single metadata entry `(mime-teile ueberschritten)` — otherwise the sheer
  number of parts multiplies every other cap. The link scrub is linear in the number of
  matches (a single back-substitution over the placeholder pattern) and has its own per-mail
  budget with `links.MAX_LINKS_PER_MAIL` (2000): further matches are still removed (I3 holds
  without exception) but appear only as `[Link removed]` and set `links_capped` in the report
  (note line `too many links, further links removed unlisted`). What none of these caps
  reaches is `email.message_from_bytes` itself: the standard library's line-wise parser runs
  **before** any budget, and a 25 MB mail of three-byte lines costs around 2 s there alone
  (ADR-084 addendum, fifth iteration).
- **Not the sanitizer's job:** `RawMail.date`/`from_domain` are taken over unchanged (the
  trust model from ADR-020); formatting the attachment notes for the message ("⚠ 2 unprocessed
  attachments …") is WP7 (`output/`), based on the `AttachmentInfo` list.

### LLM layer (`llm/`)

**As of WP4 (ADR-021 to ADR-025):**

- `base.py`: the protocol
  `LLMProvider.complete(system, user, *, max_tokens: int | None, temperature: float | None = None) -> str`.
  `max_tokens=None` = no limit (ADR-085): `openai.py` then does not send the field,
  `anthropic.py` sets the mandatory ceiling `ANTHROPIC_MAX_TOKENS_CEILING` (32,000). No tool
  use in the interface (I2). `temperature=None` (the default) means: the field is not sent —
  current models reject it with HTTP 400 (ADR-022). Error classes: `LLMError` (base),
  `LLMTimeout`, `LLMRateLimited`, `LLMInvalidResponse`, `LLMTransportError` (HTTP/connection
  errors that are neither a timeout nor a 429). Constants `DEFAULT_TIMEOUT_SECONDS = 60.0`,
  `MAX_ATTEMPTS = 3`.
- `_http.py`: the shared HTTP mechanics of both providers (POST, retry, error mapping) —
  guaranteeing identical semantics. Retries 429 and 5xx only, max. 3 attempts, backoff 1 s/2 s
  (capped at 30 s) or `Retry-After` in seconds. Timeouts are not retried. Error messages
  contain the status code and `error.type`, never the response body (I5).
- `anthropic.py`: `POST {base_url}/v1/messages`, headers `x-api-key` +
  `anthropic-version: 2023-06-01`, default host `https://api.anthropic.com`. Model from the
  config (no code default). The response is the concatenation of all `text` blocks; without a
  text block, `LLMInvalidResponse`.
- `openai.py`: `POST {base_url}/chat/completions` (default `https://api.openai.com/v1`),
  bearer auth only when a key is present — local servers (Ollama/vLLM) do not need one. System
  prompt as `role="system"`, data block as `role="user"` (I8).
- `schema.py`: `complete_json(provider, system, user, schema, *, max_tokens, temperature)`.
  Extracts JSON robustly from Markdown code fences and surrounding text (a brace-balanced
  scan, string- and escape-safe), validates against pydantic, exactly one repair retry, then
  `LLMInvalidResponse` (I6). The repair hint sits in the system prompt and contains field
  paths, error types and the JSON schema — never the rejected model answer (ADR-024).
- `factory.py`: `build_provider(config, role)` with `role = "summarizer" | "critic"`
  (override inheritance from `[llm.critic]`), plus `max_tokens_for(config, role)` (`None` = no
  limit, the factory state since ADR-085). A missing API key with provider `anthropic` ⇒
  `ConfigError` pointing at `MAILDIGEST_LLM_API_KEY`.
- `prompts.py`: all prompt texts centrally, versioned through `PROMPT_VERSION` (scheme
  `wp<NR>/<YYYY-MM-DD>[.n]`; every substantive change bumps it). As of WP5: the summarizer
  building blocks `summarizer_system_prompt(*, token, language, summary_length,
  custom_instructions)` and `summarizer_user_prompt(mail, *, token)`, plus
  `default_token_source()` / `block_markers(token)` for the per-call random data-block markers
  (ADR-032) and `format_size()` for German size renderings. As of WP6: additionally
  `critic_system_prompt(*, token, language, max_reasons)`,
  `critic_user_prompt(mail, summary, signals, *, token)` and `summary_markers(token)` for the
  critic's second untrusted block (ADR-041). `PROMPT_VERSION` stands at `wp6/2026-09-02`.

### Operation without a language model (`agents/offline.py`)

**As of 2026-09-09 (ADR-076).** `[llm] provider = "none"` is the default after
`maildigest init`. `build_runner` and `cli.Hooks` then install the stages
`OfflineSummarizer`/`OfflineCritic` instead of `SummarizerAgent`/`CriticAgent`:

- **OfflineSummarizer** builds the `Summary` from `SanitizedMail` alone: the subject as the
  headline — truncated to 100 characters with `summarizer.clamp_headline` **before**
  construction, because `Summary.headline` carries that bound as `max_length` and the
  post-check would come too late (HC-1) —, an excerpt of the already sanitised text truncated
  to 400 characters with a fixed label (`Excerpt, not a summary …`), and
  `importance = "normal"` (a guessed `low` would silently push mail into the collected
  digest). Afterwards the same `enforce_output_policy` runs as for a model output — including
  `detect_injection_evidence` (F-SEC-5/CT-6).
- **Attachments without a model:** per entry in `attachment_texts` the OfflineSummarizer puts
  a labelled excerpt, truncated to 400 characters (the composer's per-value limit), into
  `attachment_summaries`; the composer renders it as `— <file>: Excerpt: …`. Without that
  step, an attachment whose text was successfully extracted dropped out of the message without
  a trace — `📎 Not processed` only names the *blocked* ones (HC-2).
- **OfflineCritic** starts at `phishing_risk = "none"` and leaves the raising entirely to
  `enforce_verdict_policy(collect_signals(mail))` — the same code signals as in model
  operation (ADR-043/ADR-063). `summary_accurate` is always true: the text comes from the
  sanitizer, there is nothing to hallucinate.
- **Retries:** only the self-built offline stages run without `RetryingSummarizer`/
  `RetryingCritic` — there is no network call. A stage injected from outside (tests) keeps its
  retries.
- **Security posture:** stricter than model operation, not looser — there is no untrusted
  model output. I2 is trivially met (no model call), I1/I3/I4/I6 unchanged.

### Agents (`agents/`)
- `summarizer.py`: builds the prompt (system + labelled custom instructions + delimited data
  block), calls `complete_json`, runs the deterministic post-check.
- `critic.py`: first computes deterministic signals (code!), then calls the LLM with the mail
  plain text + summary + signals, returns a `CriticVerdict`. Details under "As of WP6".

**As of WP5 (ADR-031 to ADR-034):**

- **Entry point:** `agents.summarizer.SummarizerAgent` implements the `Summarizer` protocol
  from `pipeline.py` (`summarize(mail) -> Summary`). Constructor:
  `SummarizerAgent(provider, *, language, summary_length, instructions, max_tokens,
  token_source)`; convenience: `SummarizerAgent.from_config(config, provider=None)` (uses
  `[general] language/summary_length`, `[summarizer] instructions`,
  `llm.factory.build_provider(config, "summarizer")` and `max_tokens_for(config,
  "summarizer")`). A `[llm.summarizer]` section does not exist (ADR-025): the role
  `summarizer` = `[llm]`.
- **Prompt construction (I8, SECURITY §5):** system prompt = role → user settings (a separate,
  labelled block capped at 2000 characters) → non-overridable security rules +
  language/length/importance/output format. User message = trustworthy program facts from the
  `sanitization_report`, then the mail content (subject, display name, domain, date, blocked
  attachments as metadata, `body_text`, `attachment_texts`) between per-call random markers
  with an untrusted notice, then a format reminder.
- **Post-check (`enforce_output_policy`, pure code, I4):** scan of every text field for
  Markdown links, HTML tags, numeric entities, URL patterns (`schema://`, `hxxp`, `www.`,
  `mailto:`, `tel:`, `(.)`/`[.]`/`(dot)`, `domain.tld/path`) and Unicode `C*` characters. A hit
  ⇒ replacement by `[entfernt]` (URL patterns word-wise) **and** `injection_suspected = true`;
  a flag set by the model stays set. Then: headline single-line and ≤ 100 characters, empty
  fields filled from sanitizer values (`headline` ← subject, `category` ← `sonstiges`,
  `summary_text` ← metadata substitute text), `attachment_summaries` restricted to keys from
  `attachment_texts`. Bare domains without a path remain — the markers `[Link #n: domain.tld]`
  are permitted under I3. This layer does **not** normalise to NFKC; fullwidth forms are only
  caught by the output sanitizer (ADR-033 "Consequences", ADR-036).
- **Mail without displayable text:** the LLM call happens anyway (subject, sender and
  attachment names are signals); if `summary_text` stays empty, the deterministic substitute
  text `describe_without_body` takes over ("Mail without displayable content, N blocked
  attachments: …"). If text was read from attachments, it does not claim missing content but
  names it ("No mail body; N attachments with readable text", HC-2).
- **Not the summarizer's job:** the delivery threshold `deliver_min_importance` (evaluated by
  `pipeline.process_mail`, including the F-CRIT-2 promotion) and the message formatting (WP7).

**As of WP6 (ADR-041 to ADR-044):**

- **Entry point:** `agents.critic.CriticAgent` implements the `Critic` protocol from
  `pipeline.py` (`review(mail, summary) -> CriticVerdict`). Constructor:
  `CriticAgent(provider, *, language, max_tokens, token_source)`; convenience:
  `CriticAgent.from_config(config, provider=None)` (uses `[general] language`,
  `llm.factory.build_provider(config, "critic")` and `max_tokens_for(config, "critic")`, i.e.
  the overrides from `[llm.critic]`). There is **no** `instructions` parameter: the critic is
  the independent second instance (ADR-042).
- **Deterministic signals (`collect_signals`, pure code, F-CRIT-3):** from
  `SanitizationReport` + `AttachmentInfo` an ordered sequence of `Signal(key, text, hard)`
  arises, with the keys `reply_to_mismatch`, `return_path_mismatch`,
  `auth_ok` / `auth_failed` / `auth_missing` (value rendering `DKIM=…, DMARC=…, SPF=…`;
  "passed" = `pass|none|neutral|policy`, identical to the note line in `output/`), `punycode`,
  `mixed_script`, `blocked_attachments` (with declared MIME types), `links_removed`,
  `hidden_text`, `control_chars`, `truncated`. The sequence is never empty (the auth line
  always appears) and contains no mail text — it cannot be manipulated by the sender (T9).
- **Prompt construction (I8, SECURITY §5, ADR-041):** system prompt = role → non-overridable
  security rules (naming both marker pairs) → check task 1 (phishing patterns: urgency, payment
  request, credential request, sender discrepancies, atypical language, manipulation of the
  processing) → check task 2 (hallucination check including a warning about the fail-closed
  effect of `summary_accurate = false`) → risk levels → language/form → output format. User
  message = the signals as program facts, then the mail block
  (`<<<MAILDIGEST-UNTRUSTED-DATA …>>>`, built identically to the summarizer's), then the
  summary block (`<<<MAILDIGEST-UNTRUSTED-SUMMARY …>>>`), both with the same random identifier
  and both marker-neutralised.
- **Post-check (`enforce_verdict_policy`, pure code, I4, ADR-043/044):** every text field is
  NFKC-normalised and scrubbed with the same policy as the summarizer's
  (`agents.summarizer.scrub_text`); then made single-line, `risk_reasons` ≤ 5 entries of 200
  characters without duplicates or empty entries, `notes` ≤ 500 characters. A hit makes itself
  visible as a reason of its own and raises `phishing_risk` to at least `low`; hard signals do
  the same (`hard=True`, currently only `mixed_script`). Code reasons come before model
  reasons so that truncation does not displace them. The level is never lowered and never set
  to `high` by code; `summary_accurate` is left untouched.
- **Not the critic's job:** the effect of the verdict. The warning banner and minimum
  importance at `high` (F-CRIT-2) as well as the fail-closed path at
  `summary_accurate = false` (T8) live in `pipeline.process_mail` and `output/composer.py`.

### Output (`output/`)
- Builds the `DigestMessage` from summary + verdict (format §7) and sanitises every field
  (URL/Markdown/HTML strip, escaping, length split per messenger).

**As of WP7 (ADR-035 to ADR-040):** two modules. `output/sanitizer.py` provides `scrub_field`
(entity resolution → `unicode_clean.clean_text` → optional field truncation → tag strip →
segmentation: WP3 markers and already defanged forms unchanged, everything else markup
neutralisation + `links.LinkCollector.scrub`), `scrub_plain` (the same without link detection,
for domain/display name/filename), `final_guard` (a final pass over the finished message:
break every live scheme containing `://` as well as `javascript:`/`data:`-like schemes, break
`www.`, remove `<`/`>`, split `](`, defang domains/IPv4) and `split_parts`.
`output/composer.py` contains `DigestComposer` (implements `OutputComposer` from `pipeline.py`
with `compose` **and** `compose_failure`; `from_config` picks the messenger limit); `parts` is
produced exclusively through `_finalize()` = `final_guard` + `split_parts`. One `LinkCollector`
per message ⇒ continuous marker numbering across all fields.

### Messenger (`messenger/`)
- The protocol `Messenger.send(DigestMessage)`, `healthcheck()`.
- `telegram.py` (Bot API, plain text without parse_mode), `discord.py` (webhook),
  `signal.py` (signal-cli JSON-RPC, feature flag).

**As of WP7:** `base.py` = the adapter protocol `send(DigestMessage)` + `healthcheck() -> bool`
plus `MessengerError` (carried in `pipeline._ERROR_CLASSES` as `delivery_error`) and the
constants `DEFAULT_TIMEOUT_SECONDS = 30.0`, `MAX_ATTEMPTS = 3`. The protocol is deliberately
wider than the one of the same name in `pipeline.py` (the CLI needs the health check).
`_http.py` = its own retry mechanics with the LLM layer's policy (429/5xx only, max. 3
attempts, backoff 1 s/2 s or `Retry-After`, timeouts not retried), error messages without
URL/headers/body (I5 — Telegram carries the token in the path). `telegram.py`:
`POST {base_url}/bot<token>/sendMessage`, one request per part, **no `parse_mode`** (ADR-006)
and `disable_web_page_preview: true`; `ok: false` counts as an error despite HTTP 200; health
check via `getMe`. `discord.py`: a webhook POST with `content` only and
`allowed_mentions: {"parse": []}`, no embeds; health check via a GET on the webhook URL.
`signal.py`: JSON-RPC (`send` with `noteToSelf`, `version`) over a Unix socket, behind
`[messenger.signal] enabled`. `factory.py`: `build_messenger(config)` picks by
`[messenger] active` and raises `ConfigError` on a missing token/chat ID/webhook or on Signal
not being enabled.

### State (`state/db.py`)
- SQLite, tables:
  - `seen_mails(message_id_hash TEXT PK, first_seen_at, status, error_class, retry_count,
    content_hash)`
  - `low_digest_queue(id, message_id_hash, received_at, headline, category, from_domain)`
  - `outbox(id, message_id_hash, kind, payload, attempts, first_queued_at, next_attempt_at,
    last_error)`
  - `meta(key, value)` — e.g. the schema version, the last digest timestamp.
- Status values: `pending → sanitized → summarized → checked → delivered | failed | skipped_low`.
- No full mail text in the DB (SECURITY.md §6).

**As of WP2 (ADR-018):** `seen_mails` and `meta` are created; `low_digest_queue` only arrives
in WP8. `message_id_hash` is `sha256(dedupe_key)` in hex — the dedupe key itself is never
stored and never logged unshortened (NF-5). `error_class` is normalised on write to
`[a-z0-9_]`, max. 64 characters (a structural I5 bound; it unifies the character set and
length and does not replace the callers' duty to pass constant labels). The schema version
lives in `meta.schema_version` and is checked on open (NF-3, no migration tooling); the DB file
is created with mode `0600`. The idempotency primitive is `StateDB.claim()` (`INSERT OR IGNORE`
on the primary key), not `was_seen()`.

**As of WP8 (ADR-045, ADR-048, ADR-049):** schema version 2. New are `low_digest_queue`
(F-SUM-5) and `outbox` (the delivery queue). The step 1 → 2 is purely additive and is performed
silently when a WP2 file is opened (only new tables, no migration tooling, NF-3); any other
version mismatch remains a `StateError`. The file path comes from `[general] state_db` or
`config.resolve_state_db_path()` (ADR-045). Both new tables contain **only already
output-sanitised** text (headline/category/domain, or the finished message parts), never raw
mail text, and are emptied after delivery (docs/SECURITY.md §6). Delivery results may only move
records in status `checked` (`promote_checked_to_delivered`).

**As of the fix round (ADR-079):** schema version 3. New is the nullable column
`seen_mails.content_hash` (`sha256(mime_bytes)` of the mail, HC-10). The step 2 → 3 is again
purely additive and performed silently on open — new tables are created by
`CREATE TABLE IF NOT EXISTS`, new columns by `ALTER TABLE … ADD COLUMN`
(`StateDB._add_missing_columns`); existing rows are left untouched and get `NULL` = "content
unknown". `claim()` has returned `ClaimResult` (`claimed`/`duplicate`/`collision`) instead of
`bool` since then.

### Orchestration & operation (`runner.py`, `delivery.py`, `logging_setup.py`)

**As of WP8 (ADR-045 to ADR-051):**

- `runner.build_runner(config, *, config_path, …)` wires all stages:
  `MailSanitizer.from_config` → `RetryingSummarizer(SummarizerAgent.from_config)` →
  `RetryingCritic(CriticAgent.from_config)` → `DigestComposer.from_config` →
  `OutboxMessenger(StateDB, messenger.factory.build_messenger)`, plus `StatusRecorder` as the
  `ProgressSink` and `deliver_min_importance` from `[general]`. Every stage is replaceable by
  argument (tests, `maildigest test` in WP9).
- `Runner.run_once()`: drain the queue → one IMAP poll (`IngestService.run_once`) → drain the
  queue again (also on failure, `finally`) → check the collected digest.
  `Runner.run_forever()`: the same cycle in a loop with the poll interval, reconnect backoff
  from `ingest.backoff_delay` and shutdown through SIGINT/SIGTERM (ADR-051).
- **Command channel in the loop (ADR-077/ADR-080, as of the fix round 2026-09-11):** after
  every cycle and thereafter after **every** wait segment of at most
  `runner.COMMAND_POLL_SECONDS = 10` seconds, `run_forever` calls `_serve_commands()`. That
  works off the batch it read completely (a list instead of `any(…)` over a generator —
  otherwise every command after the first `/digest` was dropped) and reports whether another
  cycle should follow immediately. `_wait_for_next_cycle()` replaces the earlier single
  `_wait(poll_interval)`; `stop()` ends every segment immediately so that SIGINT does not wait
  on a long poll. `run_once` serves the channel once at the end (`_serve_commands_once`):
  `/status` answers, `/digest` is merely consumed.
- **Exception-proof zone (ADR-049 addendum):** the delivery queue **and** the collected digest
  run in the `finally` of `run_once` and, in `run_forever`, in the `except IngestError` branch
  as well — neither needs a mailbox. An error in the digest is caught there
  (`low_digest_failed`) so that it does not mask the `IngestError`.
- Status sequence: `pending` (ingest `claim`) → `sanitized` → `summarized` → `checked` →
  `delivered` | `skipped_low` | `failed`. `checked` is committed **before** sending (ADR-008);
  it stays that way as long as the message sits in the `outbox` — only a confirmed delivery
  writes `delivered`, and a finally failed one `failed` with
  `error_class = "delivery_failed"`.
- `delivery.OutboxMessenger` satisfies the narrow `pipeline.Messenger` protocol: enqueue
  (commit) → attempt immediately → success deletes the row, failure postpones it. `send()` does
  not throw on delivery errors; `flush()` works off due entries (5 attempts, backoff
  60/300/900/2100 s, hard bound 1 h).
- `logging_setup.configure_logging(level, stream)` configures only the `maildigest` logger and
  writes one JSON line per event to stdout; `log_level` comes from `[general]`, tracebacks only
  at `DEBUG` (ADR-046/ADR-047).
- Operations documentation (systemd unit, cron variant, maintenance): docs/OPERATIONS.md.

### CLI (`cli.py`)

**As of WP9 (ADR-052 to ADR-057).** The complete, binding contract:
[SPEC-CLI.md](SPEC-CLI.md) — this section describes the internal structure only.

- Commands: `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test`,
  `instructions` (show/change the custom instructions, ADR-086), plus `--man` (the manual page
  from the parser, `manpage.py`, ADR-087), `run [--once]`; exit codes 0/1/2. Entry points:
  `[project.scripts] maildigest` and `python -m maildigest`.
- `argparse` with a `parents=` parser for `--config`/`--non-interactive`, so that both options
  may appear before and after the command name (ADR-052). `main(argv, stdin, stdout, stderr,
  hooks)` **returns** the exit code; only `run_cli()` calls `sys.exit`.
- `Hooks` bundles every outside contact (`build_runner`, `build_messenger_from_section`,
  `build_provider_from_settings`, `build_summarizer`/`build_critic`, `ImapClient`,
  `discover_chat_ids`, `configure_logging`, `sleep`) — tests replace them individually.
- `Console` encapsulates every input and output: `ask`/`ask_int`/`ask_secret`/`confirm`/
  `choose`, defaults, value lists, three failed attempts, EOF = abort. `--non-interactive`
  switches to "defaults and options only"; a missing mandatory value then names the
  responsible option and exits with code 2.
- `ConfigFile` holds the file as a raw dict, `render_toml` writes it back with comments, and
  `save()` creates it via `os.open(..., 0o600)` and resets the mode on every write (ADR-053,
  F-SEC-8). Validation happens section by section through `config.validate_section`, because
  the configuration is incomplete during setup.
- Extensions to existing modules for the CLI (all additive): `config.validate_section`,
  `llm.factory.build_provider_from_settings`,
  `messenger.factory.build_messenger_from_section`, `messenger.telegram.discover_chat_ids`,
  `ImapClient.list_folders`, `DigestComposer.compose_plain` (ADR-054).
- `maildigest test` feeds an `.eml` file into the real wiring — a temporary state DB and the
  delivery threshold set to `low` for that run (ADR-057). Bundled:
  `src/maildigest/data/selftest.eml`.
- Foreign data (folder names, Telegram chats, the model answer) reaches the terminal only
  filtered, or not at all (ADR-055).

## 3. Data model (binding for WP1)

```python
class RawMail(BaseModel, frozen=True):
    message_id: str | None          # header; None if missing
    dedupe_key: str                 # message_id or a fallback hash
    from_addr: str                  # "Display Name <address>"; name RFC 2047 decoded and
                                    # stripped of structural symbols, address from the raw header
    from_domain: str                # from the raw-parsed address, lowercase (never from the
                                    # display name — HC2-2)
    reply_to: str | None
    from_address: str               # addr_spec from the RFC 5322 parser, "" = unknown (O-3)
    reply_to_address: str | None    # ditto for Reply-To; None = no header, "" = unreadable
    reply_to_addresses: list[str]   # all Reply-To addresses from the parser (a mismatch if one differs)
    return_path_domain: str | None
    to_addrs: list[str]             # at most MAX_RECIPIENTS (200)
    subject_raw: str                # undecoded/decoded raw
    date: datetime | None
    auth_results_header: str | None # Authentication-Results, raw
    mime_bytes: bytes               # the complete raw mail (never leaves ingest+sanitizer!)
    size_bytes: int
    content_hash: str               # sha256(mime_bytes), the second dedupe criterion (ADR-079)
    id_collision: bool              # key taken, content different (ADR-079)
    ingest_failed: bool             # ingest could not evaluate the mail (O-1): straight to
                                    # fail-closed, never summarise

class AttachmentInfo(BaseModel, frozen=True):
    filename_sanitized: str
    declared_mime: str
    detected_kind: Literal["pdf", "text", "html", "unknown", "mismatch"]
    size_bytes: int
    processed: bool                 # True only for allowlist types under the limits
    extracted_chars: int            # 0 if not processed

class SanitizedMail(BaseModel, frozen=True):
    dedupe_key: str
    from_display: str               # sanitised display name
    from_domain: str
    subject: str                    # sanitised
    date: datetime | None
    body_text: str                  # sanitised plain text incl. [Link #n: domain] markers
    attachment_texts: dict[str, str]  # filename → extracted, sanitised text
    attachments: list[AttachmentInfo]
    links_found: list[str]          # defanged renderings, for the report/footnote only
    sanitization_report: SanitizationReport

class SanitizationReport(BaseModel, frozen=True):
    links_removed: int
    hidden_text_removed: bool
    control_chars_removed: int
    punycode_domains: list[str]
    mixed_script_domains: list[str]
    truncated: bool
    blocked_attachments: int
    reply_to_mismatch: bool
    return_path_mismatch: bool
    id_collision: bool              # a copy of RawMail.id_collision (ADR-079)
    forged_markers: int             # forged data-block markers (ADR-061, HC-5)
    auth_results: dict[str, str]    # e.g. {"spf": "pass", "dkim": "fail"}, best effort

class Summary(BaseModel):
    headline: str                   # ≤ 100 characters
    summary_text: str
    importance: Literal["high", "normal", "low"]
    importance_reason: str
    category: str
    attachment_summaries: dict[str, str]
    injection_suspected: bool

class CriticVerdict(BaseModel):
    phishing_risk: Literal["none", "low", "high"]
    risk_reasons: list[str]
    summary_accurate: bool
    notes: str

class DigestMessage(BaseModel, frozen=True):
    # fully formatted, already output-sanitised text, split if necessary
    parts: list[str]
    importance: Literal["high", "normal", "low"]
    is_warning: bool                # contains the phishing banner
    dedupe_key: str

class FailureNotice(BaseModel, frozen=True):
    dedupe_key: str
    from_domain: str
    subject_sanitized: str          # by the emergency sanitizer (ASCII printables only, truncated)
    stage: str                      # where it failed
    reason_class: str               # error class, no details or content
```

**Implementation notes (WP1, `models.py` — rationale in ADR-014):**

- The recurring `Literal` enumerations are exported as named type aliases and used by
  `config.py`/`pipeline.py` as well (semantically identical to the table above):
  `Importance = Literal["high","normal","low"]`,
  `PhishingRisk = Literal["none","low","high"]`,
  `AttachmentKind = Literal["pdf","text","html","unknown","mismatch"]`.
- All models run with `extra="forbid"`; unknown fields (e.g. from LLM JSON) are a validation
  error rather than a silent acceptance (I4).
- `Summary` and `CriticVerdict` are deliberately **not** frozen: the deterministic post-check
  in WP5/WP6 cleans fields of the untrusted LLM output. All other models are frozen.
- Count and size fields have `ge=0`, `Summary.headline` enforces `max_length=100`. Fields with
  a natural empty value (lists, dicts, `bool`, optional headers) have defaults so that ingest
  and sanitizer do not produce mandatory boilerplate; identifying fields (`dedupe_key`,
  `from_domain`, `body_text`, `mime_bytes`, …) stay required.
- **`RawMail` conventions for the unknown (WP2, ADR-020):** `from_domain` is the empty string
  when the `From` header holds no `localpart@domain` address or the header is unbalanced (an
  open comment or quoted string, S-2); `return_path_domain` is `None` in that case (so that the
  domain comparison in WP6 does not count two unknowns as a hit). `date` is `None` with a
  missing or unparsable `Date` header. `subject_raw` is the RFC 2047 decoded but unsanitised
  subject with header folding resolved. `auth_results_header` contains **all**
  `Authentication-Results` occurrences, joined with `\n`. `build_raw_mail` never throws on
  principle — a mail failing there would never reach the fail-closed path and would be lost
  silently (I6/F-OPS-3). If it does fail against expectation, `poll_once` builds the substitute
  `RawMail` (`ingest_failed = True`, readable fields only, a placeholder instead of
  `mime_bytes`) and the pipeline turns it into the metadata note immediately — status `failed`,
  error class `ingest_error` (O-1, ADR-020 addendum).
- **MIME depth in ingest (O-1, ADR-020 addendum):** `build_raw_mail` caps the parsed tree
  iteratively at `MAX_MIME_DEPTH` (32) levels **before** any serialisation; deeper subtrees are
  emptied (log `mail_mime_depth_capped`). Ordinary mail stays untouched and byte-identical.

## 4. Pipeline contract (`pipeline.py`)

```python
def process_mail(raw: RawMail, deps: PipelineDeps) -> PipelineResult: ...
# PipelineResult = Delivered | QueuedLow | FailedNotice (each with a status for state/db)
```

- Stages are injected as protocols (`Sanitizer`, `Summarizer`, `Critic`, `OutputComposer`,
  `Messenger`) → every stage is individually mockable.
- `mime_bytes` is no longer reachable after the sanitizer (the object is not passed on) — a
  structural safeguard for I1.
- Every exception of a stage is caught, classified, counted (retry policy WP8) and at worst
  ends as a `FailureNotice`.

**As of WP1 (`pipeline.py`) — details in ADR-011 to ADR-013:**

```python
Stage      = Literal["sanitize", "summarize", "critic", "compose", "deliver"]
MailStatus = Literal["delivered", "skipped_low", "failed"]

class MailRef(BaseModel, frozen=True):   # metadata copy that survives the sanitizer (I1)
    dedupe_key: str
    from_domain: str
    subject_sanitized: str               # emergency sanitizer, see FailureNotice in §3

@dataclass(frozen=True)
class PipelineDeps:
    sanitizer: Sanitizer;  summarizer: Summarizer;  critic: Critic
    composer: OutputComposer;  messenger: Messenger
    deliver_min_importance: Importance = "normal"
    progress: ProgressSink | None = None          # WP8: intermediate states into the state DB

@dataclass(frozen=True)
class Delivered:    dedupe_key: str; message: DigestMessage; status = "delivered"
@dataclass(frozen=True)
class QueuedLow:    dedupe_key: str; from_domain: str; summary: Summary; status = "skipped_low"
@dataclass(frozen=True)
class FailedNotice: notice: FailureNotice; notice_delivered: bool; status = "failed"
```

Stage protocols: `Sanitizer.sanitize(raw) -> SanitizedMail`,
`Summarizer.summarize(mail) -> Summary`, `Critic.review(mail, summary) -> CriticVerdict`,
`OutputComposer.compose(mail, summary, verdict) -> DigestMessage` **and**
`OutputComposer.compose_failure(notice) -> DigestMessage`, `Messenger.send(message) -> None`.
The pipeline's `Messenger` protocol is deliberately narrower than `messenger/base.py` (no
`healthcheck()`): the pipeline only demands what it calls.

Flow and decision points:

1. Before the sanitizer a `MailRef` is taken; after the sanitize call the `RawMail` reference
   is released with `del`. Stages 3–6 see `SanitizedMail` and `MailRef` — never
   `RawMail`/`mime_bytes` (I1).
2. `verdict.summary_accurate == False` ⇒ fail-closed with `stage="critic"`,
   `reason_class="summary_inaccurate"` (T8) — before any threshold comparison.
3. `phishing_risk == "high"` forces individual delivery and raises `importance` to at least
   `normal` (F-CRIT-2); otherwise `deliver_min_importance` decides between delivery and
   `QueuedLow`.
4. An error in a stage ⇒ `FailureNotice` + **one** delivery attempt of the note through
   `compose_failure`/`send`. If that fails too, `notice_delivered=False`; retries are WP8's
   business. `process_mail` never lets a stage exception out.
5. **As of WP8 (ADR-050):** after sanitize, after summarize and — crucially — **before**
   compose/send, the pipeline reports the intermediate state to `deps.progress`
   (`sanitized`/`summarized`/`checked`). The sink commits; if it throws, that counts as a stage
   error and ends fail-closed (`state_error`).
6. `reason_class` is derived from the exception **class name** (table in
   `pipeline._ERROR_CLASSES`), fallback `"<stage>_error"`. The exception text is never taken
   over (I5). The table was extended during the WP4 integration by
   `"LLMTransportError": "llm_transport_error"` — without that entry every HTTP/connection
   error of the LLM layer would have landed in the vague fallback (the behaviour was
   fail-closed before as well, only the label was coarse).

## 5. Configuration (`config.toml`, schema in `config.py`)

```toml
[general]
language = "de"              # language of the summaries
summary_length = "medium"    # short | medium | long
deliver_min_importance = "normal"  # low | normal | high
low_digest_time = "18:00"    # daily collected delivery (local time)
state_db = ""                # empty = state.db next to the configuration file (ADR-045)
log_level = "INFO"           # DEBUG | INFO | WARNING | ERROR (ADR-046)

[imap]
host = "imap.example.org"
port = 993
username = "mirror@example.org"
# password via MAILDIGEST_IMAP_PASSWORD or here (the file is 0600)
folder = "INBOX"
poll_interval_seconds = 120
move_processed_to = ""       # empty = only mark as read

[llm]
provider = "anthropic"       # anthropic | openai_compatible
model = "…"                  # mandatory field, no hardcoded default in the code
# api_key via MAILDIGEST_LLM_API_KEY
base_url = ""                # for openai_compatible / local servers
# max_tokens = 4096          # absent = no limit (ADR-085); only to cap the cost per call

[llm.critic]                 # optional override, otherwise as [llm]
# model = "…"

[summarizer]
instructions = ""            # the user's custom instructions (semi-trusted, I8)

[links]
footnote = false             # append the defanged link list as a footnote

[messenger]
active = "telegram"          # telegram | discord | signal

[messenger.telegram]
# token via MAILDIGEST_TELEGRAM_TOKEN
chat_id = ""

[messenger.discord]
webhook_url = ""             # careful: contains a secret → file 0600

[messenger.signal]
enabled = false
signal_cli_socket = ""

[limits]                     # defaults see SECURITY.md §4
max_mail_bytes = 26214400          # 25 MB
max_text_chars = 30000
pdf_max_input_bytes = 10485760     # 10 MB
pdf_max_output_chars = 50000
pdf_timeout_seconds = 20
pdf_time_budget_seconds = 30      # time budget of all PDF extractions of one mail (ADR-029)
max_mime_depth = 10
max_attachments_processed = 20
max_html_elements = 50000          # element budget of the HTML conversion per mail (ADR-084)
max_html_bytes = 1048576           # byte budget per mail, checked before parsing (ADR-084)
```

**Implementation notes (WP1, `config.py` — rationale in ADR-015):**

- Secret fields are `pydantic.SecretStr` (`imap.password`, `llm.api_key`,
  `messenger.telegram.token`, `messenger.discord.webhook_url`) and therefore appear neither in
  `repr()`/logs nor in error messages (I5).
- Env overrides (`MAILDIGEST_IMAP_PASSWORD`, `MAILDIGEST_LLM_API_KEY`,
  `MAILDIGEST_TELEGRAM_TOKEN`) are mirrored into the raw dict **before** validation; env beats
  the file, empty values are ignored.
- All sections are `extra="forbid"` — a typo is an error, not a silent omission. Errors are
  translated into a `ConfigError` with a German, field-specific, multi-line message
  (`[section] field: <reason>`).
- `[llm.critic]` is an override with inheritance: `Config.critic_model()`, `critic_provider()`,
  `critic_base_url()`, `critic_max_tokens()` return the override or the value from `[llm]`.
- `[llm] model` deliberately has no default (a mandatory field); the other values above are the
  actual code defaults.
- Besides `load_config(path)` there is `load_config_from_dict(data)` for the CLI (WP9) and
  tests.

**Still open (from WP7; deliberately untouched in WP9):** `[messenger.signal]` has no recipient
field, so the adapter delivers to "Note to Self" (ADR-039). Delivering to another number would
require `[messenger.signal] recipient = ""` — its own ADR, because it extends the config
schema.

**Additions from WP4 (ADR-025):**

- `[llm] api_key` applies to **both** roles. If the critic uses a different *cloud* provider
  than the summarizer, a shared key is not enough; for the normal case (critic on a local
  server without a key, or the same provider) that is unproblematic. An `[llm.critic] api_key`
  is deliberately not yet provided.
- A section `[llm.summarizer]` deliberately does **not** exist: the summarizer is the normal
  case and uses `[llm]` directly; only the critic is overridable via `[llm.critic]`. The
  statement to the contrary in PLAN.md WP4 is stale — ARCHITECTURE is authoritative here, and
  `extra="forbid"` would reject `[llm.summarizer]` as a config error.
- `base_url` is not restricted to `https`, because local servers (Ollama/vLLM) are addressed
  over `http://localhost`. For cloud providers, `https` is a matter of configuration; since WP9
  `connect-llm` warns on stderr when the base URL is neither `https` nor local.

## 6. Error & retry policy (implemented in WP8)

- IMAP errors: reconnect with exponential backoff (max. 10 min), the loop continues.
- LLM errors: 429 and 5xx are retried up to 3 times **inside** the LLM layer with backoff
  (1 s/2 s, `Retry-After` takes precedence); timeouts are not (otherwise one mail blocks for up
  to 3 minutes). If the error persists, the layer throws
  `LLMRateLimited`/`LLMTransportError`/`LLMTimeout`, and the WP8 stage retry policy decides on
  further attempts, ending in a `FailureNotice` (ADR-023).
- Schema invalidity: 1 repair retry (in `llm/schema.py`), then a `FailureNotice`.
- **Three retry levels, multiplicative** (clarification WP11, CT-16c): (1) transport
  (`llm/_http.py`, `MAX_ATTEMPTS = 3`) — but **only** on HTTP 429 and 5xx. (2) Schema
  (`llm/schema.py`) — exactly one additional repair call when the answer is not schema-conformant
  JSON. (3) Stage (`runner.py`, ADR-050) — 3 attempts per LLM stage, but only on timeout, rate
  limit and transport error; `LLMInvalidResponse` is deliberately **not** retried here. From
  this the observable numbers follow: a persistent HTTP 500 → 3 × 3 = 9 requests; a valid HTTP
  response with non-JSON content → 2 requests (initial call + repair, then no stage retry); a
  transport error without an HTTP status → 3 requests. The value "3 attempts" named in F-SEC-7
  refers to the **stage** level.
- **System clock (HC-25):** all delivery deadlines compute with the wall clock. If it jumps,
  "five attempts" still holds, but "over at most one hour" does not: an age that does not fit
  the retry plan is ignored rather than believed. Details in the ADR-048 addendum.
- Messenger errors: 5 attempts over at most 1 h (the message is fully sanitised and may be
  resent from the DB queue), then `failed` + log. The parts of a multi-part message go to the
  adapter individually; if delivery aborts at part *n*, the queue row is trimmed to the parts
  from *n* onwards and the retry resumes there (ADR-066). At-least-once per ADR-008 remains:
  exactly the part whose confirmation did not arrive can come twice — the parts before it
  cannot.
- Process crash: state in SQLite such that a restart is idempotent (commit the status before
  sending ⇒ at worst one duplicate delivery, never a loss — ADR-008).

**Concretisation WP8 (ADR-048, ADR-050):**

| Error location | Policy | Place in the code |
|-----------|---------|-------------|
| IMAP | reconnect, backoff 5 s → 600 s, the loop continues | `ingest.backoff_delay`, `Runner.run_forever` |
| LLM (timeout/429/transport) | provider-internal max. 3 (429/5xx), above that 3 stage attempts with 2 s/4 s | `llm/_http.py`, `runner.RetryingSummarizer`/`RetryingCritic` |
| LLM schema | 1 repair attempt, **no** stage retry | `llm/schema.py` |
| Critic `summary_accurate = false` | immediately fail-closed, no retry | `pipeline.process_mail` |
| Delivery | 5 attempts, 60/300/900/2100 s, hard bound 1 h, then `failed`/`delivery_failed` | `delivery.OutboxMessenger` |
| Clock jump during delivery | age clamped to `>= 0`; the one-hour limit only applies with a plausible age (`age_is_plausible`), otherwise the attempt counter decides and `first_queued_at` is reset. A due time more than 2 h in the future is corrected to `now` (`outbox_clock_skew_corrected`) | `delivery._handle_failure`, `StateDB.outbox_due` (ADR-048 addendum, HC-25) |
| State DB not writable | stage error ⇒ metadata note (`state_error`) | `pipeline._record` |

## 7. Message format (finalised in WP7, `output/composer.py`)

```
⚠️ SUSPECTED PHISHING: <risk_reasons, comma-separated, max. 5>  ← only at phishing_risk = high
📧 <headline> [important]                                       ← tag only at importance = high
From: <from_display> (<from_domain>) · <DD.MM. HH:MM>           ← without a display name, domain only;
                                                                  without a Date header `date unknown`
<summary_text>
— <file>: <1–2 sentences per processed attachment>
📎 Not processed: <file (size)>, … [and N more]                 ← all AttachmentInfo with processed = false
🔍 Notes: <injection flag; encrypted; Message-ID collision; auth failures; punycode;
           mixed scripts; HTML part differs from the text part; hidden text removed from
           the HTML; Reply-To/Return-Path divergence; text truncated;
           critic reasons at risk = low>
<link footnote (defanged)>                                      ← only with [links] footnote = true
```

The **frame is English and language-independent** (ADR-083); `[general] language` steers only
the fields filled by the model. The literal contract is in docs/SPEC-CLI.md §6 and is bound
mechanically to the composer through `tests/unit/test_hc14_spec_literals.py`.

The **prefixes of this format are reserved**: the composer appends them after the field scrub,
and `output/sanitizer.neutralize_markup()` prevents model-supplied text from forging them at
the start of a line (ADR-062, CT-8). The link footnote is likewise appended by the composer —
it belongs to the delivery and not in the prompt (ADR-072, CT-14).

Binding additional rules (WP7):

1. **All** domains and filenames appear with broken dots (`stadtwerke-x[.]de`,
   `invoice[.]pdf`, including inside `[Link #n: …]` markers) — Telegram and Discord would
   otherwise autolink bare domains (T7, ADR-036). The example above is to be read accordingly.
2. Individual limits per untrusted field (headline 120, summary 3000, attachment summary 400,
   critic reason 200, display name 80, domain 100, filename 80 characters; max. 10 attachments
   listed, max. 5 banner reasons), truncation with `…` (ADR-040).
3. Split at line boundaries to the limit of the active messenger (Telegram 4096, Discord 2000,
   Signal 2000), without a part counter. If a **single line** has to be cut hard, every
   fragment after the first carries the continuation prefix `… ` (U+2026 + space). It counts
   towards the limit. Reason: only this cut produces a line start that no layer has checked —
   without the prefix a part could begin with a forged program line (HC-6, addendum to
   ADR-062). Cuts at line boundaries do not need it: those line starts have already been seen
   by `neutralize_markup` or were produced by the composer itself.
4. Fail-closed note (`compose_failure`, F-OPS-3) — five lines, metadata only:

   ```
   ⚠️ This mail could not be processed safely — no content delivered.
   From: <from_domain>
   Subject: <emergency-sanitised subject>
   Stage: <stage> · Reason: <reason_class>
   Open your real mailbox to read it.
   ```

   `importance = normal`, `is_warning = false`; stage and reason are normalised to
   `[A-Za-z0-9_-]` labels (I5).

The collected digest (daily, `DigestComposer.compose_low_digest`, F-SUM-5): **one** message,
grouped by category (largest group first), one line per mail
`• <headline> (<from_domain>)`:

```
🗂 12 low-priority mails: 8 newsletter, 3 benachrichtigung, 1 other

newsletter (8):
• Weekly review, week 36 (news[.]example[.]org)
…
... and N more                                   ← from 60 listed mails onwards
```

The message is produced like any other through `_finalize()` (final pass + split),
`importance = low`, `is_warning = false`, `dedupe_key = "low-digest"`. It is delivered on the
first cycle at or after `[general] low_digest_time`; a missed time is caught up the same day,
and an empty queue produces no message (ADR-049).
