# MailDigest — Requirements

> Binding requirements list. Every requirement has an ID (F = functional,
> NF = non-functional, SEC = security-functional). Agents reference these IDs in commits,
> tests and ADRs. Changes to this document only with an ADR in DECISIONS.md.
>
> Status: `open` → `in-progress (WPx)` → `done (WPx)`.

## 1. Functional requirements

| ID | Requirement | Source | Status |
|----|-------------|--------|--------|
| F-ING-1 | The tool reads mail from a dedicated mirror mailbox over IMAPS. It writes and deletes nothing there beyond the seen flag and an optional move into a `Processed` folder. | user | done (WP2, corrected in WP11 — ADR-064: raw UID commands, no EXPUNGE; verifiable for the first time) |
| F-ING-2 | Every mail is processed exactly once (dedupe via Message-ID **and** a content hash, persistent state). "Exactly once" applies per mail, not per Message-ID: two mails with different content and the same header are a collision and both get processed (ADR-079). | derived | done (WP2, extended in the fix round, HC-10) |
| F-ING-3 | The mirror mailbox is set up through a CLI command (`connect-mail`) with a connection test and instructions for setting up forwarding. | user | done (WP9) — prompts, IMAPS connection test, folder choice from the server list (`ImapClient.list_folders`), forwarding instructions for Gmail/posteo/mailbox.org/generic; nothing is saved until the test passes. Since 2026-09-09 additionally (ADR-075): an explanation of the term IMAP host with examples, translation of a typed mail address into the host, provider-specific instructions for obtaining a password, immediate abort for providers without password login (Outlook.com, Proton) and a provider-specific hint after a failed login |
| F-SUM-1 | One LLM instance ("summarizer") produces a structured summary per mail (headline, text, category) in a configurable language and length. | user | done (WP5) |
| F-SUM-2 | The summarizer classifies every mail as `high`/`normal`/`low` importance, with a reason. | user | done (WP5) — `importance` + `importance_reason` come from the summarizer; the delivery threshold is evaluated by `pipeline.process_mail` |
| F-SUM-3 | The user can adapt the behaviour with custom instructions (what to summarise, how detailed, what counts as important). | user | done (WP5) — `[summarizer] instructions` as a labelled semi-trusted block (I8, ADR-031) |
| F-SUM-4 | Content of processable attachments (v0.1: PDF text only) is summarised along with the mail; the file itself is never delivered. | user | done (WP5) — attachment text sits in the data block, `attachment_summaries` is limited to attachments actually extracted |
| F-SUM-5 | Mail below the configured importance threshold is not delivered individually but collected into a daily digest. | derived | done (WP8) — `low_digest_queue` + `DigestComposer.compose_low_digest`, delivery from `[general] low_digest_time` (ADR-049); a critic `high` stays exempt (F-CRIT-2) |
| F-CRIT-1 | A second, independent LLM instance ("critic") judges mail + summary for phishing/scam risk (`none`/`low`/`high`) and for factual correctness of the summary. | user | done (WP6) — `agents.critic.CriticAgent` with its own system prompt, its own provider (`[llm.critic]`) and without custom instructions (ADR-042) |
| F-CRIT-2 | At `high` risk the delivery carries a clear warning banner and is never moved into the low digest. | derived | done (WP6 + WP7) — verdict from WP6, promotion to `importance = normal` in `pipeline.process_mail`, banner in `output/composer.py`; chain test sanitizer → critic → composer in `tests/unit/test_critic_corpus.py` |
| F-CRIT-3 | Deterministic signals (Reply-To≠From, domain discrepancies, punycode, auth results, blocked attachments) are computed in code and handed to the critic as facts. | derived | done (WP3 + WP6) — `agents.critic.collect_signals` from the `sanitization_report`, tested LLM-free; hard signals raise the risk level even without a model (ADR-043); several independent forgery signals including at least one hard one raise it to `high` and trigger the banner (WP11, ADR-063) |
| F-MSG-1 | Delivery to at least Telegram and Discord; Signal optionally via signal-cli. Adapter architecture for further messengers. | user | done (WP7 + WP8 + WP9) — adapters, wiring in the daemon through the queue (ADR-048) and setup via `connect-messenger` |
| F-MSG-2 | Messenger setup through the CLI (`connect-messenger`) including a test message. | user | done (WP9) — Telegram including chat-ID discovery via `getUpdates`, Discord webhook, Signal socket; health check + test message through `DigestComposer.compose_plain` (ADR-054) |
| F-LLM-1 | The LLM provider is interchangeable: at least the Anthropic API and OpenAI-compatible endpoints (which covers local models). Selection + model name via config. | user | done (WP4) Since 2026-09-09 additionally `provider = "none"` as the default: operation entirely without a language model (ADR-076), so that setup succeeds without signing up with a third party; `connect-llm` offers the modes as a list to pick from, including three providers with a free tier and the local variant. |
| F-LLM-2 | Provider setup through the CLI (`connect-llm`) including a test call. | user | done (WP9) — provider/model choice, key from a prompt or `MAILDIGEST_LLM_API_KEY`, test call with `max_tokens = 16`; the model's answer is never displayed (ADR-055) |
| F-OPS-1 | `maildigest run` runs as a long-lived process (polling); `--once` processes once and exits (cron-friendly). | derived | done (WP8 + WP9) — `run`/`run --once` through the WP8 runner, JSON logs on stdout, summary line on stderr, SIGINT/SIGTERM shutdown |
| F-OPS-2 | `maildigest test` performs an end-to-end self-test with a sample mail. | derived | done (WP9) — bundled `.eml` or `--eml <path>`, real wiring on a temporary state DB, `--dry-run` shows the message instead of sending it (ADR-057) |
| F-OPS-3 | Mail and attachments that cannot be processed produce a metadata note to the messenger (fail-closed), so nothing is ever lost silently. | derived | done (WP1 + WP7 + WP8) — note in `output/composer.compose_failure`, delivery (of the note as well) through the persistent queue with 5 attempts; crash test in `tests/integration/test_runner_e2e.py` |

