#!/bin/sh
# The probe of docs/PLAN-SELFHOST-MAIL.md §7: it applies the files that
# `maildigest selfhost-mail` generates to a real Postfix and a real Dovecot, delivers one
# mail and lets `--check` prove the chain. Templates that were never applied are guesses;
# this script is what turns them into a fact (F-ING-4, ADR-089).
#
#   sh tests/integration/test_selfhost_probe.sh [--mode container|systemd] [--keep]
#
# Two environments, one script:
#   * container — debian:trixie without an init: Postfix and Dovecot are started in the
#     foreground and stopped again at the end. This is what CI runs
#     (.github/workflows/selfhost-probe.yml).
#   * systemd   — a throw-away VM (Debian 13): the services are managed with systemctl.
# Without --mode the environment is detected: systemd when /run/systemd/system exists
# (the canonical test; `is-system-running` fails on a merely degraded host), container otherwise.
#
# It needs root, it changes /etc/postfix, /etc/dovecot, /etc/hosts and the CA store, and it
# creates a certificate authority under its work directory. Run it in a container or in a
# machine you are willing to throw away — never on a host that does anything else.
#
# `maildigest` has to be on PATH (the .deb, pipx, or a venv whose bin directory is in PATH).
#
# Exit code 0 means every one of the seven steps passed, the negative case included.

set -eu

DOMAIN=mirror.test
PASSWORD='probe-pw-4c1f9a'          # throw-away, lives only inside this machine
WORK=${WORK_DIR:-/tmp/maildigest-selfhost-probe}
# NOT under $WORK: Debian's dovecot.service runs with systemd's PrivateTmp, so a
# certificate below /tmp simply does not exist for Dovecot — it keeps its old
# configuration and every reload still reports success. Real setups use
# /etc/letsencrypt/live/<domain>, which has no such problem.
CERT_DIR=${CERT_DIR:-/etc/maildigest-probe/certs}
OUT_DIR="$WORK/generated"
MODE=
KEEP=0
STEP=0

while [ $# -gt 0 ]; do
  case $1 in
    --mode) MODE=${2:-}; shift 2 ;;
    --mode=*) MODE=${1#--mode=}; shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { STEP=$((STEP + 1)); printf '\n=== %s/7  %s\n' "$STEP" "$1"; }
note() { printf '     %s\n' "$1"; }
die() { printf '\nFAILED: %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "this probe has to run as root"
command -v maildigest >/dev/null 2>&1 || die "maildigest is not on PATH"

if [ -z "$MODE" ]; then
  if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    MODE=systemd
  else
    MODE=container
  fi
fi
case $MODE in
  container|systemd) ;;
  *) die "--mode has to be container or systemd (got $MODE)" ;;
esac
printf 'MailDigest selfhost probe — mode %s, work directory %s\n' "$MODE" "$WORK"
PATH="$PATH:/usr/sbin:/sbin"
export PATH

# --- service control, the one thing the two modes disagree about -------------------------

start_services() {
  if [ "$MODE" = systemd ]; then
    systemctl start postfix dovecot
  else
    # No init in a container: both in the foreground, their logs into the work directory.
    # Postfix first, and only then Dovecot: the LMTP socket from the drop-in lives below
    # /var/spool/postfix/private, and that directory does not exist until Postfix has
    # started once (its first start runs post-install). Started side by side, Dovecot
    # loses the race and dies with "bind(/var/spool/postfix/private/dovecot-lmtp) failed:
    # No such file or directory / Fatal: Failed to start listeners" — seen on the CI
    # runner with debian:trixie. Under systemd this never shows, because apply.sh has
    # restarted Postfix before anything reloads Dovecot.
    postfix start-fg >"$WORK/postfix.log" 2>&1 &
    echo $! > "$WORK/postfix.pid"
    wait_for_port 127.0.0.1 25 30 || die "nothing listens on 25"
    dovecot -F >"$WORK/dovecot.log" 2>&1 &
    echo $! > "$WORK/dovecot.pid"
  fi
  wait_for_port 127.0.0.1 25 30 || die "nothing listens on 25"
  wait_for_port 127.0.0.1 993 30 || die "nothing listens on 993"
}

reload_services() {
  if [ "$MODE" = systemd ]; then
    systemctl reload postfix dovecot
  else
    postfix reload >>"$WORK/postfix.log" 2>&1 || true
    doveadm reload >>"$WORK/dovecot.log" 2>&1 || true
  fi
  sleep 2
}

stop_services() {
  if [ "$MODE" = systemd ]; then
    systemctl stop postfix dovecot 2>/dev/null || true
  else
    [ -f "$WORK/postfix.pid" ] && kill "$(cat "$WORK/postfix.pid")" 2>/dev/null || true
    [ -f "$WORK/dovecot.pid" ] && kill "$(cat "$WORK/dovecot.pid")" 2>/dev/null || true
  fi
}

