# Plan: delivery through apt and dnf

As of 2026-09-16. Goal: besides the source on GitHub, MailDigest should be installable from
a package repository of our own — `apt install maildigest` on Debian derivatives,
`dnf install maildigest` on Fedora — and **every new release should carry both repositories
along automatically**, triggered by the same git tag that already marks the release today.

This plan describes the target architecture, the work, the steps only the copyright holder
can take, and the alternatives deliberately not chosen. The decision itself is ADR-088 in
[DECISIONS.md](DECISIONS.md) (German, like the rest of that record).

## 1. What the user ends up typing

Debian, Ubuntu, Kali — add the repository once, then live with `apt` from there on:

```bash
curl -fsSL https://kpafi.github.io/maildigest/apt/maildigest-archive-keyring.gpg \
  | sudo tee /usr/share/keyrings/maildigest-archive-keyring.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/maildigest-archive-keyring.gpg] https://kpafi.github.io/maildigest/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/maildigest.list
sudo apt update && sudo apt install maildigest
```

Fedora — enable the COPR project once:

```bash
sudo dnf copr enable kpafi/maildigest
sudo dnf install maildigest
```

From then on `apt upgrade` and `dnf upgrade` carry new versions along without the user ever
having to think about this project again. That is the real gain over `pipx install .` — not
the first installation, but the updates.

## 2. Target architecture

```
git tag v0.3.0  ──►  .github/workflows/release.yml
                        │
                        ├─ builds sdist + wheel      ──►  GitHub release (assets)
                        │
                        ├─ builds the .deb in a Debian container
                        │     └─ install test on debian:trixie, ubuntu:24.04, kalilinux
                        │           └─ signed + filed into gh-pages/apt  ──►  GitHub Pages
                        │
                        └─ calls the COPR webhook  ──►  COPR builds the .rpm from
                                                        packaging/rpm/maildigest.spec
                                                     └─ COPR signs and publishes on its own
```

The single source of truth for the version number stays `version` in `pyproject.toml`. The
tag has to match it; the workflow aborts when `v<version>` and the tag drift apart. Neither
`debian/changelog` nor the `.spec` carries a maintained version number — both are generated
during the run from the tag and `CHANGELOG.md`. That leaves no second place to forget on
release day.

## 3. Dependencies — the decisive finding

A distribution package draws its dependencies from the distribution, not from PyPI. If even
one is missing there, it has to be packaged as well. Survey of 2026-09-16:

| Runtime dependency | Debian/Kali | Fedora |
|---|---|---|
| `imap-tools` | `python3-imap-tools` 1.10.0 | **missing** |
| `httpx` | `python3-httpx` 0.28.1 | `python3-httpx` |
| `pydantic` | `python3-pydantic` 2.13.4 | `python3-pydantic` |
| `beautifulsoup4` | `python3-bs4` 4.15.0 | `python3-beautifulsoup4` |
| `lxml` | `python3-lxml` 6.1.0 | `python3-lxml` |
| `pdfminer.six` | `python3-pdfminer` 20260107 | `python3-pdfminer` 20260107 |

So for apt there is **nothing** to rebuild. For dnf exactly one package is missing:
`imap-tools`. It goes into the same COPR project as a second package, through COPR's
built-in source type **PyPI**: COPR generates the spec with `pyp2spec` itself and rebuilds
it at the push of a button. No spec in this repository, no maintenance — and since COPR
resolves dependencies within a project, `maildigest.spec` needs no special handling at all.

The version constraints in `pyproject.toml` stay deliberately open (no upper bounds).
Whether the distribution versions really carry the program is not assumed but **measured**:
the workflow installs the built `.deb` in every target container and runs `maildigest --help`
and `maildigest --man` there. If that fails, there is no release.

Two findings from the first run belong here, because both were invisible until the package
was really built. `dh_python3` could not map the dist name `pdfminer.six` to
`python3-pdfminer` and dropped the dependency **without failing the build** — the package
shipped five of its six dependencies. Nothing noticed, because `pdfminer` is imported
lazily inside a subprocess (`sanitize/extract_pdf.py`), so `maildigest --help` runs
happily and PDF extraction would have been dead on the user's machine. It is therefore
spelled out in `debian/control`, and the install test now imports every runtime dependency
instead of only starting the program.

The same applies to the lower bounds: `dh_python3` emits unversioned dependencies even
where `pyproject.toml` states a minimum, so `python3-pydantic (>= 2)` and
`python3-imap-tools (>= 1.0)` are written out in `debian/control` as well. Both findings
have the same shape — the tool drops what it cannot resolve and the build stays green —
which is why the package's own `Depends` line is read back from the published index after
every run rather than taken on trust. The second finding is Ubuntu in section 4.