## 2. Security requirements (testable, basis for cold testing)

| ID | Requirement | Status |
|----|-------------|--------|
| F-SEC-1 | No LLM ever receives raw HTML, raw MIME parts or attachment binaries — only sanitised plain text (invariant I1). | done (WP3 + WP5 + WP6) — both agents structurally accept only `SanitizedMail`/`Summary` |
| F-SEC-2 | LLM calls are text-in/text-out without tools, function calling or network access in the model context (I2). | done (WP4 + WP5 + WP6) — summarizer and critic only ever call `complete_json` via `LLMProvider.complete` |
| F-SEC-3 | Delivered messages never contain clickable URLs, Markdown/HTML links, file attachments or executable content. URLs at most defanged or as domain text (I3). | done (WP7) — field scrub + final pass + property tests; Telegram without `parse_mode`, Discord without embeds; tightened in WP11 for line-leading Markdown, edge underscores and mass pings (ADR-062) |
| F-SEC-4 | Attachments are handled by allowlist: only `text/plain` and `application/pdf` are processed for content; everything else — **including `text/html`** — is reported as metadata only. The MIME type is verified via magic bytes. | done (WP3; wording corrected in WP11 — CT-16d: the line wrongly listed `text/html`, contradicting SECURITY §4, SPEC-CLI §7.3 and the actual behaviour) |
| F-SEC-5 | Instructions in the mail content ("ignore previous instructions", hidden text, etc.) must not change the behaviour; suspicion is flagged and shown to the user. | done (WP5 + WP7, tightened in WP11) — prompt hardening, deterministic post-checking of the model output **and** model-independent evidence from the mail (forged data-block markers, clusters of invisible characters, literal instructions to a model) set `injection_suspected` (ADR-061); hidden text appears as a note of its own. Cold-test evidence in WP11 (CT-6) |
| F-SEC-6 | LLM output is schema-validated and passes through a deterministic output sanitizer before sending (I4). | done (WP5 + WP6 + WP7) — `Summary` and `CriticVerdict` are schema-enforced, post-checked on the agent side and scrubbed again in the composer |
| F-SEC-7 | Errors in sanitizer/LLM/critic lead to fail-closed behaviour: a metadata note instead of unchecked content (I6). | done (WP1 + WP8) — including stage retries (3 LLM attempts at **stage** level, ADR-050 — the three multiplicative retry levels are broken down in ARCHITECTURE §6, CT-16c) and state errors (`state_error`); demonstrated end to end in `tests/integration/test_runner_e2e.py` |
| F-SEC-8 | Secrets never appear in prompts, logs or the database; the config file is created with mode 0600 (I5). | done (WP1 + WP4 + WP7 + WP9) — `SecretStr` in the config, no secrets in prompts/logs/DB, `ConfigFile.save()` creates the file with `0o600` and resets the mode on every write; secrets deliberately have no command-line options (ADR-056). Black-box evidence in WP11 |
| F-SEC-9 | Attachment text extraction runs in a resource-limited subprocess (timeout, memory, input/output size) (I7). | done (WP3) |
| F-SEC-10 | Zero-width and bidi control characters are removed; punycode/homoglyph domains are flagged. | done (WP3) |

