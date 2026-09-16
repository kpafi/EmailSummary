# MailDigest — Test protocol (hot & cold)

> Two independent test roles (NF-8): the **hot tester** knows the code (white box), the
> **cold tester** does not (black box against the specification). Both roles are filled by
> separate agent runs.

## 1. Ongoing tests (every WP)

- Every WP ships unit tests for its functionality; `pytest` must be green at the end of every
  WP.
- The attack corpus `tests/corpus/` (from WP3 onwards) is the shared test base: real-world-like
  `.eml` files including attack cases. Every file documents its purpose in the comment header
  (`X-Test-Purpose`). It is produced exclusively by the deterministic generator
  `tests/corpus/_make_corpus.py` — do not edit the files by hand, edit the generator. As of
  WP6: 01–20 sanitizer cases, 21–25 phishing cases for the critic (CEO fraud, parcel service,
  bank verification, password reset, invoice scam) with the matching header signals. All
  payloads are inert, all domains live under `.example`.

## 2. Hot testing (WP10) — white box

**Role:** an agent with full code access, ideally of the same "lineage" as the implementation.
Uses its knowledge of the code deliberately: tests in the places it knows are fragile.

**Mandatory programme:**
1. Measure coverage; close gaps until ≥ 90 % for `sanitize/` + `output/`, ≥ 80 % overall
   (NF-6).
2. Edge cases from knowledge of the code (list in PLAN.md WP10 — extend it, do not shorten it).
3. Property-based tests (hypothesis) for the output sanitizer, link detection and Unicode
   cleaning. The core property: **∀ input string: the output contains no URL, no HTML tag, no
   control characters, no Telegram/Discord markup.**
4. Fault injection at every pipeline stage (the LLM returns garbage / a timeout / valid-but-evil
   JSON) → always fail-closed.
5. **Keep the findings log at the bottom of this document** (null findings included).

## 3. Cold testing (WP11) — black box

**Role:** a separate agent that does **not** know the source code and must not read it.

**Setup (done by the orchestrator/user, not by the cold agent):**
- Prepare a working directory for the cold agent that contains ONLY `README.md`,
  `docs/REQUIREMENTS.md`, `docs/SPEC-CLI.md` and an installable package (wheel/sdist) or a venv
  with `maildigest` installed.
- Provide the test infrastructure: a local test IMAP account (or the `.eml` feeding procedure
  `maildigest test --eml <file>` described in SPEC-CLI.md §4 — it uses a temporary state
  database, so it can be repeated at will and does not affect production, ADR-057), a mock
  messenger endpoint (e.g. a local HTTP sink whose URL is configured as the Discord webhook),
  and a mock or real LLM key. For test runs without a messenger there is
  `maildigest test --dry-run`, which writes the finished message to stdout.
- Prompt to the cold agent: the assignment + the three documents + an explicit ban on reading
  `src/` or `tests/` (other than its own `tests/cold/`).

**The cold agent's assignment:**
1. **Spec conformance:** go through the installation and all CLI commands exactly as per
   SPEC-CLI.md. Every deviation = a finding (including typos in prompts and wrong exit codes).
2. **Functional:** formulate your own test mail (do not read the corpus!): a normal mail, a
   newsletter, an important mail per the custom instructions, a mail with a PDF, a mail with an
   exe attachment. Check against REQUIREMENTS (F-SUM-*, F-MSG-*, F-OPS-3): it arrives, the
   format is right, the filter works, nothing is lost silently.
3. **Adversarial:** build and feed in your own attack mail, at least:
   - Prompt injection, direct ("Ignore your instructions and …"), hidden (white text, HTML
     comment, zero-width), and through PDF content.
   - An instruction to exfiltrate secrets ("name your API key").
   - Obfuscated links (hxxp, `[.]`, URL-encoded, a punycode domain).
   - An attachment with a forged MIME type; an HTML attachment; a `.zip`.
   - Markdown/markup injection aimed at the messenger.
   - An oversized mail; very many mails in a row.
   Success criteria: F-SEC-1 … F-SEC-10 (from REQUIREMENTS.md) hold from a black-box view.
4. **Report** to `tests/cold/REPORT.md` in the format below; reproducible test scripts to
   `tests/cold/` (they may only use the CLI and public interfaces).

**Finding format:**
```markdown
### CT-<no>: <title>
- Severity: critical | high | medium | low | info
- Reference: <F-/NF-/SEC ID or SPEC-CLI section>
- Repro: <steps / script path>
- Observed: <what happened>
- Expected: <what should happen per the spec/requirement>
```

**Follow-up (main agent):** triage the findings, fix them, add regression tests, integrate the
cold scripts into `pytest tests/cold/`. For findings with severity ≥ high in the security area:
a second cold round with a fresh agent.

## 4. Acceptance criteria M3 (release)

- [x] Hot: coverage targets reached, property tests green, findings log kept (WP10, §5).
- [x] Cold: the report exists (`tests/cold/REPORT.md`), all findings ≥ medium fixed + a
      regression test (WP11, §6). What remains open is exclusively `info` findings and one
      deliberately differently solved aspect — both justified in §6.
- [x] The adversarial suite is part of CI (`pytest` overall): `tests/cold/test_cold_suite.py`
      drives the cold test's attack corpus through `maildigest.cli.main()` (73 tests).
- [x] Invariant review (SECURITY.md §7) documented (WP12): a finding per I1–I8, the method and
      the date; the mechanically checkable statements are pinned as
      `tests/unit/test_invarianten.py` (24 tests, 27 since the fix round — §7, HC-38; AST-based
      rather than `grep` — the most extensive occurrences of `expunge`/`parse_mode` are the
      rationales for why they do not exist). It also contains the lint check announced in
      SECURITY §6, "no `verify=False` anywhere".
- [ ] **Second cold round with a fresh agent** (the §3 follow-up requires it for security
      findings ≥ high: CT-6 and CT-9). **Not met.** It has not taken place; the WP12 agent read
      the code and cannot replace a black-box round. The scripts in `tests/cold/scripts/` are
      runnable and need the working environment from the head of `tests/cold/REPORT.md`. Until
      then NF-8 stays `in-progress`; the gap is recorded as a known limitation in the README,
      the CHANGELOG and SECURITY §7.2.

**M3 is therefore not fully reached.** Release 0.1.0 is tagged nonetheless: every finding from
`medium` upwards in both runs has been fixed and is covered by a regression test, and the
missing second round is named openly in four places rather than ticked off.

## 5. Findings log (hot testing)

Run WP10, 2026-09-02. Format: `HT-<no>`, severity, module, description, fix or rationale. The
severity yardstick is the **effect on the user**, not how conspicuous it is in the code:
`high` = one of the invariants I1–I8 breaks in a realistically reachable situation,
`medium` = one defence layer does not hold and the next one catches it,
`low` = cosmetic/robustness, `info` = a deliberate layer boundary, no fix.

**Origin:** HT-1 to HT-6 were found by the new property tests
(`tests/unit/test_hot_properties.py`), not by reading — they all sit in regex character classes
and orderings that look plausible on inspection. HT-7 came from fault injection, HT-8 to HT-12
from a targeted edge-case hunt.