cleanup() {
  status=$?
  [ "$status" -eq 0 ] || {
    printf '\n--- last log lines -------------------------------------------------\n' >&2
    tail -n 30 "$WORK/postfix.log" "$WORK/dovecot.log" >&2 2>/dev/null || true
    if [ "$MODE" = systemd ]; then
      journalctl --no-pager -n 40 -t postfix/lmtp -t dovecot >&2 2>/dev/null || true
    fi
  }
  stop_services
  [ "$KEEP" -eq 1 ] || rm -rf "$OUT_DIR"
  exit $status
}
trap cleanup EXIT INT TERM

wait_for_port() {
  _host=$1; _port=$2; _tries=$3
  while [ "$_tries" -gt 0 ]; do
    if python3 - "$_host" "$_port" <<'PY'
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2).close()
except OSError:
    sys.exit(1)
PY
    then return 0; fi
    _tries=$((_tries - 1))
    sleep 1
  done
  return 1
}

rm -rf "$WORK" "$CERT_DIR"
mkdir -p "$WORK" "$CERT_DIR"
chmod 0755 "$CERT_DIR"

# --- 1. packages -------------------------------------------------------------------------

say "packages: postfix, dovecot-imapd, dovecot-lmtpd"
if command -v postconf >/dev/null 2>&1 && command -v doveadm >/dev/null 2>&1; then
  note "already installed"
else
  export DEBIAN_FRONTEND=noninteractive
  # A plain `apt-get install postfix` would open a debconf dialogue; the preseed picks the
  # answer the probe wants and the generated postfix.sh overwrites the rest anyway.
  debconf-set-selections <<PRESEED
postfix postfix/main_mailer_type select Internet Site
postfix postfix/mailname string $DOMAIN
PRESEED
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    postfix dovecot-imapd dovecot-lmtpd openssl ca-certificates python3 >/dev/null
fi
postconf mail_version
doveconf dovecot_config_version 2>/dev/null || doveadm --version

# --- 2. generate ---------------------------------------------------------------------------

say "maildigest selfhost-mail --domain $DOMAIN --cert-dir $CERT_DIR"
# `mirror.test` has two labels, so the apex rule of SPEC-CLI.md §4 applies and the probe
# says so explicitly — a real deployment uses a subdomain and needs no such flag.
maildigest selfhost-mail --domain "$DOMAIN" --allow-apex --cert-dir "$CERT_DIR" \
  --out "$OUT_DIR" --non-interactive >"$WORK/generate.out" 2>"$WORK/generate.err" ||
  { cat "$WORK/generate.err" >&2; die "generation failed"; }
ADDRESS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["address"])' \
  "$OUT_DIR/state.json")
note "address: $ADDRESS"
for f in dns.txt postfix.sh dovecot.conf apply.sh checklist.txt state.json; do
  [ -f "$OUT_DIR/$f" ] || die "$f was not generated"
done
grep -rq "$PASSWORD" "$OUT_DIR" && die "the generated directory holds the password (I5)"

# --- 3. fake the world: DNS and a throw-away CA ---------------------------------------------

say "fake DNS ($DOMAIN -> 127.0.0.1) and a throw-away CA"
grep -q "[[:space:]]$DOMAIN\$" /etc/hosts || printf '127.0.0.1 %s\n' "$DOMAIN" >> /etc/hosts
# OpenSSL 3.6 refuses to verify a chain whose CA certificate has no keyUsage=keyCertSign.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$WORK/ca.key" -out "$WORK/ca.crt" -subj "/CN=MailDigest Probe CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$CERT_DIR/privkey.pem" \
  -out "$WORK/server.csr" -subj "/CN=$DOMAIN" 2>/dev/null
cat > "$WORK/server.ext" <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$DOMAIN
EXT
openssl x509 -req -in "$WORK/server.csr" -CA "$WORK/ca.crt" -CAkey "$WORK/ca.key" \
  -CAcreateserial -out "$WORK/server.crt" -days 825 -extfile "$WORK/server.ext" 2>/dev/null
cat "$WORK/server.crt" "$WORK/ca.crt" > "$CERT_DIR/fullchain.pem"
chmod 0644 "$CERT_DIR/fullchain.pem"
chmod 0600 "$CERT_DIR/privkey.pem"
mkdir -p /usr/local/share/ca-certificates
cp "$WORK/ca.crt" /usr/local/share/ca-certificates/maildigest-probe.crt
update-ca-certificates >/dev/null
note "CA installed, certificate for $DOMAIN in $CERT_DIR"

# --- 4. apply -------------------------------------------------------------------------------

say "sh apply.sh (password on stdin)"
printf '%s\n' "$PASSWORD" | sh "$OUT_DIR/apply.sh" >"$WORK/apply.log" 2>&1 ||
  { cat "$WORK/apply.log" >&2; die "apply.sh failed"; }
grep -q '{BLF-CRYPT}' /etc/dovecot/users || die "/etc/dovecot/users holds no hash"
grep -q "$PASSWORD" /etc/dovecot/users && die "the cleartext password was written (I5)"
note "$(ls -l /etc/dovecot/users)"
start_services
reload_services

