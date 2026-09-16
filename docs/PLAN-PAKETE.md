# Plan: Auslieferung über apt und dnf

Stand 2026-09-16. Ziel: Neben dem Quelltext auf GitHub soll MailDigest aus einer eigenen
Paketquelle installierbar sein — `apt install maildigest` auf Debian-Derivaten,
`dnf install maildigest` auf Fedora — und **jedes neue Release beide Quellen automatisch
mitziehen**, ausgelöst durch denselben Git-Tag, der heute schon das Release markiert.

Dieser Plan beschreibt die Zielarchitektur, die Arbeitsschritte, die Handgriffe, die nur
der Rechteinhaber selbst tun kann, und die bewusst nicht gewählten Alternativen. Die
Entscheidung selbst steht als ADR-088 in [DECISIONS.md](DECISIONS.md).

## 1. Was der Nutzer am Ende tippt

Debian, Ubuntu, Kali — einmalig die Quelle eintragen, danach dauerhaft über `apt`:

```bash
curl -fsSL https://kpafi.github.io/EmailSummary/apt/maildigest-archive-keyring.gpg \
  | sudo tee /usr/share/keyrings/maildigest-archive-keyring.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/maildigest-archive-keyring.gpg] https://kpafi.github.io/EmailSummary/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/maildigest.list
sudo apt update && sudo apt install maildigest
```

Fedora — einmalig das COPR-Projekt freischalten:

```bash
sudo dnf copr enable kpafi/maildigest
sudo dnf install maildigest
```

Ab da liefert `apt upgrade` beziehungsweise `dnf upgrade` neue Versionen mit, ohne dass
der Nutzer je wieder etwas von diesem Projekt wissen muss. Das ist der eigentliche Gewinn
gegenüber `pipx install .` — nicht die Erstinstallation, sondern die Aktualisierung.

## 2. Zielarchitektur

```
Git-Tag v0.3.0  ──►  .github/workflows/release.yml
                        │
                        ├─ baut sdist + Wheel        ──►  GitHub Release (Anhänge)
                        │
                        ├─ baut .deb im Debian-Container
                        │     └─ Einbauprobe in debian:trixie, ubuntu:24.04, kalilinux
                        │           └─ signiert + in gh-pages/apt einsortiert  ──►  GitHub Pages
                        │
                        └─ ruft COPR-Webhook  ──►  COPR baut .rpm aus packaging/rpm/maildigest.spec
                                                     └─ COPR signiert und veröffentlicht selbst
```

Einzige Wahrheitsquelle für die Versionsnummer bleibt `version` in `pyproject.toml`. Der
Tag muss dazu passen; der Workflow bricht ab, wenn `v<version>` und Tag auseinanderlaufen.
Weder `debian/changelog` noch die `.spec` tragen eine gepflegte Versionsnummer — beide
werden im Lauf aus Tag und `CHANGELOG.md` erzeugt. Damit gibt es keine zweite Stelle, die
man beim Release vergessen kann.

## 3. Abhängigkeiten — der entscheidende Befund

Ein Distributionspaket zieht seine Abhängigkeiten aus der Distribution, nicht aus PyPI.
Fehlt dort auch nur eine, muss sie mitpaketiert werden. Erhebung vom 2026-09-16:

| Laufzeit-Abhängigkeit | Debian/Kali | Fedora |
|---|---|---|
| `imap-tools` | `python3-imap-tools` 1.10.0 | **fehlt** |
| `httpx` | `python3-httpx` 0.28.1 | `python3-httpx` |
| `pydantic` | `python3-pydantic` 2.13.4 | `python3-pydantic` |
| `beautifulsoup4` | `python3-bs4` 4.15.0 | `python3-beautifulsoup4` |
| `lxml` | `python3-lxml` 6.1.0 | `python3-lxml` |
| `pdfminer.six` | `python3-pdfminer` 20260107 | `python3-pdfminer` 20260107 |