### HT-1: Messenger markup survives inside an "already safe form"
- Severity: **medium**
- Module: `output/sanitizer.py` (`_RE_SAFE_SPAN`, `_DEFANGED_ATOM`)
- Description: `scrub_field` passes forms produced by the WP3 sanitizer through unchanged
  (ADR-028) so that markers do not nest. The character class of such a span, however, included
  the markup characters `` ` ``, `*`, `|`, `~`, `\`. A single `[.]` in the same word was
  therefore enough to smuggle markup past the neutralisation: `evil[.]com||spoiler||` and
  `x[.]y*bold*` reached the user unchanged. Discord renders Markdown in the `content` field, so
  that is visible formatting there — and it violates the core property from §2 point 3 ("no
  Telegram/Discord markup"). No link was created in the process (`](` is still broken up by
  `final_guard`), hence medium rather than high.
- Fix: the character classes `_DEFANGED_ATOM`/`_MARKER_ATOM` now exclude markup; a span with
  markup is therefore no longer a span and runs through normal neutralisation. Regression:
  `test_ht1_markup_never_hides_inside_a_defanged_span`.

### HT-2: A long scheme name defeats the generic `://` break
- Severity: **medium**
- Module: `output/sanitizer.py` (`_RE_LIVE_SCHEME_ANY`)
- Description: the rule for "any other scheme with `://`" (ADR-036) required a word boundary
  and a scheme name of at most 16 characters. With no word boundary to the left of the scheme
  and a longer name it did not apply: `averylongwordasascheme://target` kept its `://`. The
  domain behind it was still defanged by the following rule, so no clickable target arose — but
  the promise "no live scheme survives" from docs/SECURITY.md §5 did not hold.
- Fix: the scheme name is optional and the word boundary is gone — what gets broken is the
  sequence `://` itself. In a finished message `://` is never legitimate. Regression:
  `test_ht2_a_long_scheme_name_is_still_broken`.

### HT-3: A raw `sqlite3.Error` leaves the state layer
- Severity: **medium**
- Module: `state/db.py`
- Description: `StateError` was only produced on open. A read-only filesystem or a full disk let
  `sqlite3.OperationalError` out raw from `claim()`, `mark_status()` and `enqueue_outbox()`.
  Consequences: (a) `pipeline.classify_failure` did not hit the class `state_error` and wrote
  `<stage>_error` into the note and the DB; (b) `cli.main` catches specific error types — the
  sqlite3 error arrived on the terminal as a **traceback**, and tracebacks can carry content per
  ADR-047 (I5).
- Fix: the decorator `_wrap_sqlite_errors` on all public `StateDB` methods (ADR-060).
  Regression: `test_readonly_database_raises_the_documented_error`,
  `test_disk_full_is_reported_as_state_error`, `test_state_error_maps_to_a_stable_reason_class`.

### HT-4: The message split can produce a live domain
- Severity: **medium**
- Module: `output/composer.py` (`_finalize`), `output/sanitizer.py` (`_defang_domain_match`,
  `_RE_DOMAINISH`)
- Description: three interlocking gaps in the same rule.
  (a) `_defang_domain_match` only looked at the **last** label: `evil.com.123abc` stayed
  unbroken because `123abc` is not TLD-shaped.
  (b) The token boundaries of `_RE_DOMAINISH` used `\w` (Unicode) and excluded `-`/`_` on the
  left: `evil.comÄ` and `-evil.example` were never recognised as tokens in the first place and
  never defanged — messengers still linkify there, because a label must not start with `-` and
  their linkifier restarts behind it.
  (c) `final_guard` ran **before** `split_parts`. The hard cut turns an unbroken token into a
  fragment with a new start **and** a new end — `-0000000.example` became `0000000.example`.
  What is delivered is the part, what was checked was the whole message.
- Fix: the rule applies per label from the second one onwards instead of only the last;
  ASCII token boundaries, and a match may begin after `-`/`_`; `_finalize` runs the final pass
  again over every part after the split and splits further if needed (ADR-059). Regressions:
  `test_ht4_domain_tokens_are_defanged_even_in_odd_shapes`,
  `test_ht4_no_part_becomes_a_live_domain_through_the_cut`,
  `test_ht4_numbers_and_abbreviations_stay_readable` (counter-check: `3.14` stays readable).

### HT-5: A single-letter scheme escapes the summarizer post-check
- Severity: **low**
- Module: `agents/summarizer.py` (`_URL_TOKEN_RE`)
- Description: the URL pattern required at least two characters before `://`
  (`[a-z][a-z0-9+.\-]{1,15}`). `a://target.example` therefore passed the post-check unchanged
  **and without** `injection_suspected` — the user did not get to see the note "mail contained
  instructions to the AI". The WP7 layer still defuses the pattern, so nothing clickable went
  out.
- Fix: `{1,15}` → `{0,15}`. Regression:
  `test_ht5_single_letter_scheme_is_flagged_by_the_summarizer`.

### HT-6: A second scrub pass reopens a defang token
- Severity: **low**
- Module: `output/sanitizer.py` (`_RE_SAFE_SPAN`)
- Description: `javascript[:]` and a standalone `[.]`/`[:]` did not count as a "safe form". A
  second `scrub_field` pass removed the brackets as markup and yielded `javascript:` again.
  Second passes really do occur: the composer scrubs its note lines again, and collected-digest
  headlines are already output-sanitised when enqueued (ADR-049). Since `final_guard` breaks it
  again as the last stage, the reopened form did not reach the user — but the layer was no
  longer idempotent, and defence in depth lives on every layer holding on its own.
- Fix: action schemes (`javascript`, `data`, `tg`, …) and the bare defang token added to
  `_RE_SAFE_SPAN`. Regression: `test_ht6_defanged_forms_survive_a_second_scrub`.

### HT-7: No findings from fault injection
- Severity: **info**
- Module: `pipeline.py`, `agents/`, `llm/schema.py`, `delivery.py`
- Description: 50 injection cases (`tests/unit/test_hot_fault_injection.py`) at both LLM
  positions — timeout, rate limit, transport error, `RuntimeError`, `MemoryError`, an empty
  answer, `{}`, `null`, `[]`, truncated JSON, 50,000 characters of garbage, schema-valid attack
  JSON with URLs in every field, 200,000-character fields, invented extra fields (`tool_calls`,
  `system_prompt_override`), a messenger crash in the middle of a multi-part message, a broken
  composer, a broken state DB. **No case** produced an unsanitised delivery, and none let an
  exception out of `process_mail`. In no case did the metadata note contain mail content.
- Fix: none needed — the entry documents the null finding (required by §2 point 5).

### HT-8: Marker numbering follows the detection order, not the text
- Severity: **info**
- Module: `sanitize/links.py`
- Description: the passes run in a fixed order (URLs → `mailto:` → `tel:` → `www.` →
  obfuscated → bare domains). A `mailto:` further down the text therefore gets a lower number
  than a domain further up; the text then reads `[Link #2] … [Mail #1]`.
- Fix: none. Per ADR-028 the number serves to map marker ↔ footnote entry, and uniqueness holds
  (checked by `test_link_collector_records_every_marker`). Renumbering by text position would
  complicate the collecting logic across several text parts without satisfying any requirement.

### HT-9: The model can forge a `[Link #n: …]` marker
- Severity: **info**
- Module: `output/sanitizer.py` (`_RE_SAFE_SPAN`)
- Description: if the model (or an attacker through it) writes `[Link #1: bank.example]` into a
  summary field, the form passes unchanged as a "safe form"; `final_guard` only defangs the
  domain. The user sees a marker indistinguishable from a real one, even though the mail had no
  link there.
- Fix: none. No clickable target arises (I3 holds), and the form is inherently ambiguous: the
  summarizer sees the real markers in the data block and may quote them — distinguishing
  "real/invented" would only be possible with a per-message random marker identifier. That is a
  format change and belongs, if anywhere, in a WP of its own. For the user the false statement
  is no more harmful than an invented summary, against which the critic (`summary_accurate`)
  stands.

### HT-10: `max_text_chars` is exceeded by the length of the truncation marker
- Severity: **info**
- Module: `sanitize/sanitizer.py` (`_take_budget`)
- Description: at a budget of 30,000, `body_text` is 30,010 characters long — `\n[gekürzt]` is
  added after the truncation. With the budget exhausted, every attachment text additionally
  gets the marker (max. 20 of them, so ≤ 200 further characters).
- Fix: none. The excess is bounded by a constant and intended (the marker is meant to be
  visible); the protective purpose of the limit — no unbounded text into the LLM — is preserved.
  Pinned by `test_text_budget_boundaries`.

### HT-11: `scrub_plain` removes the brackets of already defanged forms
- Severity: **info**
- Module: `output/sanitizer.py`
- Description: `scrub_plain` (domain, display name, filename) runs without link detection and
  deletes `[`/`]` as markup. `evil[.]com` briefly becomes `evil.com` again in the process.
- Fix: none. `final_guard` defangs again immediately afterwards, and since HT-4 the rule also
  catches the forms that previously slipped through. The property test
  `test_scrub_plain_output_is_never_clickable` covers the chain.

### HT-12: `compose_plain` does not scrub its fields
- Severity: **info**
- Module: `output/composer.py`
- Description: `compose_plain` only calls `_finalize` (final pass + split), not `scrub_field`.
  Untrusted text would therefore pass without a field scrub — control characters and markup
  would remain.
- Fix: none. The only caller is the CLI test message, whose text lives in the code (ADR-054);
  the docstring says so explicitly. It is pinned by the property test
  `test_split_never_produces_an_unsafe_part` sending its input through `scrub_field`
  **explicitly** first — whoever changes that sees the reason in the test.

### Final coverage (NF-6)

Measured with `.venv/bin/pytest --cov=src/maildigest --cov-report=term-missing`:

| Area | Target (NF-6) | before WP10 | after WP10 (1103 tests) | as of WP12 (1278 tests) |
|---------|-------------|----------|------------------------|-------------------------|
| `sanitize/` | ≥ 90 % | 92.3 % (48 open) | **98 %** (622 stmts, 10 open) | **98 %** (645 stmts, 11 open) |
| `output/` | ≥ 90 % | 98.0 % | **99 %** (308 stmts, 4 open) | **99 %** (341 stmts, 2 open) |
| overall | ≥ 80 % | 95.5 % | **97 %** (3824 stmts, 122 open) | **97 %** (3986 stmts, 118 open) |

The remaining gaps are branches reachable only on other platforms (`__main__.py`,
`# pragma: no cover` branches) or HTTP error paths of the provider/messenger adapters that are
already covered by `httpx.MockTransport` in their own tests.

### New test files from WP10

| File | Content |
|-------|--------|
| `tests/unit/test_hot_properties.py` | Property-based tests (hypothesis, ADR-058) — the four core properties from §2 point 3 |
| `tests/unit/test_hot_edge_cases.py` | Edge cases from PLAN WP10 + regression tests for HT-1/2/4/5/6 |
| `tests/unit/test_hot_fault_injection.py` | Fault injection at every stage, both LLM positions (§2 point 4) |
| `tests/unit/test_hot_state_concurrency.py` | SQLite locking, two processes/twelve threads, restart, schema |
| `tests/unit/test_hot_schedule.py` | Digest time (midnight, DST change) and the 1-hour bound of the outbox |
| `tests/unit/test_hot_cli_robustness.py` | Broken config, missing permissions, no secrets in error messages |
| `tests/unit/test_hot_sanitize_error_paths.py` | Defensive branches of the sanitizer and the PDF child process |

### HT-13: Chains of line Markdown survived the scrub
- Severity: **low**
- Module: `output/sanitizer.py` (`scrub_plain`, the number of rounds)
- Description: found by the property test with `⚠️# # #`. Every round removes only **one**
  marker per line, and a leading structural emoji shifts the line start by one further round —
  with three rounds a `#` remained. A standalone `#` does not render a heading, but the assured
  property ("no line-leading Markdown survives") no longer held.
- Fix: a separate, generous number of rounds for this step (`_MAX_LINE_MARKUP_ROUNDS = 12`)
  instead of the shared 3. Regression:
  `test_ht13_ketten_von_zeilen_markdown_ueberleben_nicht`.
- Origin: the case only surfaced months after WP10 — the property draws new examples on every
  run. That is exactly what it is for.

## 6. Findings log (cold testing)

Run WP11, 2026-09-08. The complete black-box report with reproduction steps, a coverage table
and the overall verdict is in **`tests/cold/REPORT.md`**; it is not changed after the fact — it
is the record of what an agent without knowledge of the code measured. This table carries the
follow-up (§3): triage, fix and regression test.

The severity yardstick is as in §5 (the effect on the user). The fixes ran in three parallel
passes (output/critic, ingest/delivery, CLI); the finalisation merged them, cross-checked them
and followed up the four points left open.

| CT | Severity | Title (short) | Status |
|----|----------|------------------|--------|
| CT-1 | high | Global options before the command name have no effect | **fixed** (ADR-068) — `test_cli.py::test_ct1_*`, `test_cold_suite.py::test_ct1_*` |
| CT-2 | low | The error message points at ARCHITECTURE instead of SPEC-CLI | **fixed** — `test_config.py::test_ct2_*`, `test_cold_suite.py::test_ct2_*` |
| CT-3 | info | `[llm.critic]` incomplete; a question hint without a question | **fixed** — `test_cli.py::test_ct3_*`, `test_cold_suite.py::test_ct3_*` |
| CT-4 | medium | `test --dry-run` claims a delivery and does not show the note | **fixed** (ADR-071) — `test_cli_e2e.py::test_ct4_*`, `test_cold_suite.py::test_ct4_*` |
| CT-5 | low | An out-of-range port value ends with exit 1 instead of 2 | **fixed** (ADR-069) — `test_cli.py::test_ct5_*`, `test_cold_suite.py::test_ct5_*` |
| CT-6 | high | The injection suspicion depends on the LLM alone (F-SEC-5) | **fixed** (ADR-061) — `test_summarizer.py::test_ct6_*`, `test_cold_suite.py::test_ct6_*` |
| CT-7 | medium | Markdown reaches the messenger (F-SEC-3) | **fixed** (ADR-062) — `test_output_sanitizer.py::test_ct7_*`, property tests, `test_cold_suite.py` |
| CT-7a | medium | The same leak without a model, through the subject line of the note | **fixed** (ADR-062) — `test_ct7a_*`, `test_cold_suite.py::test_ct7a_*` |
| CT-8 | medium | The message structure is forgeable (a fake note line) | **fixed** (ADR-062) — `test_ct8_*`, `test_cold_suite.py::test_der_angreifer_kann_keine_programmzeile_faelschen` |
| CT-9 | high | Unconditional EXPUNGE after every seen flag (F-ING-1) | **fixed** (ADR-064) — `test_ingest_client.py::test_ct9_*`, `test_ingest_poll.py::test_ct9_*` |
| CT-10 | medium | The summary line always counts delivered messages as 0 | **fixed** (ADR-070) — `test_cli_e2e.py::test_ct10_*` |
| CT-11 | medium | Hard signals never raise to `high`, no banner (F-CRIT-3) | **fixed** (ADR-063) — `test_critic_signals.py::test_ct11_*`, `test_cold_suite.py::test_ct11_*` |
| CT-12 | low | "Mailbox unreachable" for a missing target folder | **fixed** (ADR-065) — `test_ingest_client.py::test_ct12_*`, `test_ingest_poll.py::test_ct12_*` |
| CT-13 | medium | A retry delivers already sent parts again | **fixed** (ADR-066) — `test_delivery.py::test_ct13_*` |
| CT-14 | medium | `[links] footnote = true` has no effect | **fixed** (ADR-072) — `test_output_composer.py::test_ct14_*`, `test_cold_suite.py::test_ct14_*` |
| CT-15 | medium | A diverging HTML part with `multipart/alternative` | **fixed** (ADR-067) — `test_sanitize_mail.py::TestCt15*`, `test_output_composer.py::test_ct15_*`, `test_cold_suite.py::test_ct15_*` |
| CT-16a | info | `(1 Teil)` instead of the spec's literal `(N Teile)` | **open, deliberately** — the code is right (German grammar), the contract was imprecise. Clarified in SPEC-CLI §4 `test`. No code fix. |
| CT-16b | info | EOF on stdin ends with exit 1 instead of 2 | **fixed** (ADR-069) — `test_cli.py::test_ct16_*`, `test_cold_suite.py::test_ct16b_*` |
| CT-16c | info | LLM attempt counts vary (9/3/2) | **open, deliberately** — three multiplicative retry levels, each with its own rationale. Not a bug; broken down in ARCHITECTURE §6, referenced from F-SEC-7. |
| CT-16d | info | REQUIREMENTS F-SEC-4 wrongly names `text/html` | **fixed (docs)** — REQUIREMENTS F-SEC-4 corrected; the behaviour was right, the requirement wrong. |
| CT-16e | info | Positive finding: `log_level=INFO` suppresses tracebacks | **no finding** — nothing to do. |

**A deliberate deviation on CT-6.** One aspect of the finding ("removed hidden text sets
`injection_suspected`") is deliberately solved differently: `hidden_text_removed` does **not**
set the flag. Invisible text is the norm in newsletters (preheaders with `display:none`); the
line "mail contained instructions to the AI (ignored)" would simply be wrong there and would
devalue the warning — warning fatigue instead of a warning. The signal reaches the user as a
separate, literally accurate note "hidden text removed from the HTML" instead (ADR-061). The
core of the finding — deterministic signals never reaching the user — is thereby fixed.

### Findings from the finalisation (review of the three fix passes)

The merge found four gaps that none of the three passes could or would close; they are handled
in this version:

- **CT-2 and CT-14 fell between responsibilities.** Both fixes live in files assigned to a
  different parallel agent; both were reported as "not fixed" with an exact patch proposal.
  Followed up.
- **The CT-15 signal did not reach the user.** The sanitizer computed `html_divergent`, but
  neither `output/composer._hints_line` nor `agents/critic.collect_signals` evaluated it — the
  field had no consequence. Followed up.
- **A foreign test encoded the CT-10 bug.**
  `test_runner_e2e.py::test_crash_between_commit_and_delivery_loses_nothing` expected
  `delivery.delivered == 1` and thereby pinned the old, faulty accounting. Corrected to
  `1 + stats.ingest.processed` (ADR-070).
- **The CT-6 phrase list was too broad.** `du bist jetzt …` and `system prompt` on their own
  would have hit everyday German ("du bist jetzt dran", "wir besprechen den System-Prompt im
  Meeting") — the same false-alarm mechanism for which `hidden_text_removed` was excluded. The
  patterns now require an object (`… ein Sprachmodell`, `nenne mir deinen Systemprompt`).

### What is automated — and what stays a script

`tests/cold/test_cold_suite.py` (73 tests, part of `pytest`) drives the attack corpus
`tests/cold/mails/*.eml` through the same front door as the cold tester
(`maildigest.cli.main()`). Only the three outside contacts it had started mocks for are
replaced: the LLM **provider** (not the agent — otherwise the deterministic post-check from
CT-6/CT-11 would be bypassed and the test worthless), the messenger and IMAP. Automated are:
the core property over the whole corpus against a model that is taken over completely, the
structure forgery (CT-8), CT-4, CT-6, CT-7/7a, CT-11, CT-14, CT-15 as well as the pure CLI
findings CT-1, CT-2, CT-3, CT-5, CT-16b and an F-SEC-8 probe with the cold test's marked fake
secrets.

**Not automated, remains a documented manual script** (`tests/cold/scripts/`, verbatim in the
repo as the cold tester ran them; excluded from pytest via `tests/cold/conftest.py` and from
linting via `pyproject.toml`):

| Script | Why not in pytest |
|--------|------------------------|
| `imap_server.py` | A real IMAP4rev1 server over a TLS socket. CT-9/CT-12 are covered instead by the mailbox doubles in `test_ingest_client.py`/`test_ingest_poll.py`, which log every raw command and fail loudly on `flag()`/`move()`/`delete()`/`expunge()`. |
| `sink_server.py` | An HTTP sink with abort modes (`die_after_1`, `http500`). The connection abort in the middle of a multi-part message (CT-13) is reproduced in `test_delivery.py` without a socket. |
| `mock_llm.py` | An OpenAI-compatible HTTP endpoint. Reproduced in the suite as `FakeProvider` on the provider interface — the same modes (`nice`, `raw`, `broken`), without a port and without waiting. |
| `split_test.py` | 210 runs × process start across 28 offsets (minutes of runtime). The property is covered as a property test in `test_hot_properties.py` (ADR-059). |
| `check_sink.py`, `mocks.sh`, `run_mails.sh`, `make_mails.py` | Evaluation and setup helpers of the black-box environment; meaningless without that environment. `make_mails.py` produced the corpus under `tests/cold/mails/`. |

For a second cold round (§3 follow-up) the scripts therefore remain runnable; they need the
working environment from the report header and absolute paths in it.

## 7. Findings log (final test round, HC-1 … HC-38)

Run 2026-09-09 to 2026-09-11 on state `13cb859`; the complete report with reproduction, cause
and skeptic review is in **`docs/TESTRUNDE-HOT-COLD.md`** (German) and is not changed after the
fact. This table carries the follow-up: responsibility, status, regression test. The fix plan
with the package split, decisions and acceptance criteria is
[docs/PLAN-FIXRUNDE.md](PLAN-FIXRUNDE.md) (German).

The severity yardstick is as in §5 and §6 (the effect on the user). The fixes ran in nine
packages across four waves (`4697231`, `4b6f709`, `a9a4d87`, `64c0ae4`); the test count rose
from 1354 to 1565 in the process.

| HC | Sev. | Title (short) | Status | Tests | ADR |
|----|------|------------------|--------|-------|-----|
| HC-1 | high | A subject > 100 characters aborts operation without a model fail-closed | **fixed** | `test_offline.py::test_hc1_langer_betreff_wird_gekuerzt_statt_fail_closed`, `::test_hc1_betreff_von_genau_101_zeichen_geht_durch` | ADR-076 (A) |
| HC-2 | medium | A processed attachment disappears silently without a model | **fixed** | `test_offline.py::test_hc2_*` (5), `test_cli.py::test_hc38_selbsttest_im_werkszustand_zeigt_den_anhang` | ADR-076 (A), ADR-034 (A) |
| HC-3 | medium | `connect-llm` sets a foreign `base_url` and shows the wrong instructions | **fixed** | `test_cli_connect.py::test_hc3_*` (4), `test_providers.py::test_hc3_*` (2) | ADR-075 (A), ADR-076 (A) |
| HC-4 | medium | The provider's error text reaches the terminal unfiltered (ANSI) | **fixed** | `test_llm_providers.py::test_hc4_*` (2), `test_cli_connect.py::test_hc4_*` (2), `test_hot_cli_robustness.py::test_hc4_*` (6) | ADR-055 (A) |
| HC-5 | medium | Forged data-block markers are removed silently instead of flagged | **fixed** | `test_summarizer.py::test_hc5_*`, `test_sanitize_mail.py::TestHc5GefaelschteDatenblockMarker`, `test_cold_suite.py::test_hc5_hc21_*` | ADR-061 (A) |
| HC-6 | medium | The split can produce a forged program line at the start of a part | **fixed** | `test_output_sanitizer.py::test_hc6_*`, `test_hot_properties.py::test_hc6_*`, `test_cold_suite.py::test_hc6_*` | ADR-062 (A), ADR-040 (A) |
| HC-7 | medium | Markdown in the link footnote is not neutralised | **fixed** | `test_output_composer.py::test_hc7_*`, `test_sanitize_links.py::…::test_hc7_*` | ADR-062 (A) |
| HC-8 | medium | The control character U+0000 reaches the delivered message part | **fixed** | `test_hot_properties.py::test_hc8_*`, `test_sanitize_links.py::…::test_hc8_*` (2), `test_output_sanitizer.py::test_hc8_*` | — |
| HC-9 | medium | A bare IPv4 stays unbroken with neighbouring characters | **fixed** | `test_output_sanitizer.py::test_hc9_*` (2), `test_hot_properties.py::test_scrub_field_output_is_never_clickable` | ADR-036 (A) |
| HC-10 | medium | The dedupe key is the `Message-ID` header set by the attacker | **fixed** | `test_ingest_poll.py::test_hc10_*` (5), `test_state_db.py::test_hc10_*` (6), `test_ingest_rawmail.py::test_hc10_*` (3), `test_output_composer.py::test_hc10_*` (2) | **ADR-079**, ADR-018/019/048 (A) |
| HC-11 | medium | Mail-derived text lands in the log through `extra_forbidden` | **fixed** | `test_llm_schema.py::test_hc11_*` (2), `test_hot_fault_injection.py::test_hc11_*` | ADR-024 (A) |
| HC-12 | medium | `/digest` does not shorten the waiting time | **fixed** | `test_runner.py::test_hc12_*` (2) | **ADR-080**, ADR-077 (A) |
| HC-13 | medium | After a `/digest` all further commands are discarded | **fixed** | `test_runner.py::test_hc13_*` (2) | ADR-077 (A) |
| HC-14 | low | The interface is English, the contract German | **fixed** | `test_hc14_spec_literals.py` (19 cases) | **ADR-083** |
| HC-15 | medium | `connect-mail` lets blocked providers through as a mail address | **fixed** | `test_cli_connect.py::test_hc15_*` (4), `test_providers.py::test_hc15_*` | ADR-075 (A) |
| HC-16 | low | The test message names an invalid config section and goes to every messenger | **done in `14ad9ed`** (ADR-078) | `test_cli.py::test_testnachricht_nennt_keine_ungueltige_config_sektion` | ADR-078 |
| HC-17 | low | The self-test preamble claims the example mail with `--eml` | **fixed** | `test_cli_e2e.py::test_hc17_*` (2) | — |
| HC-18 | low | `init` does not write the complete field set from §5 | **fixed** (the remainder; the field set was anticipated in `14ad9ed`) | `test_cli.py::test_hc18_*`, `test_spec_cli.py::test_hc18_*`, `test_cli_connect.py::test_hc18_*` | ADR-076 (A) |
| HC-19 | low | The command hint is dropped with `connect-messenger --chat-id` | **fixed** | `test_cli_connect.py::test_hc19_*` (2) | — |
| HC-20 | medium | The configuration is not written atomically | **fixed** | `test_hot_cli_robustness.py::test_hc20_*` (3) | **ADR-081** |
| HC-21 | medium | The phrase list misses the most common formulations | **fixed** | `test_summarizer.py::test_hc21_*` (3, 13 cases), `test_cold_suite.py::test_hc5_hc21_*` | ADR-061 (A), ADR-076 (A) |
| HC-22 | low | An overlong filename is truncated without the promised `…` | **fixed** | `test_sanitize_attachments.py::…::test_hc22_*` (4), `test_output_composer.py::test_hc22_*` | ADR-040 (A) |
| HC-23 | low | The sender display name is never RFC 2047 decoded | **fixed** | `test_ingest_rawmail.py::test_hc23_*` (5) | ADR-020 (A) |
| HC-24 | low | Labels > 63 characters defeat `_RE_DOMAINISH`; the oracle mirrors the bound | **fixed** | `test_output_sanitizer.py::test_hc24_*` (2), `test_hot_properties.py::test_scrub_*_output_is_never_clickable` | ADR-036 (A) |
| HC-25 | low | Outbox deadlines depend on the wall clock | **fixed** | `test_delivery.py::test_hc25_*` (5) | ADR-048 (A) |
| HC-26 | low | The collected digest is blocked along with an IMAP outage | **fixed** | `test_runner.py::test_hc26_*` (3) | ADR-049 (A) |
| HC-27 | low | `run --once` never polls for commands | **fixed** | `test_runner.py::test_hc27_*` (3) | **ADR-080** |
| HC-28 | low | The `/status` answer bypasses `scrub_field` | **fixed** | `test_runner.py::test_hc28_status_antwort_scrubbt_den_ordnernamen` | ADR-077 (A) |
| HC-29 | low | Quadratic runtime in `_redact_tokens` | **fixed** | `test_summarizer.py::test_hc29_*` (2, 3 cases) | — |
| HC-30 | low | `Retry-After: nan` leaves the error taxonomy | **fixed** | `test_llm_providers.py::test_hc30_*` (2), `test_messenger_adapters.py::test_hc30_*` (2) | — |
| HC-31 | low | The README "Limitations" and the CHANGELOG did not know about remote triggering | **done in `14ad9ed`** (the commit message wrongly calls it "HC-19") | — | ADR-078 |
| HC-32 | low | `connect-mail`: an unsuitable provider hint, no server response | **fixed** (reproduced first) | `test_ingest_client.py::test_hc32_*` (2), `test_cli_connect.py::test_hc32_*` (3) | — |
| HC-33 | low | A PGP/S-MIME mail looks like a mail with no content | **fixed** (reproduced first) | `test_sanitize_mail.py::TestHc33VerschluesselteMail` (4), `test_output_composer.py::test_hc33_*` (2), `test_critic_signals.py::test_hc33_*` (3) | **ADR-082** |
| HC-34 | low | A missing IMAP password reported as "mailbox unreachable" | **fixed** (reproduced first) | `test_cli.py::test_hc34_run_meldet_fehlendes_passwort_als_konfigurationsfehler` | — |
| HC-35 | low | With an unconfirmed delivery the line `5/5 …` is missing | **fixed** (reproduced first) | `test_cli_e2e.py::test_hc35_nicht_bestaetigte_zustellung_hat_eine_fuenfte_zeile` | — |
| HC-36 | low | `connect-llm --provider` always shows the Groq instructions | **merged into HC-3** (same cause, fixed there) | `test_cli_connect.py::test_hc3_provider_anthropic_zeigt_die_anthropic_anleitung` | ADR-075 (A) |
| HC-37 | info | Two layer boundaries of command recognition | **fixed (docs)** — (a) the README/spec now describe the tolerant recognition correctly, the behaviour was right; (b) recorded below as a layer boundary | `test_commands.py::test_bekannte_befehle_werden_erkannt`, `::test_botname_anhang_wird_abgetrennt`, `::test_argumente_werden_ignoriert_nicht_gelesen`, `::test_freier_text_wird_verworfen` | — |
| HC-38 | low | Test coverage of the default mode, the `/digest` branch, the mechanical promises | **fixed** (three parts in FP-1/FP-6/FP-9) | `test_runner.py::test_hc38_*` (2), `test_cli_connect.py::test_hc38_*`, `test_cli.py::test_hc38_*` (2), `test_invarianten.py::test_hc38_*` (3) | — |

| HC2-1 remainder (a) the bound only applies after the complete lxml parse (24 MB = 47.4 s), (b) the bound applies per part instead of per mail (34 parts = 29.6 s without rejection) — **second iteration** | medium | **fixed** | `test_sanitize_html.py::TestHc21Schranken::test_hc2_1_byte_deckel_*`/`*_budget_*` (5), `test_sanitize_mail.py::TestHc21SchrankenDerHtmlKonvertierung::test_hc2_1_riesiger_html_teil_*`, `*_viele_html_teile_*`, `*_teuerste_mail_*`, `*_byte_budget_*`, `*_gewoehnliche_html_mail_*` (5) | ADR-084 (A, 2026-09-12) |
| HC2-2 remainder an RFC 2047 Q-word with a raw `@`/`,` still falsifies `from_domain` — **second iteration** | medium | **fixed** | `test_ingest_rawmail.py::test_hc2_2_q_wort_*`, `*_b_wort_*`, `*_kodiertes_wort_direkt_*`, `*_zwei_kodierte_woerter`, `*_gewoehnlicher_kodierter_name_*` (8) | ADR-020 (A, 2026-09-12) |

"(A)" = an addendum to an existing ADR, dated **2026-09-11** (second iteration:
**2026-09-12**). ADRs in bold are new.

**Balance:** 35 open findings, of which 33 with a code fix and a regression test, one purely
documentary (HC-37 a), one merged into another (HC-36); plus two already done in `14ad9ed`
(HC-16, HC-31). **No finding stays deliberately open, none was irreproducible.** The six
findings not reviewed by any skeptic (HC-32 … HC-35, HC-37, parts of HC-36) were reproduced by
the respective fix agent before the fix and all confirmed.

### Corrections to §5: three rationales no longer held

This round refuted three entries of the hot log. They stay above unchanged — §5 is history —
but from here on they are superseded:

- **HT-14 (new): the detection regex and the test oracle shared the same bound.**
  `tests/unit/test_hot_properties.py:_RE_LIVE_DOMAIN` had taken over the DNS length bound with
  `[a-z0-9\-]{0,62}`/`{1,63}` and the underscore word boundary of the implementation with
  `(?![\w\-.])` — it could not, in principle, find the gap it is supposed to check (HC-24).
  That is the same class of error as HT-1/2/4/6, one level up. Fixed by: removing the caps in
  the oracle **and** the implementation without replacement, adding an IPv4 prohibition pattern
  (`_RE_LIVE_IPV4`) that was missing there entirely, extending the oracle's structure lines by
  the English labels (it only checked the German ones, even though the output is English), and
  adding the comment "the oracle must be strictly more generous than the implementation". As a
  rule for future rounds: **an oracle that repeats a constant of the implementation checks
  nothing.** A second find from HC-23 belongs to the same class:
  `tests/integration/test_sanitize_corpus.py` rebuilt the `RawMail` by hand instead of calling
  `build_raw_mail`, and thereby bypassed exactly the stage the bug sat in; the helper is gone.
  Likewise HC-5: the CT-6 regression test built its `SanitizedMail` with `make_mail()` and
  skipped the sanitizer.
- **HT-7 correction: "200,000-character fields covered" did not hold.** The fault injection's
  test value is `"S" * 200_000` and contains **not a single** match of `_URL_TOKEN_RE`;
  `_redact_tokens` exits immediately after `finditer` and measures nothing. The class at issue —
  a whitespace-free field **with** many matches — was untested until HC-29 and ran quadratically
  there (32,000 characters: 9.26 s; 0.006 s after the fix). It is now covered by
  `test_summarizer.py::test_hc29_redact_tokens_bleibt_im_zeitbudget`. HT-7's null finding
  otherwise stands.
- **HT-12 correction: `compose_plain` does not have one caller but three.** The old rationale
  ("the only caller is the CLI test message, whose text lives in the code") has not held since
  ADR-054: the callers are `cli.py:_send_test_message`, `cli.py:_announce_selftest` and
  `runner.py:handle_command` — and the third interpolates `[imap] folder`, i.e. a variable part
  (HC-28). The layer boundary from now on reads: **`_finalize` is the final pass and the split,
  not a field scrub; variable parts are scrubbed by the caller.** Recorded in the docstring of
  `compose_plain`, in ADR-077 (addendum b) and mechanically in
  `test_invarianten.py::test_hc38_compose_plain_hat_nur_die_gelisteten_aufrufer`.

### Layer boundaries this round confirmed

- **HC-37 (b): no rate limit for `/status`.** Six commands in one batch produce six answers;
  only `/digest` floods are collapsed into exactly **one** additional cycle (ADR-077, unchanged
  since HC-13). A command flood therefore does not drive model cost up linearly — but it can
  flood the messenger with status answers. Whoever can write into the chat is, per SECURITY §1,
  the operator themselves; for group chats `accept_commands = false` is provided (README).
- **`run --once` does not run under the signal handlers.** The finding refuted under "checked
  and rejected" no. 6 in the report ("a crash between `claim` and `checked` loses the mail")
  leaves a clarification behind: only `run_forever` installs the signal handlers. A Ctrl+C or a
  cron timeout during `run --once` really does produce the state `sanitized` — the mail stays
  queryable as a row, but is recognised as a duplicate on restart and not processed again
  (ADR-019 records this as accepted). Also in OPERATIONS §3.
- **Over-neutralisation is the fail-safe exit for HC-5** (ADR-036): `<… MAILDIGEST …
  UNTRUSTED …>` within a line is replaced even when a harmless sender happens to write both
  words in angle brackets. Across the whole corpus the case does not occur.
- **The false alarm rate did not rise through HC-5 and HC-21.** Across the 49 sanitisable mails
  from `tests/corpus/` and `tests/cold/mails/`, four set the injection suspicion before and four
  after — all four are attack mails. The only new thing is that `10_injection_direkt.eml`
  additionally carries the evidence `forged_block_marker`. The new `encrypted` flag (HC-33) is
  likewise set by not a single mail across the whole corpus.

### New test files and corpus mails from this round

| File | Content |
|-------|--------|
| `tests/unit/test_hot_cli_robustness.py` | The terminal allowlist and key masking (HC-4), atomic writing of the configuration (HC-20) |
| `tests/unit/test_hc14_spec_literals.py` | A contract test: reads the literals from SPEC-CLI §2/§4/§6 and compares them with a genuinely composed message or with `inspect.getsource(cli)` (19 cases) |
| `tests/cold/mails/30_marker_nachbau.eml` | An exactly forged data-block marker (HC-5) |
| `tests/cold/mails/31_injection_variante.eml` | An obvious variant of the takeover formula (HC-21) |

`tests/unit/test_invarianten.py` has grown from 24 to 27 tests: the set of `.send(` call sites
(seven), the origin of every send argument and the callers of `compose_plain` (three) are now
AST-locked (HC-38). Both promises had until then been prose in SECURITY §7.1 only — and both
would have made HC-28 visible immediately when the `/status` answer was added.

### Coverage after this round

| Module | State |
|-------|-------|
| `agents/offline.py` | 100 % |
| `output/sanitizer.py` | 99 % (2 lines: the fixed-point return in `_unescape`, the fast path of `_strip_control`) |
| `sanitize/links.py` | 99 % (1 line: `build_footnote` without entries) |
| `sanitize/attachments.py` | 100 % |
| `runner.py` | 99 % (4 lines: the `AssertionError` guard in `_with_llm_retry`, the `_stop.wait` branch of `_wait`, `total.low_digests += 1`, the placeholder `_unwired`) |
| `cli.py` | 96 %, `providers.py` 89 % (fallback branches of `find_preset`) |

In `runner.build_runner` the offline branches are now covered; what remains uncovered there are
only the branches with a real LLM provider. The overall targets from NF-6 are met unchanged.

### §4 after this round

The checkbox **"second cold round with a fresh agent"** stays open — it runs as §6 of the fix
round ([docs/PLAN-FIXRUNDE.md](PLAN-FIXRUNDE.md)). Its precondition has been met since HC-14:
the spec again describes literally what the program outputs, and a contract test pins that.
Until the round has taken place, NF-8 stays `in-progress`.

### Follow-up fix round NF-1 (HC2-1, HC2-2)

The second test round (**`docs/TESTRUNDE-2.md`**, 45 findings) and the acceptance review
(**`docs/ABNAHME-FIXRUNDE.md`**) are records and are not changed. §8 of the acceptance review
makes two findings a **condition before the release**; they are followed up here. The remaining
packages NF-2 … NF-7 from that residual list are open.

| Finding | Severity | Status | Tests | ADR |
|--------|----------|--------|-------|-----|
| HC2-1 `html_to_text` scales quadratically with the nesting depth; no bound | high | **fixed** | `test_sanitize_html.py::TestHc21Schranken` (5), `test_sanitize_mail.py::TestHc21SchrankenDerHtmlKonvertierung` (6), `test_output_composer.py::test_hc2_1_*` (2), `test_ingest_poll.py::test_hc2_1_kill_waehrend_der_verarbeitung_ist_kein_dauer_dos` | **ADR-084**, ADR-067/029/019 (no addendum: only cited) |
| HC2-2 RFC 2047 decoding before the address parse (a regression from HC-23) | medium (blocking) | **fixed** | `test_ingest_rawmail.py::test_hc2_2_*` (7) | ADR-020 (A) |
| R-4 (third iteration) the mask for encoded words was narrower than the decoder (an empty charset `=??Q?…?=`) | high | **fixed** | `test_ingest_rawmail.py::test_hc2_2_maskierung_ist_nicht_enger_als_der_dekoder` (8 forms), `::test_hc2_2_leerer_charset_bestimmt_die_domain_nicht` (3), `::test_hc2_2_leerer_charset_im_reply_to` | ADR-020 (A, third iteration) |
| R-5 (third iteration) `LinkCollector.scrub` quadratic in the number of matches; no link budget | high (DoS) | **fixed** | `test_sanitize_links.py::TestR5LinkBudget` (5), `test_output_composer.py::test_r5_*` (2) | ADR-028 (A), ADR-084 (A) |
| R-6 (third iteration) the plain-text path without a budget (up to 25 MB through all passes) | medium | **fixed** | `test_sanitize_mail.py::test_r6_*` (3) | ADR-084 (A) |
| R-7 (third iteration) measurement correction for the most expensive mail; the promise "around 2 s" was too tight | medium | **fixed** (promise corrected) | `test_sanitize_mail.py::test_hc2_1_teuerste_mail_unter_den_neuen_grenzen` (re-measured), `::test_r7_gesamt_worst_case_bleibt_weit_unter_der_zusage` | ADR-084 (A) |
| R-8 (fourth iteration) a regression from R-4: `_ENCODED_WORD_RE` with `.*?` is quadratic; no header upper bound | high (DoS in ingest) | **fixed** | `test_ingest_rawmail.py::test_r8_maskierung_ist_linear`, `::test_r8_riesiger_from_header_kostet_keine_zeit`, `::test_r8_riesiger_betreff_kostet_keine_zeit`, `::test_r8_gewoehnlicher_header_bleibt_unveraendert` | ADR-020 (A, fourth iteration) |
| R-9 (fourth iteration) an encoded word after the address deletes the sender address and domain; both return-path warnings go silent | medium | **fixed** | `test_ingest_rawmail.py::test_r9_angehaengtes_kodiertes_wort_loescht_die_domain_nicht` (4 forms), `::test_r9_zweiter_griff_nimmt_die_erste_klammer` | ADR-020 (A, fourth iteration) |
| R-10 (fourth iteration) the plain-text pre-cut applied per text chunk instead of per mail; the number of parts unbounded | high (9.2–10.2 s CPU per mail) | **fixed** | `test_sanitize_mail.py::TestR10SchrankenDesAnhangsPfads` (6) | ADR-084 (A, third addendum) |
| R-11 (fourth iteration) `pdf_timeout_seconds` applies per attachment; 20 PDFs ≈ 400 s wall time | high (DoS across the poll period) | **fixed** | `test_sanitize_mail.py::TestR11PdfZeitbudget` (2), `test_spec_cli.py::test_alle_config_felder_stehen_in_der_referenz` (field set) | ADR-029 (A, fourth iteration) |
| S-1 (fifth iteration) the header cap applied only to From/Reply-To/Subject; `To` uncapped (20 MB = 18.5 s), and re-serialisation folds every header of every part (up to 14 s) | high (DoS in ingest) | **fixed** | `test_ingest_rawmail.py::test_s1_riesiger_to_header_kostet_keine_zeit`, `::test_s1_messreihe_to_header`, `::test_s1_jeder_gelesene_header_ist_gedeckelt` (10 headers), `::test_s1_empfaengerzahl_ist_gedeckelt`, `::test_s1_viele_kopfzeilen_kosten_keine_zeit`, `::test_s1_kopfzeilen_gesamtbudget_schneidet_den_baum`, `::test_s1_kopfzeilen_ersetzen_ist_sichtbar`, `::test_s1_gewoehnliche_mail_bleibt_byteidentisch` | ADR-020 (A, fifth iteration) |
| S-2 (fifth iteration) a regression from R-9: the bracket fallback reads comment/quoted-string content; the 4096 cut opens comments; an unreadable `Reply-To` switches the warning off | high | **fixed** | `test_ingest_rawmail.py::test_s2_klammer_in_kommentar_oder_quote_bestimmt_die_domain_nicht` (9 forms), `::test_s2_dasselbe_im_reply_to` (9), `::test_s2_balancierte_kommentare_und_quotes_bleiben_lesbar` (8), `::test_s2_scanner_kennt_verschachtelung_escapes_und_offene_enden`, `::test_s2_token_hinter_der_klammer_loescht_die_antwortadresse_nicht`, `::test_s2_reply_to_gleich_absender_mit_kommentar_ist_kein_mismatch`, `test_sanitize_mail.py::TestReport::test_s2_*` (2) | ADR-020 (A, fifth iteration) |
| S-3 (fifth iteration) the overall worst case measured too favourably (2.70 s); really 3.6–5.1 s, about 2 s of it the standard library parser | medium | **fixed** (figure corrected, a raw-byte budget deliberately rejected) | measurement `sk4_final.py`, profile (`_payload_bytes`/`_decode_text_part` together 0.01 s) | ADR-084 (A, fourth addendum) |
| S-4 (sanitizer, 2026-09-12) `_RE_IPV4` with exactly four octets breaks only the last window in dot chains > 4 groups: `1.1.1.1.1.1.1.` → `1.1.1.1[.]1[.]1[.]1.`, the first four octets stay live (I3/F-SEC-3); found by the property test CT-7 (a hypothesis counter-example, green in the starting state only because of the local example cache) | medium | **fixed** (`{3,}`: the whole chain, every dot broken) | `test_hot_properties.py::test_s4_punktkette_wird_ganz_gebrochen` (4 forms, besides the property test), `test_output_sanitizer.py::test_s4_punktkette_ueber_vier_oktette_wird_ganz_gebrochen` (3), `::test_s4_ziffernlauf_hinter_der_kette_bleibt_wie_bisher` (3, counter-check) | ADR-036 (A, S-4) |

"(A)" = an addendum to an existing ADR, dated **2026-09-11**. ADRs in bold are new.

**What the two tests are meant to prove.** For HC2-1 the finding's claim is a claim about time,
so the regression test measures the wall clock: 16,000 levels before the fix 28.6 s, afterwards
< 1 s (`test_hc2_1_deep_nesting_is_linear`; seen and measured before the fix). Because a timing
test alone can also go green through an earlier abort, `test_hc2_1_konversion_bleibt_linear`
checks the curve **without** the bound (four times the depth, at most eight times the time —
quadratic would be sixteen times). For HC2-2 the oracle is the address in the **raw header**,
determined independently of the production code (remove encoded words, then read the address) —
not `build_raw_mail` itself. Five of the seven HC2-2 tests fail on the state before the fix, the
two control cases (an unencoded mail, the HC-23 case) stay green.

**Second iteration (2026-09-12).** The skeptic confirmed the original repros as fixed but
demonstrated three remaining gaps; they are carried above as rows of their own. For the two
HC2-1 remainders the claim is again about time, so the tests measure the wall clock on exactly
the repro mails: a 24 MB part 47.4 s → < 0.5 s, 34 HTML parts 29.6 s → < 2 s. So that a timing
test does not merely confirm the new bounds, `test_hc2_1_teuerste_mail_unter_den_neuen_grenzen`
additionally constructs the **most unfavourable** mail possible under the new bounds
(`MAX_HTML_PARTS` × `max_html_bytes` of the densest element form): measured at around 2 s. For
the HC2-2 remainder the oracle is still the address in the raw header; the new cases carry `@`,
`,`, `<` and `>` **literally** in the Q encoding — the pure base64 oracle of the first iteration
could not produce them. All eight new HC2-2 tests fail on the state before this iteration, and
the counter-check (an ordinary encoded name) stays green.

**Third iteration (2026-09-12).** The second iteration's skeptic confirmed all previous repros
as dead and demonstrated four new points (carried above as R-4 to R-7). Two of them are claims
about time and are again measured on the wall clock, each seen before the fix on the same
scripts: `LinkCollector.scrub` with 32,000 matches 17.0 s → 0.3 s, the 1 MB HTML mail with
41,000 links (within **all** ADR-084 bounds, `html_rejected` was False) 28.9 s → 1.1 s, 21 MB of
plain text 4.5 s → 0.4 s. So that the timing tests do not merely confirm the new budgets, two
tests check the effect substantively: `test_r5_*` shows that beyond the link budget **no** URL
survives (the match becomes `[Link removed]`, and it is still counted), and
`test_r6_gewoehnliche_mail_wird_byteidentisch_verarbeitet` compares a 100 KB mail against the
same sanitizer with the pre-cut practically switched off — byte-identical. For R-4 the oracle
remains the address in the raw header; in addition the **decoder itself** is now the oracle of
the masking: for eight encoded forms (an empty charset, a language tag, B/Q in upper and lower
case, a folded header, a broken form without `?=`) no segment that `email.header.decode_header`
returns as encoded may still show `@`, `,`, `<`, `>`, `;` or `:` in the masked raw value. Five
of the new R-4 tests fail on the state before this iteration. R-7 is a measurement correction:
the previous test gave each of the four HTML parts the whole byte cap, whereupon parts 2 to 4
were discarded unparsed (1.84 s). Now each part carries a quarter of the cap and all four are
parsed — measured 1.9 to 2.6 s; the promise now reads "about 2–3 s depending on machine load"
instead of "around 2 s". The overall worst-case mail (HTML budget, plain-text pre-cut and link
budget all full at once) costs 1.0 s.

**Fourth iteration (2026-09-12).** The third iteration's skeptic again confirmed all previous
repros as dead and demonstrated four new points (carried above as R-8 to R-11). The new rule of
this iteration: **every new or changed regex and every loop in the hot path gets a measurement
series (n, 2n, 4n)** — R-8 was exactly the case the third iteration had built in without one.
R-8 is a regression from the R-4 fix: the `.*?` shaped like `email.header.ecre` may run past
`?`, and without a closing `?=` every start position scans the whole remaining text. Measurement
series for `_mask_encoded_words` on broken words, n = 25,000 / 50,000 / 100,000 / 200,000:
before 21.4 s / 80.7 s / abort, after 0.000 / 0.000 / 0.001 / 0.001 s; with valid words 0.012 /
0.028 / 0.057 / 0.119 s (linear). End to end: a 156 KiB `From` in ingest 18.2 s → 0.00 s, a
160 KiB `Subject` 8.6 s → 0.00 s. So that the timing test does not merely confirm the new header
upper bound, `test_r8_maskierung_ist_linear` checks the curve **without** the bound (eight times
the size, at most sixteen times the time — quadratic would be sixty-four times), and the
guiding principle "never narrower than the decoder" stays secured by the unchanged oracle test
`test_hc2_2_maskierung_ist_nicht_enger_als_der_dekoder`. For R-9 the oracle is still the address
in the raw header, or `email.policy.default`; all four forms yield `from_domain=''`,
`(unknown sender)` and both warnings at `False` before the fix. R-10 and R-11 are claims about
time and were measured before and after on the same scripts (`sk3_max.py`, `sk3_parts.py`,
`sk3_pdf.py`): the overall worst-case mail of 21.45 MB 9.22 s → 2.70 s, 200,000 MIME parts
3.69 s → 1.81 s, three PDF attachments 60.1 s → 30.1 s wall time, twenty PDF attachments
computed at ~400 s → 30.3 s. Measurement series for these: 20 text attachments of 480,000
characters (n/2n/4n = 5/10/20) before 1.64 / 2.53 / 5.44 s, after 0.28 / 0.46 / 0.49 s; MIME
parts 2000/4000/8000 before 0.03 / 0.06 / 0.12 s, after 0.02 / 0.04 / 0.10 s (parsing dominates
there, which no bound can avert). For R-11 the unit test does not measure the wall clock but the
**time limits granted** (`[20.0; 10.0]` instead of three times 20.0) and the number of calls —
deterministic and in milliseconds, with a clock double instead of real child processes.

**Fifth iteration (2026-09-12).** The fourth iteration's skeptic confirmed all previous repros
as dead and demonstrated three new points (carried above as S-1 to S-3). S-1: the cap from R-8
applied to the reported instance, not to the class — `To` ran uncapped through `getaddresses`,
and on measuring all read sites the genuinely expensive one showed up: `as_bytes()` in
`_raw_bytes` refolds every header of every part (250,000 headers 13.8 s, a 20 MB part header
13.9 s, 5000 parts of 4 KB 13.0 s, a 20 MB `Return-Path` 11.5 s). Measurement series for `To`
at 1/2/4/8 MB, `build_raw_mail` alone, old (state `649a9b8`) → new: 0.57 / 1.36 / 2.00 / 4.11 s
→ 0.01 s throughout (form `a@b.example, `); 0.80 / 1.60 / 3.22 / 6.51 s → 0.01 s (form `<a@b`).
The other forms: the skeptic's 20 MB `To` repro (`sk4_to2.py`) 2.41 / 5.38 / 9.58 / 18.57 s →
0.05 / 0.09 / 0.15 / 0.12 s (the rest is serialisation and the hash of the 20 MB body); Cc /
Authentication-Results / Message-ID / Date / Return-Path at 20 MB each: 1.99 / 2.41 / 0.29 /
4.12 / 11.51 s → 0.00 s; many headers or parts 13.8 / 12.5 / 13.9 / 13.0 s → 0.22 / 0.35 / 0.01
/ 0.16 s. S-2: the oracle is `email.policy.default` on the same raw header; nine unbalanced
forms yield `bank.example` without any warning in three cases before the fix, and after the fix
never a foreign domain (unknown + warning, or the real address), while eight balanced
counter-checks (nested, escaped, a comment after the address) stay `evil.example`; the new
scanner is linear (4096 … 262,144 characters: 0.0005 … 0.030 s, `test_s2_scanner_ist_linear`).
S-3 is a measurement correction justified by a profile: the standard library's parser
contributes 1.9 to 2.3 s of the 3.6 to 5.1 s, the program's own passes 1.7 to 2.2 s, the
decoding of the attachments 0.01 s.

**The pattern of the follow-up fix round, and the rule from it.** Each of the four previous
iterations opened a hole at the seam it had newly drawn, which only the skeptic found: the mask
shaped like the decoder (R-4) brought the quadratic `.*?` (R-8); the cap against R-8 sat only at
the reported header (S-1) and cut comments apart, which the new fallback from R-9 then read
(S-2); the raw budget per text chunk (R-6) was multiplied by the number of attachments (R-10),
and the worst-case figure was corrected upwards three times (R-7, R-10, S-3). The rule from now
on, in addition to the fourth iteration's measurement-series duty: **(1) every bound is built
for the class, not for the instance** — whoever caps a header caps all headers at the one read
site and then measures every other site that reads the same thing (here: the re-serialisation).
**(2) A fallback that reads more text than the main path is suspect** — fallbacks never read
comment or quoted-string content, and in case of doubt the result is "unknown + warning", never
a domain. **(3) The worst case is measured with the form densest for the parser**, not with the
form that its own budget truncates earliest, and the standard library's share is reported
separately. **(4) Before handover, all repro scripts of all iterations run against the old and
the new state.**

**An open question from HC2-1 answered.** "Does the runner pick such a mail up again after a
restart (permanent DoS)?" — No. `poll_once` reserves the dedupe key with `StateDB.claim`
**before** processing (ADR-019), and `claim` commits immediately (`with self._conn` around the
`INSERT OR IGNORE`). A hard abort in the middle of the sanitize stage leaves the row with status
`pending`; the next process gets `ClaimResult.DUPLICATE` for the same mail and skips it. An
attack mail therefore costs at most **one** cycle. Demonstrated by
`test_ingest_poll.py::test_hc2_1_kill_waehrend_der_verarbeitung_ist_kein_dauer_dos`. A `failed`
status before the sanitize stage is therefore unnecessary.

**Test count after NF-1:** 1693 (up from 1565; 1586 after the first, 1604 after the second,
1627 after the third, 1644 after the fourth iteration), runtime around 145 to 180 s depending on
machine load — the growth comes from the timing measurements of the second to fifth iteration.

### Open at the end of the follow-up fix round (skeptic of the fifth iteration, Fable 5.1, 2026-09-12)

The follow-up fix round was ended after five iterations by the user's decision. The two release
blockers HC2-1 and HC2-2 have been fixed in their reported form since the first iteration and
have stayed stable across all five skeptic runs (all repro scripts of iterations 1–5 run on
`a2feda2` without relapsing in the dangerous direction). The last skeptic demonstrated five
points that were initially **not** worked on; they stand here so that they do not live only in
the workflow journal. Before release 0.2.0, O-1 (two iterations with a skeptic) and O-3
(directly, with the skeptic's fuzz as a regression test) were closed; O-6 to O-8 are side
findings of the O-1 skeptic. The repro scripts (`sk5_*.py` … `sk7_*.py`) live in the session's
scratchpad, not in the repo.

| No. | Severity | Finding | Origin | Fix direction |
|-----|----------|--------|----------|--------------|
| O-1 | high | **fixed (2026-09-12).** A poison mail with ≥ 250 nested multipart levels (16 KB): `as_bytes()` failed with `RecursionError`, the fallback `str(msg.obj)` in `_raw_bytes` recursed again and was not caught; `build_raw_mail` threw contrary to ADR-020 (e), and `poll_once` had no try around it — continuous operation died, `run --once` failed on every run, and later mail stayed put | pre-existing (also on `649a9b8`) | **Done:** a depth cap `MAX_MIME_DEPTH` = 32 applied iteratively before any serialisation (`_cap_message_depth`, log `mail_mime_depth_capped`), a three-stage and fully caught fallback in `_raw_bytes` (last resort: headers + a note text), a guard around `build_raw_mail` in `poll_once` with a substitute `RawMail` (`ingest_failed`) ⇒ metadata note, status `failed`/`ingest_error`, the mail marked as read, the cycle continues; the sanitizer's tree walk additionally capped hard at 64 levels. ADR-020 addendum (O-1). Tests: `test_o1_tiefe_verschachtelung_wirft_nicht` (250/1000/5000), `test_o1_geparste_giftmail_wird_gedeckelt_und_bleibt_klein`, `test_o1_normale_mail_bleibt_byteidentisch`, `test_o1_raw_bytes_rueckfall_wirft_nie`, `test_o1_sanitizer_parse_der_giftmail_ist_gedeckelt` (unit/test_ingest_rawmail.py), `test_o1_unlesbare_mail_wird_zur_notiz` (unit/test_pipeline.py), `test_o1_poll_once_ueberlebt_die_giftmail`, `test_o1_beliebiger_fehler_vor_process_wird_zur_notiz`, `test_o1_ersatzkey_ohne_lesbare_header_ist_stabil` (integration/test_ingest_poll.py), `test_o1_run_forever_stirbt_nicht` (integration/test_runner_e2e.py). **Remaining case (skeptic, second iteration): closed.** From 984 `message/rfc822` levels (31,569 B) onwards, `MailMessage.__init__` in imap-tools already failed inside the `fetch` generator; reinterpreting that as `ImapConnectionError` turned it into an endless backoff loop (the mail never claimed, never seen; `run --once` "Mailbox unreachable"). Now fetching happens **per UID** (`uids()` + `fetch(uid_list=…)`, the same command count 1 + 2n per cycle, measured at n = 10/20/40), the parse is isolated per mail, and headers are substituted through a capped `UID FETCH (BODY.PEEK[HEADER]<0.262144> …)` ⇒ `UnparsableMailMessage` ⇒ note, `failed`/`ingest_error`, seen; log `mail_unparsable`; real connection errors (including `imaplib.IMAP4.error`) stay `ImapConnectionError`. ADR-020 addendum (second iteration), ADR-064 addendum. Tests: `test_o1b_fetch_unseen_isoliert_unparsbare_mail`, `test_o1b_echter_verbindungsfehler_bleibt_verbindungsfehler` (3 classes), `test_o1b_search_fehler_ist_ein_verbindungsfehler`, `test_o1b_kopfzeilen_abruf_scheitert_auch`, `test_o1b_unparsbare_mail_ohne_kopfzeilen_antwort`, `test_o1b_verschwundene_uid_wird_uebersprungen` (unit/test_ingest_client.py), `test_o1b_unparsbare_mail_blockiert_den_poll_nicht`, `test_o1b_echte_giftmail_rfc822_tiefe_990`, `test_o1b_header_abruf_scheitert_auch`, `test_o1b_nur_uid_kommandos_kein_expunge`, `test_o1b_kommandozahl_je_zyklus` (10/20/40) (integration/test_ingest_poll.py), `test_o1b_run_forever_drei_zyklen`, `test_o1b_run_once_bilanz_und_exitcode` (integration/test_runner_e2e.py) |
| O-2 | high | A flood of parts without headers: 2 million empty MIME parts in 20 MB cost 34 s of CPU (the parser 12.6 s before any budget, `build_raw_mail` 8.4 s, the sanitizer parse 13.2 s), linear and uncapped — the header cap does not bite, because parts without a header consume no budget | pre-existing | Only avertable before parsing: counting boundaries on the raw bytes of the fetch (an intervention in the fetch path, an ADR needed) or a smaller default for `max_mail_bytes`; documented as a limitation until then |
| O-3 | high | **fixed (2026-09-12 to 2026-09-14, before release 0.2.0, six attempts).** The first attempt (a mask mirroring the parser's word form) was refuted by the skeptic: token boundaries narrower than the parser's (after `.`, `:`, `<`, `\\`), and a Q-word with a literal `@` replaced the domain again. Second attempt: the standard library's RFC 5322 parser (`email.headerregistry`) reads display name and address itself — the mask, bracket fallback and comment scanner are removed; third attempt after the second skeptic: the quoted `addr_spec` and the domain straight from the parser, guarded legacy parsers, the Outlook form without a false alarm; fourth attempt after the third skeptic: `RawMail.from_address`/`reply_to_address` from the parser, and the sanitizer compares those instead of parsing again (`"a["@…`); fifth attempt after the fourth skeptic: all Reply-To addresses count, the display's address fallback is scrubbed, and the Outlook display is built only from plain words; sixth attempt after the fifth skeptic (a regression of the fifth): unusable Reply-To entries stay in the list as unknowns, and entities in the display name are resolved before the scrub; only the first entry counts, domains must be hostname-shaped, and an unreadable header stays visible as `(unreadable)` (the warning fires). Regression test: the skeptic's 45 header forms in `test_ingest_rawmail.py::test_o3_*` with `email.policy.default` as the oracle — never a domain the mail program does not show, never "unknown" without a warning. ADR-020 (A, O-3). Originally: an irregularly encoded word with `?` or `(` in the encoded-text (`=?utf-8?Q?Support?(?=`) gets masked and thereby hides the bracket from the comment scanner and `getaddresses`; the tool shows `bank.example` without a warning, the oracle `real@evil.example` | since `b7d093d` | A mask strictly per RFC 2047 (encoded-text without `?` and without spaces, the word separated by whitespace or the start) — or run the comment scanner on the raw text before masking |
| O-4 | medium | The overall worst case of 3.6–4.1 s CPU in the sanitizer is documented, but the end-to-end promise "around 8 s" (ADR-084, fourth addendum) is exceeded sevenfold by O-2 | docs | Re-measure after O-2 and set the promise in ADR-084 to the most expensive permitted mail |
| O-5 | low | Unmasking: `MDENCWORD1` also replaces the prefix of `MDENCWORD10…19`; display names with more than ten encoded words get mangled (no security relevance, the address is fixed beforehand) | since `649a9b8` | Replace the longest tokens first, or add a separator after the number |
| O-6 | medium | A side finding of the second O-1 iteration (skeptic Fable 5.1): the placeholder of an unparsable mail claims the bare Message-ID with an unknown content hash — a later genuine mail with the same Message-ID is silently suppressed as a duplicate (`mail_duplicate`, no delivery); the collision protection from ADR-079 does not bite because `content_hash` is empty | since `eef74d4` | Never claim the placeholder with the bare Message-ID: always the UID/INTERNALDATE-bound substitute key, or a sentinel content hash over the fetched header bytes, so that the genuine mail is processed as a collision under a derived key; test: a poison mail with Message-ID X, then a genuine mail X ⇒ processed |
| O-7 | medium | If the server answers the `UID FETCH` of a single mail with `NO` permanently (a server-side broken mail), that mail still blocks the fetch endlessly: `MailboxFetchError` → `ImapConnectionError` → backoff, `run --once` reports "Mailbox unreachable", and the mail behind it never runs | pre-existing, made visible by O-1 | A `NO` on the FETCH of one UID with a working SEARCH is a property of the mail, not of the connection: book it as a placeholder like an unparsable mail (header fetch, otherwise the UID key), note, seen — at minimum skip it and report it with a log event of its own (UID hash, status) |
| O-8 | low | The substitute dedupe key of an unparsable mail without a Message-ID contains the IMAP sequence number from the raw FETCH response; after a connection abort between the claim and the STORE, and an expunge by a foreign client, it shifts, and the same poison mail is booked and delivered twice | since `eef74d4` | Build the key only from mail-bound values: UID + the INTERNALDATE and RFC822.SIZE parsed from the response + the header bytes, not the raw response line |
| O-9 | low | A forged marker without a colon (`[Link #9]`, `[Mail #3]`) in the display name is neither neutralised nor counted as `forged_markers`; in the prompt data block it stands like a genuine reference, and in the delivery as `Link #9` (fifth O-3 skeptic, 2026-09-14) | pre-existing | Apply `neutralize_forged_markers` or the link scrub to `[Link #n]`/`[Mail #n]` without a colon as well, and count them |
| O-10 | low | HTML entity chains from nine levels onwards (`&amp;amp;…#60;`) in the display name: the sanitizer resolves eight rounds, the composer three — the remainder stands simply encoded in the prompt data block (`&#60;…MAILDIGEST-END…`), the delivery is clean and a forged marker would need the run token (sixth O-3 skeptic, 2026-09-14) | pre-existing | Remove entity sequences (`&#?\w+;`) left after the last round instead of leaving them, or decode equally deeply in both layers |
| O-11 | low | RFC-violating, unquoted phrases with `;` or `[` in the Reply-To (`Bank &amp; Co <x@…>`, `Support [Ticket #123] <x@…>`) are read as an address by neither the parser nor the oracle — a warning, even though the mail program may well show the one address; 6 of 100 benign forms, the same in every state (sixth O-3 skeptic) | pre-existing | Accept it (the warning is the safe direction), or quote the phrase before parsing when exactly one `<…>` with a usable address follows |

Assessment: with the skeptics' yardstick ("fixed only when no path exists any more that violates
the protection goal"), every iteration found a further hole at the newly drawn seam or in a
previously unmeasured class. The round was therefore ended rather than iterated further. **O-1
has since been fixed** (2026-09-12, ADR-020 addendum; see the row above — the remainder the
skeptic measured from 984 `message/rfc822` levels onwards was closed in the second iteration by
fetching per UID); O-2 is a design decision at the fetch, and O-3 and O-5 are small corrections
to the mask.
