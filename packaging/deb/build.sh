#!/usr/bin/env bash
# Baut das Debian-Paket aus dem Arbeitsstand.
#
#   packaging/deb/build.sh [version]
#
# Ohne Argument wird die Version aus pyproject.toml gelesen — die einzige
# Wahrheitsquelle. debian/changelog wird dabei erzeugt und nicht gepflegt: Eine
# zweite Stelle mit einer Versionsnummer ist eine Stelle, die beim Release
# vergessen werden kann.
#
# Ergebnis: dist/deb/maildigest_<version>_all.deb
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

version="${1:-$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)}"
[ -n "$version" ] || { echo "Version nicht ermittelbar" >&2; exit 1; }

maintainer="${DEBFULLNAME:-Samuel Krapf} <${DEBEMAIL:-samukrapf@gmail.com}>"

cat > debian/changelog <<CHANGELOG
maildigest ($version) stable; urgency=medium

  * Release $version. Die Änderungen stehen in CHANGELOG.md.

 -- $maintainer  $(date -R)
CHANGELOG

echo "==> baue maildigest $version"
dpkg-buildpackage -us -uc -b

mkdir -p dist/deb
# dpkg-buildpackage legt die Ergebnisse neben dem Quellverzeichnis ab.
mv -f ../maildigest_"$version"_all.deb dist/deb/
rm -f ../maildigest_"$version"_*.buildinfo ../maildigest_"$version"_*.changes

echo "==> fertig: dist/deb/maildigest_${version}_all.deb"
dpkg-deb -I "dist/deb/maildigest_${version}_all.deb" | sed -n '/Package:/,/Description:/p'
