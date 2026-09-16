#!/usr/bin/env bash
# Sortiert ein gebautes .deb in die Apt-Paketquelle ein und erzeugt die Indizes neu.
#
#   packaging/deb/publish.sh <repo-verzeichnis> <deb-datei> [gpg-key-id]
#
# <repo-verzeichnis> ist der Ordner `apt/` aus dem Zweig gh-pages. Die Indizes
# werden bei jedem Lauf vollständig aus dem Pool neu berechnet — es gibt keine
# Zustandsdatenbank, die zwischen zwei CI-Läufen überleben müsste (ADR-088).
#
# Ohne Key-ID wird nicht signiert; das ist nur für einen Probelauf gedacht. Apt
# lehnt eine unsignierte Quelle zu Recht ab.
set -euo pipefail

repo="${1:?Repo-Verzeichnis fehlt}"
deb="${2:?.deb-Datei fehlt}"
key="${3:-}"

pool="$repo/pool/main/m/maildigest"
dist="$repo/dists/stable"
binary="$dist/main/binary-all"
mkdir -p "$pool" "$binary"

cp -f "$deb" "$pool/"
echo "==> Pool enthält:"
ls -1 "$pool"

cd "$repo"
# Pfade in Packages müssen relativ zur Wurzel der Paketquelle stehen.
apt-ftparchive --arch all packages pool > "dists/stable/main/binary-all/Packages"
gzip -9cn "dists/stable/main/binary-all/Packages" > "dists/stable/main/binary-all/Packages.gz"

# Alte Signaturen und der alte Index müssen weg, BEVOR apt-ftparchive das
# Verzeichnis abtastet — sonst nimmt es sie in die Prüfsummenliste der neuen
# Release-Datei auf und beschreibt damit Dateien, die es gar nicht mehr gibt.
rm -f dists/stable/InRelease dists/stable/Release.gpg dists/stable/Release

apt-ftparchive \
  -o APT::FTPArchive::Release::Origin="MailDigest" \
  -o APT::FTPArchive::Release::Label="MailDigest" \
  -o APT::FTPArchive::Release::Suite="stable" \
  -o APT::FTPArchive::Release::Codename="stable" \
  -o APT::FTPArchive::Release::Architectures="all" \
  -o APT::FTPArchive::Release::Components="main" \
  -o APT::FTPArchive::Release::Description="MailDigest — eigene Paketquelle" \
  release dists/stable > dists/stable/Release.tmp
# Erst jetzt an den endgültigen Platz: Läge die Datei schon während des
# Abtastens dort, listete sie sich selbst auf.
mv dists/stable/Release.tmp dists/stable/Release

if [ -n "$key" ]; then
  # InRelease (eingebettete Signatur) ist das, was heutige apt-Versionen holen;
  # Release.gpg bleibt für ältere Clients daneben liegen.
  gpg --batch --yes --default-key "$key" --clearsign -o dists/stable/InRelease dists/stable/Release
  gpg --batch --yes --default-key "$key" -abs -o dists/stable/Release.gpg dists/stable/Release
  echo "==> signiert mit $key"
else
  echo "==> WARNUNG: nicht signiert (keine Key-ID übergeben)" >&2
fi

echo "==> Paketquelle aktualisiert unter $repo"