That install test is also the reason `debian/rules` switches pybuild's own test step off.
pybuild would run `python3 -m unittest discover` right after the build, which imports the
package — but the runtime dependencies are `Depends`, not `Build-Depends`, so they are
absent from the build container and that import can only fail. Adding them as build
dependencies would buy an import check in the build directory, where nothing is being
measured that the install test does not measure better on the real, installed package.

## 4. Reach and limits

A `.deb` is not portable across distributions, even though `Architecture: all` suggests it
is. What is supported is what the install test confirms; the list goes into the README
afterwards:

- **Debian 13 (trixie) and newer** — carries all six dependencies, blocking in the
  install test.
- **Kali Rolling** — same base, green in every run so far.
- **No Ubuntu release** — not through apt. Measured on the first runs and then checked
  against the Ubuntu archive:

  | Ubuntu | `imap-tools` | `pydantic` | usable |
  |---|---|---|---|
  | 22.04 LTS | 0.50 | 1.x | no |
  | 24.04 LTS | 0.54 | 1.10 | no |
  | 25.04 | absent | 2.x | no |
  | 25.10 | 1.10 | 2.x | yes |
  | 26.04 LTS | 1.10 | 2.12 | yes |

  24.04 installed cleanly in the first run and then failed to start: it ships pydantic
  1.10, and the code needs pydantic 2 (`ConfigDict`, `model_validator`). That is what the
  lower bounds in `debian/control` are for — apt now refuses the installation instead of
  creating one that cannot run.

  26.04 LTS carries every dependency in a usable version and should work, but the probe was
  red and the reason was **not investigated** — a deliberate decision, not a finding. Until
  someone reads that log, Ubuntu is not claimed as supported and goes through `pipx`, where
  pip enforces the same bounds from `pyproject.toml`. The probe was taken out of the matrix
  rather than left permanently red, because a check that is always red teaches people to
  ignore red.

A single suite directory `stable` is enough as long as one package serves every supported
system. Should that fall apart later (because Ubuntu needs an older dependency, say), it
becomes two suites — the directory layout already allows for that.

## 5. Layout of the apt repository

Hosted on GitHub Pages from the `gh-pages` branch of this same repository. No server, no
cost, HTTPS and CDN included.

```
apt/
├── dists/stable/
│   ├── Release            (generated by apt-ftparchive)
│   ├── Release.gpg        (detached signature)
│   ├── InRelease          (embedded signature — what apt fetches today)
│   └── main/binary-all/Packages{,.gz}
├── pool/main/m/maildigest/maildigest_0.3.0_all.deb   (older versions stay put)
└── maildigest-archive-keyring.gpg
```

Generated with `apt-ftparchive` rather than with `reprepro` or `aptly`: both keep a state
database that would have to survive between two CI runs — state that has to be restored
artificially on a stateless runner. `apt-ftparchive` instead recomputes the indices on
every run from whatever lies in the pool; the pool lives on the `gh-pages` branch and is
therefore versioned, auditable and recoverable.

Demonstrated on 2026-09-16 with a dummy package and a throwaway key: `apt update` fetches
`InRelease`, verifies the signature without complaint, reads both versions that were filed
in and picks the newer one as the installation candidate. What is still outstanding is the
build of the real `.deb` — that needs `dh-python`, which is (still) missing on this machine,
and otherwise runs in the workflow's container.

Old versions are not deleted. Whoever pins an older version should be able to keep it, and
the disk footprint of a pure Python package is negligible.

## 6. Signature

A package repository of one's own without a signature is an invitation: whoever controls
the transport decides what gets executed on the user's machine. `apt` rightly refuses
unsigned repositories. Therefore:

- A **key pair of its own, for this repository only**, separate from any key that signs
  commits or tags. If it is compromised, exactly this repository is affected and nothing
  else.
- The private key lives **without a passphrase** as the GitHub Actions secret
  `APT_GPG_PRIVATE_KEY` — an unattended CI cannot type a passphrase. That is the conscious
  price of the automation, and the reason the key is allowed to do nothing but sign this
  repository.
- The public key is shipped as a **dearmored** keyring
  (`maildigest-archive-keyring.gpg`) under `/usr/share/keyrings` and bound via `signed-by`
  in the `sources.list` line to **this one repository**. No `apt-key`, no key in the global
  trust store: a key that may sign anything on the system has rightly been deprecated since
  Debian 11.

The key is generated by the copyright holder personally (section 8), not in the repository
and not by a tool — a private key someone else generated is not a private key.

## 7. What is added to the repository

