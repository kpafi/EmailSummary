# Contributing

MailDigest is a one-person project with a fixed security model. Bug reports, findings and
small fixes are welcome; larger changes are better discussed in an issue first, because
the invariants in [docs/SECURITY.md](docs/SECURITY.md) decide what can go in and every
design change gets an ADR in [docs/DECISIONS.md](docs/DECISIONS.md).

## Reporting a bug

Open an issue with the *Bug report* template: version, steps, observed, expected. That is
the finding format from [docs/TESTING.md](docs/TESTING.md) §3, and a report in that shape
can usually be reproduced without a round trip. Most processing bugs reproduce with a
stripped-down `.eml` file and no infrastructure at all:

```bash
maildigest test --eml the-mail.eml --dry-run
```

Never paste a real mail, a password, an API key or a bot token into an issue.

## Reporting a security problem

Not as a public issue. Use a private
[security advisory](https://github.com/kpafi/maildigest/security/advisories/new);
what counts as a security problem and what to expect is in
[docs/SECURITY.md](docs/SECURITY.md) §8.

## Working on the code

```bash
git clone https://github.com/kpafi/maildigest && cd maildigest
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests && mypy src && pytest -q
```

That is exactly what CI runs. Three things every change has to keep:

* **The invariants I1–I8** in docs/SECURITY.md §7. A change that weakens one is not a
  bug fix, it is a design change and needs an ADR.
* **The CLI contract** in [docs/SPEC-CLI.md](docs/SPEC-CLI.md): every prompt, output
  line, config field and exit code is specified there, and the manual page is generated
  from the same source (`maildigest --man`); a test keeps them in sync.
* **A test for the bug.** Fixes to the sanitizer or the output check also get a sample in
  the attack corpus under `tests/corpus/`.

Runtime dependencies are deliberately few (NF-1/NF-2 in docs/REQUIREMENTS.md); a pull
request that adds one should say why the standard library does not do.

## Language

Code comments and the historical records under `docs/` are partly German; new code,
tests, README and the contract documents are English. Either language is fine in issues.