## 3. Non-functional requirements

| ID | Requirement | Status |
|----|-------------|--------|
| NF-1 | Lightweight: Python ≥ 3.11, runtime dependencies ≤ 8 packages, SQLite as the only store, runnable on a small VPS or Raspberry Pi. | done (WP12) — six runtime packages (`imap-tools`, `httpx`, `pydantic`, `beautifulsoup4`, `lxml`, `pdfminer.six`), SQLite from the stdlib, `hypothesis` is dev-only. **Not** established: running on a Raspberry Pi has never been measured |
| NF-2 | A new runtime dependency only with an ADR. | done (WP12) — the six packages are listed with a rationale in PLAN §3 and in docs/DECISIONS.md; since WP0 none has been added without an ADR |
| NF-3 | Configuration entirely through a `config.toml` + env vars; no database migration tooling. | done (WP1 + WP8 + WP9) — every field is documented with its default in docs/SPEC-CLI.md §5 and reconciled against the schema by a test; DB schema upgrades happen additively in code (ADR-048) |
| NF-4 | Processing latency per mail < 60 s under normal conditions (excluding LLM outliers). | **open** — never measured. Without a real LLM API there is no defensible number; the program's own stages (sanitizer, composer) are in the millisecond range, so latency is effectively the sum of two model calls. Stays open until the first production run |
| NF-5 | Logs structured, without mail content and without PII beyond sender domain + hashed Message-ID. | done (WP2 + WP8) — JSON lines on stdout, level from `[general] log_level`, non-serialisable `extra` values are reduced to their type name; tracebacks only at DEBUG (ADR-046/047) |
| NF-6 | Test coverage: ≥ 90 % for `sanitize/` and `output/`, ≥ 80 % overall (as of WP10). | done (WP10, as of WP12) — `sanitize/` 98 %, `output/` 99 %, 97 % overall across 1278 tests; measurements and remaining gaps in docs/TESTING.md §5, state after the fix round (1565 tests) in §7. Extended with property-based tests (hypothesis, dev-only, ADR-058), fault injection at every stage and the findings log HT-1…HT-12 |
| NF-7 | Documentation duty: REQUIREMENTS/ARCHITECTURE/SECURITY/DECISIONS are maintained in every WP; SPEC-CLI.md is the complete CLI contract. | done (WP12) — SPEC-CLI.md is reconciled mechanically against argparse and the config schema by `tests/unit/test_spec_cli.py`; in WP12 the whole documentation was checked against the actual state and SECURITY §7 was filled in |
| NF-8 | Two independent test runs: hot (white box) and cold (black box by an agent without code access) per TESTING.md. | **in-progress** — hot completed (WP10, §5), first cold run completed (WP11, §6: 16 findings, all ≥ medium fixed and covered by a regression test). The **second** cold round required by TESTING §3 after the `high` findings CT-6/CT-9 is still outstanding — the only open point of this requirement, named in README "Limitations of this version", CHANGELOG and SECURITY §7.2. The final test round of September 2026 (TESTING §7, 38 findings, all fixed with no deliberately open remainder) does **not** satisfy the checkbox — its skeptic review had code access — but it did create the precondition for it: since ADR-083 the CLI contract again describes the actual output verbatim |

## 4. Explicitly out of scope (v0.1)

- No access to the real mailbox (mirror only).
- No dialogue with the bot and no actions from the messenger. **Exception since
  2026-09-09 (ADR-077):** a command channel with the fixed word list `/digest` and
  `/status`, only from the configured chat. Free text is discarded and never reaches a
  language model. Since ADR-078 the channel is **on** by default; it can be switched off
  for group chats.
- No OCR / no image content analysis (known limitation: image phishing is only reported as
  an unprocessed attachment).
- No decryption of PGP/S-MIME. Such a mail has been **recognised and named** as encrypted
  since ADR-082 (note line "encrypted (PGP/S-MIME) — content not readable by design")
  instead of looking like a mail with no content.
- No multi-user or multi-mailbox operation.
