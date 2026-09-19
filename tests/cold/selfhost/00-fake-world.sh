#!/bin/sh
# Cold test, selfhost-mail: build the fake world on a fresh Debian 13 box.
#
# CLI and public interfaces only — this script never looks into the repository.
# It fakes what `maildigest selfhost-mail` needs from the internet:
#   * the domain mail.mirror.test resolving to 127.0.0.1 (/etc/hosts)
#   * a throw-away CA in the system trust store (OpenSSL 3.6 wants keyUsage=keyCertSign)
#   * a server certificate for mail.mirror.test in /etc/mirror-cert, which is what
#     --cert-dir is pointed at
#
# Run as the ordinary user; it uses sudo for the privileged parts.
# Afterwards: 10-generate-apply-check.sh
set -eu

DOMAIN=${DOMAIN:-mail.mirror.test}
CERT_DIR=${CERT_DIR:-/etc/mirror-cert}
CERT_DAYS=${CERT_DAYS:-20}

grep -q "$DOMAIN" /etc/hosts || \
  echo "127.0.0.1 $DOMAIN mirror.test" | sudo tee -a /etc/hosts >/dev/null

export DEBIAN_FRONTEND=noninteractive
echo "postfix postfix/main_mailer_type select Internet Site" | sudo debconf-set-selections
echo "postfix postfix/mailname string $DOMAIN" | sudo debconf-set-selections
sudo apt-get update -qq
sudo apt-get install -y -qq postfix dovecot-imapd dovecot-lmtpd openssl ca-certificates

work=$(mktemp -d)
cd "$work"

cat > ca.cnf <<EOF
[req]
distinguished_name=dn
x509_extensions=v3
prompt=no
[dn]
CN=MailDigest ColdTest CA
[v3]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
EOF
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 30 -config ca.cnf 2>/dev/null

cat > srv.cnf <<EOF
[req]
distinguished_name=dn
prompt=no
[dn]
CN=$DOMAIN
[v3]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$DOMAIN,DNS:mirror.test
EOF
openssl req -new -newkey rsa:2048 -nodes -keyout privkey.pem -out srv.csr -config srv.cnf 2>/dev/null
openssl x509 -req -in srv.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out srv.crt \
  -days "$CERT_DAYS" -extfile srv.cnf -extensions v3 2>/dev/null
cat srv.crt ca.crt > fullchain.pem

sudo mkdir -p "$CERT_DIR"
sudo cp fullchain.pem privkey.pem "$CERT_DIR/"
sudo chmod 644 "$CERT_DIR/fullchain.pem"
sudo chmod 640 "$CERT_DIR/privkey.pem"
sudo cp ca.crt /usr/local/share/ca-certificates/maildigest-coldtest-ca.crt
sudo update-ca-certificates >/dev/null

echo "fake world ready: $DOMAIN -> 127.0.0.1, certificate in $CERT_DIR (CA trusted)"
echo "CA material kept in $work (throw-away)"
