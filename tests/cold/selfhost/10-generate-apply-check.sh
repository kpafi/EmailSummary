#!/bin/sh
# Cold test, selfhost-mail: the five documented steps of OPERATIONS.md §6.2,
# run against the fake world of 00-fake-world.sh. CLI only.
#
#   MAILBOX_PW=... sh 10-generate-apply-check.sh
set -eu

DOMAIN=${DOMAIN:-mail.mirror.test}
CERT_DIR=${CERT_DIR:-/etc/mirror-cert}
MAILBOX_PW=${MAILBOX_PW:-CorrectHorseBatteryStapleXQ7}
WORK=${WORK:-$HOME/work}

mkdir -p "$WORK"
cd "$WORK"

[ -f config.toml ] || maildigest init --non-interactive >/dev/null

echo "== step 0: generate =="
rm -rf selfhost-mail
maildigest selfhost-mail --domain "$DOMAIN" --cert-dir "$CERT_DIR"

echo "== generated directory =="
ls -la selfhost-mail
cat selfhost-mail/state.json

echo "== the directory holds no secret (expect: no match) =="
grep -rl "$MAILBOX_PW" selfhost-mail && echo "SECRET LEAKED" || echo "clean"

echo "== step 3: apply (asks for the mailbox password once) =="
printf '%s\n' "$MAILBOX_PW" | sudo sh "$WORK/selfhost-mail/apply.sh"
sudo ls -la /etc/dovecot/users

echo "== step 3 again: apply.sh is idempotent =="
printf '%s\n' "$MAILBOX_PW" | sudo sh "$WORK/selfhost-mail/apply.sh" >/dev/null
sudo ls -la /etc/dovecot/users

echo "== step 4: check =="
MAILDIGEST_IMAP_PASSWORD="$MAILBOX_PW" maildigest selfhost-mail --check
echo "check exit=$?"

echo "== listening ports (expect 25 and 993, never 143) =="
sudo ss -ltn | grep -E ':(25|143|993)\b' || true