Für apt ist damit **nichts** nachzubauen. Für dnf fehlt genau ein Paket: `imap-tools`.
Es kommt als zweites Paket ins selbe COPR-Projekt, und zwar über COPRs eingebaute
Quellart **PyPI**: COPR erzeugt das Spec mit `pyp2spec` selbst und baut es auf Knopfdruck
neu. Kein Spec in diesem Repo, keine Pflege — und da COPR Abhängigkeiten innerhalb eines
Projekts auflöst, braucht `maildigest.spec` keinerlei Sonderbehandlung.

Die Versionsangaben in `pyproject.toml` bleiben absichtlich offen (keine oberen Schranken).
Ob die Distributionsstände wirklich tragen, wird nicht vermutet, sondern **gemessen**: Der
Workflow installiert das gebaute `.deb` in jedem Zielcontainer und ruft `maildigest --help`
und `maildigest --man` auf. Schlägt das fehl, gibt es kein Release.

## 4. Reichweite und Grenzen

Ein `.deb` ist nicht distributionsübergreifend, auch wenn `Architecture: all` das nahelegt.
Getragen wird, was die Einbauprobe bestätigt; die Liste steht danach im README:

- **Debian 13 (trixie) und neuer** — trägt alle sechs Abhängigkeiten.
- **Kali Rolling** — dieselbe Basis, hier lokal verifiziert.
- **Ubuntu 24.04 LTS und neuer** — wird in der Probe geprüft; fällt `python3-imap-tools`
  dort aus, wird Ubuntu erst ab der Version genannt, die es hat, und der README sagt das
  offen, statt einen Fehlschlag beim Nutzer zu provozieren.
- **Ubuntu 22.04 und älter, Debian 12** — ausdrücklich **nicht** unterstützt (Python < 3.11
  beziehungsweise zu alte Abhängigkeiten). Dort bleibt `pipx` der Weg.

Ein einziges Suite-Verzeichnis `stable` reicht, solange ein Paket alle getragenen Systeme
bedient. Zerfällt das später (etwa weil Ubuntu eine ältere Abhängigkeit braucht), werden
daraus zwei Suites — die Verzeichnisstruktur des Repos sieht das schon vor.

## 5. Aufbau des Apt-Repos

Gehostet wird auf GitHub Pages aus dem Zweig `gh-pages` desselben Repos. Kein Server, keine
Kosten, HTTPS und CDN inklusive.

```
apt/
├── dists/stable/
│   ├── Release            (von apt-ftparchive erzeugt)
│   ├── Release.gpg        (abgetrennte Signatur)
│   ├── InRelease          (eingebettete Signatur — das, was apt heute nimmt)
│   └── main/binary-all/Packages{,.gz}
├── pool/main/m/maildigest/maildigest_0.3.0_all.deb   (ältere Versionen bleiben liegen)
└── maildigest-archive-keyring.gpg
```

Erzeugt mit `apt-ftparchive` statt mit `reprepro` oder `aptly`: Beide führen eine
Zustandsdatenbank, die zwischen zwei CI-Läufen überleben müsste — Zustand, den man in
einem zustandslosen Runner künstlich wiederherstellen muss. `apt-ftparchive` berechnet die
Indizes stattdessen bei jedem Lauf neu aus dem, was im Pool liegt; der Pool liegt im
Zweig `gh-pages` und ist damit versioniert, nachvollziehbar und wiederherstellbar.

Nachgewiesen am 2026-09-16 mit einem Probepaket und einem Wegwerfschlüssel: `apt update`
holt `InRelease`, prüft die Signatur ohne Beanstandung, liest beide eingespielten
Versionen und wählt die neuere als Installationskandidaten. Was noch aussteht, ist der
Bau des echten `.deb` — der braucht `dh-python`, das auf diesem Rechner (noch) fehlt, und
läuft sonst im Container des Workflows.

Alte Versionen werden nicht gelöscht. Wer eine ältere Version festhält, soll sie behalten
können, und der Platzbedarf eines reinen Python-Pakets ist vernachlässigbar.

## 6. Signatur

Eine eigene Paketquelle ohne Signatur ist eine Einladung: Wer den Transportweg
kontrolliert, bestimmt, was auf dem Rechner des Nutzers ausgeführt wird. `apt` verweigert
unsignierte Quellen zu Recht. Deshalb:

