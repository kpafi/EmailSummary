#!/usr/bin/env python3
"""Cold test, selfhost-mail: the mailbox-password prompt of `--check` on a real terminal.

Runs the command under a pty and drives the prompt four ways:

    python3 50-password-prompt.py ok wrong eof sigint

  ok     — type the right password        -> expect "All checks passed.", exit 0
  wrong  — type a wrong password          -> expect IMAPS login FAIL, exit 1
  eof    — Ctrl-D at the prompt           -> expect "Error: ..." on stderr, exit 2 (SPEC-CLI §2)
  sigint — Ctrl-C at the prompt           -> expect "Error: Aborted.", exit 1 (SPEC-CLI §2)

It also reports whether the typed password was echoed to the screen.
"""
import contextlib
import os
import pty
import select
import signal
import sys
import time

PW = os.environ.get("MAILBOX_PW", "CorrectHorseBatteryStapleXQ7")
BIN = os.environ.get("MAILDIGEST_BIN", "maildigest")
WORK = os.environ.get("WORK", os.path.expanduser("~/work"))


def run(mode):
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(WORK)
        os.environ.pop("MAILDIGEST_IMAP_PASSWORD", None)
        os.execvp(BIN, [BIN, "selfhost-mail", "--check"])

    buf, acted, t0 = b"", False, time.time()
    while time.time() - t0 < 60:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            buf += data
        if not acted and b"Mailbox password" in buf:
            time.sleep(0.4)
            acted = True
            if mode == "ok":
                os.write(fd, PW.encode() + b"\n")
            elif mode == "wrong":
                os.write(fd, b"nope-wrong\n")
            elif mode == "eof":
                os.write(fd, bytes([4]))
            elif mode == "sigint":
                os.kill(pid, signal.SIGINT)
        if b"All checks passed" in buf or b"Error:" in buf or b"Traceback" in buf:
            time.sleep(0.6)
            while True:
                ready, _, _ = select.select([fd], [], [], 0.3)
                if not ready:
                    break
                try:
                    more = os.read(fd, 4096)
                except OSError:
                    break
                if not more:
                    break
                buf += more
            break
    with contextlib.suppress(OSError):
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    out = buf.decode(errors="replace")
    after = out.split("Mailbox password")[-1][:80] if "Mailbox password" in out else ""
    print(f"########## mode: {mode}")
    print(out)
    print("PASSWORD ECHOED:", PW[:12] in after)
    print("TRACEBACK      :", "Traceback" in out)
    print("EXIT           :", status >> 8, "SIGNAL:", status & 0x7F)
    print()


for m in sys.argv[1:] or ["ok", "wrong", "eof", "sigint"]:
    run(m)
