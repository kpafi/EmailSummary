#!/usr/bin/env python3
"""Cold test, selfhost-mail: what `--wait-for-mail` prints for a hostile subject.

Delivers one mail whose Subject carries ANSI colour, an OSC window-title sequence,
BEL, backspaces, a vertical tab, non-ASCII and 80 filler characters, then leaves it
to the caller to run

    MAILDIGEST_IMAP_PASSWORD=... maildigest selfhost-mail --check --wait-for-mail --timeout 60

in parallel and to look at the `Mail` line with `cat -v`. Expected per SPEC-CLI §2:
every control character replaced by `·`, the subject truncated to 60 characters.

    python3 40-hostile-subject.py <mirror address>
"""
import smtplib
import sys

address = sys.argv[1]
ESC, BEL, BS, VT = chr(27), chr(7), chr(8), chr(11)
subject = (
    f"{ESC}[31mRED{ESC}[0m {ESC}]0;PWNED{BEL} {BS}{BS} Error: fake  {VT} äöü 你好 " + "L" * 80
)
sender = 'Evil "Name" <ev@example.com>'

s = smtplib.SMTP("127.0.0.1", 25, timeout=30)
s.ehlo("probe.example.com")
msg = f"From: {sender}\r\nTo: {address}\r\nSubject: {subject}\r\n\r\nbody\r\n"
s.sendmail("bounce@example.com", [address], msg.encode("utf-8", "replace"))
s.quit()
print("sent; raw subject length:", len(subject))
