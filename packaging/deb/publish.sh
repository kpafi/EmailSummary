#!/usr/bin/env bash
# Files a built .deb into the apt repository and regenerates the indices.
#
#   packaging/deb/publish.sh <repo-directory> <deb-file> [gpg-key-id]
#
# <repo-directory> is the `apt/` folder on the gh-pages branch. The indices are
# recomputed from the pool in full on every run — there is no state database
# that would have to survive between two CI runs (ADR-088).
#
# Without a key id nothing is signed; that is meant for a dry run only. Apt
# rightly refuses an unsigned repository.
set -euo pipefail

repo="${1:?repo directory missing}"
deb="${2:?.deb file missing}"
key="${3:-}"

pool="$repo/pool/main/m/maildigest"
dist="$repo/dists/stable"
binary="$dist/main/binary-all"
mkdir -p "$pool" "$binary"

cp -f "$deb" "$pool/"
echo "==> pool holds:"
ls -1 "$pool"

cd "$repo"
# Paths in Packages have to be relative to the root of the repository.
apt-ftparchive --arch all packages pool > "dists/stable/main/binary-all/Packages"
gzip -9cn "dists/stable/main/binary-all/Packages" > "dists/stable/main/binary-all/Packages.gz"

# The old signatures and the old index have to go BEFORE apt-ftparchive scans
# the directory — otherwise it takes them into the checksum list of the new
# Release file and thereby describes files that no longer exist.
rm -f dists/stable/InRelease dists/stable/Release.gpg dists/stable/Release

apt-ftparchive \
  -o APT::FTPArchive::Release::Origin="MailDigest" \
  -o APT::FTPArchive::Release::Label="MailDigest" \
  -o APT::FTPArchive::Release::Suite="stable" \
  -o APT::FTPArchive::Release::Codename="stable" \
  -o APT::FTPArchive::Release::Architectures="all" \
  -o APT::FTPArchive::Release::Components="main" \
  -o APT::FTPArchive::Release::Description="MailDigest — own package repository" \
  release dists/stable > dists/stable/Release.tmp
# Only now to its final place: were the file already there during the scan, it
# would list itself.
mv dists/stable/Release.tmp dists/stable/Release

if [ -n "$key" ]; then
  # --pinentry-mode loopback because a CI runner has no pinentry: without it
  # gpg tries to open a prompt on a terminal that is not there and fails with
  # "Inappropriate ioctl for device", even though the key has no passphrase.
  sign="gpg --batch --yes --pinentry-mode loopback --default-key $key"
  # InRelease (embedded signature) is what current apt versions fetch;
  # Release.gpg stays next to it for older clients.
  $sign --clearsign -o dists/stable/InRelease dists/stable/Release
  $sign -abs -o dists/stable/Release.gpg dists/stable/Release
  echo "==> signed with $key"
else
  echo "==> WARNING: not signed (no key id given)" >&2
fi

echo "==> repository updated at $repo"
