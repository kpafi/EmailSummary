#!/bin/sh
# Cold test, selfhost-mail: the adversarial pass. CLI only, black box.
# Prints one line per case; compare against SPEC-CLI.md §4 and §2.
set -u

O=/tmp/coldtest-out
PW=${MAILBOX_PW:-CorrectHorseBatteryStapleXQ7}
WORK=${WORK:-$HOME/work}

dom() {
  d="$1"; shift
  rm -rf "$O"
  out=$(maildigest selfhost-mail --out "$O" --domain "$d" "$@" 2>&1); rc=$?
  printf '%-40s rc=%s | %s\n' "[$d $*]" "$rc" "$(echo "$out" | grep -iE '^Error' | head -1)"
}

echo "===== domain rules (SPEC-CLI §4 'Domain rules') ====="
dom "MX.MIRROR.TEST"                      # normalised to lowercase
dom "mx.mirror.test."                     # trailing dot removed
dom "mx.mirror.tëst"                      # non-ASCII
dom "xn--mx-hia.mirror.test"              # punycode
dom "mirror.test"                         # apex without --allow-apex
dom "mirror.test" --allow-apex
dom "test"                                # one label
dom "bad-.mirror.test"
dom "mx..mirror.test"
dom "$(python3 -c 'print("a"*64+".mirror.test")')"          # label > 63
dom "$(python3 -c 'print(("a"*63+".")*4+"test")')"          # name > 253
dom 'mx.mirror.test;id'
dom 'mx.mirror.test$(id)'
dom 'a b.mirror.test'
dom '"mx.mirror.test"'

echo
echo "===== --cert-dir values ====="
for c in "relative/certs" "/etc/x;id" '/etc/x$(id)' "/etc/mirror-cert/" "/etc/nonexistent"; do
  rm -rf "$O"
  out=$(maildigest selfhost-mail --out "$O" --domain c.mirror.test --cert-dir "$c" 2>&1); rc=$?
  printf '%-24s rc=%s | %s\n' "[$c]" "$rc" \
    "$(echo "$out" | grep -iE '^Error' | head -1)$(grep -h smtpd_tls_cert_file "$O/postfix.sh" 2>/dev/null)"
done

echo
echo "===== --out oddities ====="
for o in /tmp/ct/deep/nested /etc/maildigest-should-fail /dev/null /proc/self/out "/tmp/ct space/x" '/tmp/ct;id'; do
  rm -rf "$o" 2>/dev/null
  out=$(maildigest selfhost-mail --domain t.mirror.test --out "$o" 2>&1); rc=$?
  printf '%-32s rc=%s | %s\n' "[$o]" "$rc" "$(echo "$out" | grep -iE '^Error|^Written to' | head -1)"
done
echo "--- --out at an EXISTING directory: watch its mode"
rm -rf /tmp/ct-exdir; mkdir -p /tmp/ct-exdir; chmod 755 /tmp/ct-exdir; touch /tmp/ct-exdir/important.txt
stat -c 'before: %a %n' /tmp/ct-exdir
maildigest selfhost-mail --domain t.mirror.test --out /tmp/ct-exdir >/dev/null 2>&1
stat -c 'after : %a %n' /tmp/ct-exdir
ls /tmp/ct-exdir

echo
echo "===== mode mix-ups and ranges ====="
maildigest selfhost-mail --check --out "$WORK/selfhost-mail" --domain x.mirror.test 2>&1 | head -1
maildigest selfhost-mail --check --out "$WORK/selfhost-mail" --allow-apex 2>&1 | head -1
maildigest selfhost-mail --check --out "$WORK/selfhost-mail" --cert-dir /tmp 2>&1 | head -1
maildigest selfhost-mail --domain x.mirror.test --out /tmp/ct2 --wait-for-mail 2>&1 | head -1
maildigest selfhost-mail --check --out "$WORK/selfhost-mail" --timeout 30 2>&1 | head -1
maildigest selfhost-mail --check --out "$WORK/selfhost-mail" --wait-for-mail --timeout 9 2>&1 | head -1
maildigest selfhost-mail --check --out "$WORK/selfhost-mail" --wait-for-mail --timeout 3601 2>&1 | head -1
maildigest selfhost-mail --check --out /tmp/definitely-empty --non-interactive 2>&1 | head -1

echo
echo "===== tampered state.json ====="
S="$WORK/selfhost-mail/state.json"
cp "$S" /tmp/ct-state.bak
tamper() {
  printf '%s' "$2" > "$S"
  out=$(MAILDIGEST_IMAP_PASSWORD=x maildigest selfhost-mail --check --out "$WORK/selfhost-mail" 2>&1); rc=$?
  printf '%-16s rc=%s | %s\n' "$1" "$rc" "$(echo "$out" | grep -iE '^Error|Traceback' | head -1)"
}
tamper "not json"    '{nope'
tamper "empty"       ''
tamper "array"       '[1,2,3]'
tamper "version 2"   '{"address":"mirror-aaaaaaaa@mail.mirror.test","cert_dir":"/etc/mirror-cert","domain":"mail.mirror.test","generated_at":"2026-09-19T00:00:00Z","version":2}'
tamper "extra key"   '{"address":"mirror-aaaaaaaa@mail.mirror.test","cert_dir":"/etc/mirror-cert","domain":"mail.mirror.test","generated_at":"2026-09-19T00:00:00Z","version":1,"password":"x"}'
tamper "bad domain"  '{"address":"a@BAD..dom","cert_dir":"/etc/mirror-cert","domain":"BAD..dom","generated_at":"2026-09-19T00:00:00Z","version":1}'
tamper "missing keys" '{"domain":"mail.mirror.test","version":1}'
cp /tmp/ct-state.bak "$S"

echo
echo "===== --check while nothing listens ====="
sudo systemctl stop postfix
MAILDIGEST_IMAP_PASSWORD="$PW" maildigest selfhost-mail --check --out "$WORK/selfhost-mail"
echo "exit=$?"
sudo systemctl start postfix
