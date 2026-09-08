#!/bin/bash
# Speist eine Liste von .eml ueber `maildigest test --eml` ein.
# Aufruf: run_mails.sh <config> <mail...>
export SSL_CERT_FILE=/home/kpafi/maildigest-coldtest/work/tls/ca.crt
MD=/home/kpafi/maildigest-coldtest/venv/bin/maildigest
CFG="$1"; shift
for m in "$@"; do
  echo "########## $(basename "$m")"
  timeout 120 "$MD" test --config "$CFG" --eml "$m" 2>&1
  echo "----- exit=$?"
done
