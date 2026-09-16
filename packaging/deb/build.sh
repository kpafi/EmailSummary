#!/usr/bin/env bash
# Builds the Debian package from the working tree.
#
#   packaging/deb/build.sh [version]
#
# Without an argument the version is read from pyproject.toml — the single
# source of truth. debian/changelog is generated rather than maintained: a
# second place holding a version number is a place that gets forgotten on
# release day.
#
# Result: dist/deb/maildigest_<version>_all.deb
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

version="${1:-$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)}"
[ -n "$version" ] || { echo "cannot determine the version" >&2; exit 1; }

maintainer="${DEBFULLNAME:-Samuel Krapf} <${DEBEMAIL:-samukrapf@gmail.com}>"

cat > debian/changelog <<CHANGELOG
maildigest ($version) stable; urgency=medium

  * Release $version. The changes are listed in CHANGELOG.md.

 -- $maintainer  $(date -R)
CHANGELOG

echo "==> building maildigest $version"
dpkg-buildpackage -us -uc -b

mkdir -p dist/deb
# dpkg-buildpackage drops its results next to the source directory.
mv -f ../maildigest_"$version"_all.deb dist/deb/
rm -f ../maildigest_"$version"_*.buildinfo ../maildigest_"$version"_*.changes

echo "==> done: dist/deb/maildigest_${version}_all.deb"
dpkg-deb -I "dist/deb/maildigest_${version}_all.deb" | sed -n '/Package:/,/Description:/p'