| File | Purpose |
|---|---|
| `debian/control` | package name, dependencies, description |
| `debian/rules` | build rule (`dh` + `pybuild`, detects Hatchling on its own; the build-time test step is off, see section 3) |
| `debian/copyright` | machine-readable format 1.0, MIT, copyright holder kpafi |
| `debian/maildigest.manpages` | puts `man/maildigest.1` into `/usr/share/man/man1` |
| `debian/source/format` | `3.0 (native)` — upstream and downstream are the same project |
| `packaging/deb/build.sh` | generates `debian/changelog` from the tag, builds the `.deb` |
| `packaging/deb/publish.sh` | files into the pool, generates and signs the indices |
| `packaging/rpm/maildigest.spec` | Fedora package via the `pyproject` macros |
| `.copr/Makefile` | builds the source package inside COPR from the git tree |
| `.github/workflows/release.yml` | the whole flow from section 2, triggered by the tag |

This finally puts the manual page where `man maildigest` finds it without help — the detour
via `~/.local/share/man` from the README falls away for package users.

## 8. Steps only the copyright holder can take

No script and no agent can take these over; they need the GitHub account or they create a
secret.

0. **Check the write permission of the actions**: *Settings → Actions → General → Workflow
   permissions* has to be on *Read and write permissions* — otherwise the run may not write
   to the `gh-pages` branch.
1. **Generate the signing key** (locally, once):
   ```bash
   gpg --batch --pinentry-mode loopback --passphrase "" \
     --quick-generate-key "MailDigest Repository Signing Key <samukrapf@gmail.com>" rsa4096 sign never
   ```
   `--passphrase ""` is not an oversight: the key has to sign unattended in CI (section 6).
   That is why it is allowed to do nothing else.
2. **Store the private key as a secret**: print it with
   `gpg --armor --export-secret-keys <ID>` and paste the block under *Settings → Secrets
   and variables → Actions* as `APT_GPG_PRIVATE_KEY`. The block must not leave the terminal
   and belongs in no file in the repository.
3. **Nothing further about the key** — the workflow exports the public part from the
   private one on every run and places it in the repository as
   `maildigest-archive-keyring.gpg`. That way the signature and the published key cannot
   drift apart.
4. **Create the `gh-pages` branch and switch GitHub Pages on**: the Pages setting can only
   pick a branch that already exists, so the branch has to be there before the setting (an
   empty root commit is enough, the workflow fills it). Then *Settings → Pages → Source:
   Deploy from a branch → `gh-pages` / `/ (root)`*.
5. **Create the COPR project** on copr.fedorainfracloud.org (project `maildigest`, chroots
   `fedora-rawhide-x86_64` and the two current stable ones), and two packages inside it:
   `imap-tools` with source type **PyPI** (section 3) and `maildigest` as an **SCM** package
   pointing at this repository, build method `make_srpm`. Under *Settings → Integrations*
   COPR shows a **Custom** webhook URL of the form
   `https://copr.fedorainfracloud.org/webhooks/custom/<id>/<token>/maildigest/` — exactly
   that one (ending in `maildigest`) becomes the secret `COPR_WEBHOOK_URL`. The GitHub
   variant on the same page is wrong here: it expects a GitHub payload and would build on
   every push instead of only after a passed install test.

Step 5 is dispensable if dnf should wait for now — the workflow skips the COPR call as long
as the secret is missing, and the apt repository works independently of it.

## 9. How a release runs from then on

```bash
# 1. raise the version in pyproject.toml, write the CHANGELOG.md section
# 2. regenerate the manual page if the CLI changed
# 3.
git commit -am "Release 0.3.0" && git tag v0.3.0 && git push --follow-tags
```

Everything else happens without intervention: wheel and sdist on the GitHub release, the
`.deb` built, test-installed, signed, pushed to the Pages branch, COPR triggered. A failing
step aborts the run before anything is published — a broken package in a repository costs
more than a release that did not happen.

## 10. Alternatives rejected

- **Inclusion in Debian/Fedora themselves.** Needs an ITP bug, a sponsor and policy
  conformance; after that every version hangs on the distribution's release cycle and
  freezes in stable for years. For a project of this size, out of proportion.
- **`reprepro`/`aptly` instead of `apt-ftparchive`.** See section 5: a state database
  without state.
- **Bundling every dependency into the package** (a venv under `/opt`). It solves the
  version problem but betrays the point of a distribution package: security updates for
  `lxml` or `httpx` would pass MailDigest by.
- **`apt-key`, or the key in the global trust store.** Deprecated, and for good reason: see
  section 6.

## 11. Switch the README over only afterwards

Until the first **green** release run, the installation section of the README keeps `pipx`
as the only way. Instructions pointing at a still-empty repository produce a `404` for the
user and the suspicion that something is being promised here that does not exist. As soon
as `https://kpafi.github.io/maildigest/apt/dists/stable/InRelease` can be fetched, the
blocks from section 1 of this plan move to the top of the installation section, `pipx` moves
to second place ("for systems without the repository, and for development"), and the
supported distributions from section 4 are named there.