- Ein **eigenes Schlüsselpaar nur für diese Paketquelle**, getrennt von jedem Schlüssel,
  der Commits oder Tags signiert. Wird es kompromittiert, ist genau diese Quelle
  betroffen, sonst nichts.
- Der private Schlüssel liegt **ohne Passphrase** als GitHub-Actions-Secret
  `APT_GPG_PRIVATE_KEY` — eine unbeaufsichtigte CI kann keine Passphrase eingeben. Das ist
  der bewusste Preis der Automatisierung, und der Grund, warum der Schlüssel nichts anderes
  darf als dieses Repo zu signieren.
- Der öffentliche Schlüssel wird als **entpanzerter** Keyring
  (`maildigest-archive-keyring.gpg`) unter `/usr/share/keyrings` abgelegt und in der
  `sources.list`-Zeile per `signed-by` **genau dieser einen Quelle** zugeordnet. Kein
  `apt-key`, kein Schlüssel im globalen Vertrauensvorrat: Ein Schlüssel, der alles im
  System signieren darf, ist seit Debian 11 zu Recht abgekündigt.

Erzeugt wird der Schlüssel vom Rechteinhaber selbst (Abschnitt 8), nicht im Repo und nicht
durch ein Werkzeug — ein privater Schlüssel, den jemand anderes erzeugt hat, ist kein
privater Schlüssel.

## 7. Was im Repo entsteht

| Datei | Zweck |
|---|---|
| `debian/control` | Paketname, Abhängigkeiten, Beschreibung |
| `debian/rules` | Bauregel (`dh` + `pybuild`, erkennt Hatchling von selbst) |
| `debian/copyright` | Maschinenlesbares Format 1.0, MIT, Rechteinhaber kpafi |
| `debian/maildigest.manpages` | legt `man/maildigest.1` nach `/usr/share/man/man1` |
| `debian/source/format` | `3.0 (native)` — Ober- und Unterlauf sind dasselbe Projekt |
| `packaging/deb/build.sh` | erzeugt `debian/changelog` aus Tag und Changelog, baut das `.deb` |
| `packaging/deb/publish.sh` | sortiert ins Pool ein, erzeugt und signiert die Indizes |
| `packaging/rpm/maildigest.spec` | Fedora-Paket über `pyproject`-Makros |
| `.copr/Makefile` | baut das Quellpaket im COPR aus dem Git-Stand |
| `.github/workflows/release.yml` | der ganze Ablauf aus Abschnitt 2, ausgelöst vom Tag |

Die Handbuchseite landet dadurch endlich dort, wo `man maildigest` sie ohne Zutun findet —
der Umweg über `~/.local/share/man` aus dem README entfällt für Paketnutzer.

## 8. Handgriffe, die nur der Rechteinhaber tun kann

Diese fünf Schritte kann kein Skript und kein Agent übernehmen; sie brauchen den
GitHub-Zugang beziehungsweise erzeugen ein Geheimnis.

0. **Schreibrecht der Actions prüfen**: *Settings → Actions → General → Workflow
   permissions* muss auf *Read and write permissions* stehen — sonst darf der Lauf den
   Zweig `gh-pages` nicht beschreiben.
1. **Signierschlüssel erzeugen** (lokal, einmalig):
   ```bash
   gpg --batch --pinentry-mode loopback --passphrase "" \
     --quick-generate-key "MailDigest Repository Signing Key <samukrapf@gmail.com>" rsa4096 sign never
   ```
   `--passphrase ""` ist kein Versehen: Der Schlüssel muss unbeaufsichtigt in der CI
   signieren können (Abschnitt 6). Er darf deshalb nichts anderes tun als das.
2. **Privaten Schlüssel als Secret hinterlegen**: `gpg --armor --export-secret-keys <ID>`
   ausgeben und den Block unter *Settings → Secrets and variables → Actions* als
   `APT_GPG_PRIVATE_KEY` einfügen. Der Block darf das Terminal nicht verlassen und gehört
   in keine Datei im Repo.
3. **Nichts weiter zum Schlüssel** — den öffentlichen Teil exportiert der Workflow bei
   jedem Lauf aus dem privaten und legt ihn als `maildigest-archive-keyring.gpg` in die
   Paketquelle. So können Signatur und veröffentlichter Schlüssel nicht auseinanderlaufen.
