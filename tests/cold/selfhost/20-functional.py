#!/usr/bin/env python3
"""Cold test, selfhost-mail: the functional promises of OPERATIONS.md §6.6.

Public interfaces only: SMTP with smtplib against 127.0.0.1:25, IMAPS with imaplib
and ssl.create_default_context() — the same thing an ordinary mail client does.

    MAILBOX_PW=... python3 20-functional.py [address]

The address defaults to the one in ~/work/selfhost-mail/state.json.
"""
import contextlib
import imaplib
import json
import os
import pathlib
import smtplib
import socket
import ssl
import sys

PW = os.environ.get("MAILBOX_PW", "CorrectHorseBatteryStapleXQ7")
WORK = pathlib.Path(os.environ.get("WORK", os.path.expanduser("~/work")))
STATE = WORK / "selfhost-mail" / "state.json"

if len(sys.argv) > 1:
    address = sys.argv[1]
    domain = address.split("@", 1)[1]
else:
    state = json.loads(STATE.read_text())
    address, domain = state["address"], state["domain"]


def send(to, subject, body="hello\n", extra=0, frm="sender@example.com"):
    s = smtplib.SMTP("127.0.0.1", 25, timeout=60)
    s.ehlo("probe.example.com")
    msg = f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\n\r\n{body}"
    if extra:
        msg += "X" * extra
    try:
        s.sendmail(frm, [to], msg.encode())
        return "ACCEPTED"
    except Exception as exc:  # we want the refusal text
        return f"REFUSED {type(exc).__name__}: {str(exc)[:120]}"
    finally:
        with contextlib.suppress(Exception):
            s.quit()


print("mirror address     :", send(address, "Cold test one"))
print("mirror address     :", send(address, "Cold test two"))
print("mirror address     :", send(address, "Cold test three"))
print("other local user   :", send(f"someoneelse@{domain}", "nope"))
print("postmaster@        :", send(f"postmaster@{domain}", "nope"))
print("root@              :", send(f"root@{domain}", "nope"))
print("relay outside      :", send("relay-test@example.com", "nope"))
print("26 MiB (> 25 MiB)  :", send(address, "oversize", extra=26 * 1024 * 1024))

ctx = ssl.create_default_context()
m = imaplib.IMAP4_SSL(domain, 993, ssl_context=ctx)
m.login(address, PW)
m.select("INBOX")
ids = m.search(None, "ALL")[1][0].split()
print("IMAPS login        : ok,", len(ids), "message(s) in INBOX")
for i in ids:
    hdr = m.fetch(i, "(BODY[HEADER.FIELDS (SUBJECT)])")[1][0][1]
    print("   ", hdr.decode(errors="replace").strip())
m.logout()

try:
    sock = socket.create_connection((domain, 143), timeout=5)
    sock.settimeout(5)
    print("port 143           : OPEN, banner =", sock.recv(100))
except OSError as exc:
    print("port 143           : closed —", type(exc).__name__)

try:
    imaplib.IMAP4(domain, 143)
    print("cleartext login    : REACHABLE")
except OSError as exc:
    print("cleartext login    : impossible —", type(exc).__name__)