# --- 5. deliver one mail --------------------------------------------------------------------

cat > "$WORK/send.py" <<'PY'
import smtplib, sys
from email.message import EmailMessage

address, subject = sys.argv[1], sys.argv[2]
message = EmailMessage()
message["From"] = "forwarder@example.org"
message["To"] = address
message["Subject"] = subject
message.set_content("This mail proves Postfix -> LMTP -> Dovecot.")
with smtplib.SMTP("127.0.0.1", 25, timeout=15) as smtp:
    smtp.send_message(message)
PY

say "deliver one mail with smtplib to 127.0.0.1:25"
python3 "$WORK/send.py" "$ADDRESS" "MailDigest selfhost probe"
note "sent to $ADDRESS"

# --- 6. --check and --check --wait-for-mail -------------------------------------------------

say "maildigest selfhost-mail --check (and --wait-for-mail)"
MAILDIGEST_IMAP_PASSWORD="$PASSWORD"
export MAILDIGEST_IMAP_PASSWORD
maildigest selfhost-mail --check --out "$OUT_DIR" --non-interactive >"$WORK/check.out" 2>&1 ||
  { cat "$WORK/check.out"; die "--check did not exit 0"; }
cat "$WORK/check.out"
# The MX step has no resolver to ask in this fake world, and PLAN §7 asserts exactly that.
grep -Eq '^  DNS MX +(skipped|FAIL) ' "$WORK/check.out" ||
  die "the MX line is neither skipped nor FAIL — the fake world grew a resolver"
grep -q '^All checks passed\.$' "$WORK/check.out" || die "not every check passed"

# `--wait-for-mail` notes UIDNEXT before it starts waiting, so mail that is already in the
# INBOX deliberately does not count (SPEC-CLI.md §4) — the second mail therefore has to
# arrive *while* the command waits, exactly as a real forward would.
maildigest selfhost-mail --check --wait-for-mail --timeout 60 --out "$OUT_DIR" \
  --non-interactive >"$WORK/wait.out" 2>&1 &
wait_pid=$!
sleep 8
python3 "$WORK/send.py" "$ADDRESS" "MailDigest probe while waiting"
note "second mail sent while --wait-for-mail runs"
set +e
wait "$wait_pid"
wait_status=$?
set -e
cat "$WORK/wait.out"
[ "$wait_status" -eq 0 ] || die "--wait-for-mail exited $wait_status instead of 0"
grep -Eq '^  Mail +ok +from .*MailDigest probe while waiting' "$WORK/wait.out" ||
  die "the delivered mail did not show up in the Mail line"
grep -q "^Next: maildigest connect-mail --host $DOMAIN --username $ADDRESS\$" "$WORK/wait.out" ||
  die "the connect-mail line is missing"
grep -q "$PASSWORD" "$WORK/wait.out" && die "the check printed the password (I5)"

# The relay refusal is one of the eight lines above, but it is the assurance that matters
# most, so the probe asks Postfix itself once more and insists on a 5xx.
printf '\n---  an outside RCPT TO is refused (part of step 6)\n'
python3 - "$DOMAIN" <<'PY'
import smtplib, sys

domain = sys.argv[1]
smtp = smtplib.SMTP("127.0.0.1", 25, timeout=15)
smtp.ehlo(domain)
smtp.mail(f"probe@{domain}")
code, text = smtp.rcpt("relay-test@example.com")
smtp.quit()
if code < 500:
    raise SystemExit(f"open relay: RCPT TO an outside address answered {code} {text!r}")
print(f"     refused with {code}")
PY

# --- 7. the negative case: the check has to notice an open 143 --------------------------------

say "negative case: remove 'port = 0', reload, --check must FAIL on 143"
DROPIN=/etc/dovecot/conf.d/99-maildigest.conf
cp "$DROPIN" "$WORK/dovecot-dropin.bak"
sed -i '/^[[:space:]]*port = 0[[:space:]]*$/d' "$DROPIN"
grep -q 'port = 0' "$DROPIN" && die "the port = 0 line is still there"
reload_services
wait_for_port 127.0.0.1 143 20 || die "143 did not open — the negative case proves nothing"
set +e
maildigest selfhost-mail --check --out "$OUT_DIR" --non-interactive >"$WORK/negative.out" 2>&1
negative_status=$?
set -e
cat "$WORK/negative.out"
[ "$negative_status" -eq 1 ] || die "--check exited $negative_status instead of 1"
grep -Eq "^  IMAPS 143 +FAIL +cleartext IMAP on $DOMAIN:143 is open" "$WORK/negative.out" ||
  die "the 143 check did not fail — it tests nothing"
grep -q '^Mirror mailbox ready\.$' "$WORK/negative.out" &&
  die "the ready block was printed although a check failed"
cp "$WORK/dovecot-dropin.bak" "$DROPIN"
reload_services

printf '\nAll seven steps of PLAN-SELFHOST-MAIL §7 passed (mode %s).\n' "$MODE"