4. **Zweig `gh-pages` anlegen und GitHub Pages einschalten**: Die Pages-Einstellung kann
   nur einen Zweig wählen, den es schon gibt — der Zweig muss deshalb vor der Einstellung
   existieren (ein leerer Wurzel-Commit genügt, der Workflow füllt ihn). Danach
   *Settings → Pages → Source: Deploy from a branch → `gh-pages` / `/ (root)`*.
5. **COPR-Projekt anlegen** auf copr.fedorainfracloud.org (Projekt `maildigest`, Chroots
   `fedora-rawhide-x86_64` und die zwei aktuellen stabilen), darin die Pakete
   zwei Pakete anlegen: `imap-tools` mit der Quellart **PyPI** (Abschnitt 3) und
   `maildigest` als **SCM**-Paket auf dieses Repo, Baumethode `make_srpm`.
   COPR zeigt unter *Settings → Integrations* eine **Custom**-Webhook-URL der Form
   `https://copr.fedorainfracloud.org/webhooks/custom/<id>/<token>/maildigest/` — genau
   die (mit `maildigest` am Ende) kommt als Secret `COPR_WEBHOOK_URL` dazu. Die
   GitHub-Variante derselben Seite ist hier falsch: Sie erwartet eine GitHub-Nutzlast und
   würde bei jedem Push bauen, statt erst nach bestandener Einbauprobe.

Schritt 5 ist entbehrlich, wenn dnf vorerst warten soll — der Workflow überspringt den
COPR-Aufruf, solange das Secret fehlt, und das Apt-Repo funktioniert unabhängig davon.

## 9. Ablauf eines Releases danach

```bash
# 1. Version in pyproject.toml anheben, CHANGELOG.md-Abschnitt schreiben
# 2. Manpage neu erzeugen, falls sich die CLI geändert hat
# 3.
git commit -am "Release 0.3.0" && git tag v0.3.0 && git push --follow-tags
```

Alles Weitere geschieht ohne Zutun: Wheel und sdist am GitHub-Release, `.deb` gebaut,
eingebaut geprüft, signiert, ins Pages-Repo geschoben, COPR angestoßen. Ein
fehlgeschlagener Schritt bricht den Lauf ab, bevor etwas veröffentlicht wird — ein
kaputtes Paket in einer Paketquelle ist teurer als ein ausgefallenes Release.

## 10. Verworfene Alternativen

- **Aufnahme in Debian/Fedora selbst.** Braucht einen ITP-Bug, einen Sponsor und
  Policy-Konformität; danach hängt jede Version am Release-Zyklus der Distribution und
  friert in Stable jahrelang ein. Für ein Projekt dieser Größe ein Missverhältnis.
- **`reprepro`/`aptly` statt `apt-ftparchive`.** Siehe Abschnitt 5: Zustandsdatenbank ohne
  Zustand.
- **Alle Abhängigkeiten ins Paket bündeln** (venv unter `/opt`). Löst das
  Versionsproblem, verrät aber den Sinn eines Distributionspakets: Sicherheitsupdates von
  `lxml` oder `httpx` würden an MailDigest vorbeigehen.
- **`apt-key` beziehungsweise Schlüssel im globalen Vertrauensvorrat.** Abgekündigt, und
  aus gutem Grund: siehe Abschnitt 6.

## 11. README erst danach umstellen

Der Installationsabschnitt im README behält bis zum ersten **grünen** Release-Lauf den
`pipx`-Weg als einzigen. Eine Anleitung, die auf eine noch leere Paketquelle zeigt,
produziert beim Nutzer ein `404` und bei uns den Verdacht, hier werde etwas versprochen,
was es nicht gibt. Sobald `https://kpafi.github.io/EmailSummary/apt/dists/stable/InRelease`
abrufbar ist, wandern die Blöcke aus Abschnitt 1 dieses Plans an den Anfang des
Installationsabschnitts, `pipx` rückt auf den zweiten Platz („für Systeme ohne
Paketquelle und zum Entwickeln"), und die getragenen Distributionen aus Abschnitt 4
werden dort genannt.
