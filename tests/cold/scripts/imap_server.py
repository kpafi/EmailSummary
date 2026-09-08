#!/usr/bin/env python3
"""Minimaler IMAP4rev1-Server ueber TLS fuer den Cold-Test.

Serviert alle .eml aus einem Verzeichnis als ungelesene Mails in INBOX.
Protokolliert jedes Kommando nach work/logs/imap.jsonl (um F-ING-1 zu pruefen:
kein DELETE/EXPUNGE, nur \\Seen und optional COPY).

Aufruf: python3 imap_server.py PORT MAILDIR
"""
import socket, ssl, sys, os, threading, json, datetime, glob

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9930
MAILDIR = sys.argv[2] if len(sys.argv) > 2 else "/home/kpafi/maildigest-coldtest/work/maildir"
BASE = "/home/kpafi/maildigest-coldtest/work"
LOG = os.environ.get("IMAP_LOG", BASE + "/logs/imap.jsonl")
CERT = BASE + "/tls/srv.pem"
FOLDERS = ["INBOX", "Processed", "Archiv/2026", "Spam"]


def logcmd(kind, data):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                            "kind": kind, "data": data}, ensure_ascii=False) + "\n")


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.seen = set()      # uids marked \Seen
        self.moved = set()     # uids copied away


ST = State()


def mails():
    out = []
    for i, p in enumerate(sorted(glob.glob(os.path.join(MAILDIR, "*.eml"))), start=1):
        out.append((i, p))
    return out


def handle(conn):
    f = conn.makefile("rwb")

    def send(s):
        f.write((s + "\r\n").encode())
        f.flush()

    send("* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] ColdTest IMAP ready")
    selected = None
    while True:
        line = f.readline()
        if not line:
            return
        try:
            raw = line.decode("utf-8", "replace").rstrip("\r\n")
        except Exception:
            return
        parts = raw.split(" ")
        tag = parts[0]
        cmd = parts[1].upper() if len(parts) > 1 else ""
        args = parts[2:]
        logcmd("cmd", raw if cmd not in ("LOGIN", "AUTHENTICATE") else f"{tag} {cmd} <redacted>")

        if cmd == "CAPABILITY":
            send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
            send(f"{tag} OK CAPABILITY done")
        elif cmd == "LOGIN":
            send(f"{tag} OK LOGIN completed")
        elif cmd in ("LIST", "LSUB"):
            for fol in FOLDERS:
                send(f'* {cmd} (\\HasNoChildren) "/" "{fol}"')
            send(f"{tag} OK {cmd} completed")
        elif cmd in ("SELECT", "EXAMINE"):
            selected = args[0].strip('"') if args else "INBOX"
            n = len(mails()) if selected.upper() == "INBOX" else 0
            send(f"* {n} EXISTS")
            send("* 0 RECENT")
            send("* OK [UIDVALIDITY 1] UIDs valid")
            send(f"* OK [UIDNEXT {n+1}] Predicted next UID")
            send(r"* FLAGS (\Seen \Answered \Flagged \Deleted \Draft)")
            send(rf"{tag} OK [READ-WRITE] {cmd} completed")
        elif cmd == "SEARCH" or (cmd == "UID" and args and args[0].upper() == "SEARCH"):
            uids = [str(u) for u, _ in mails() if u not in ST.seen and u not in ST.moved]
            send("* SEARCH " + " ".join(uids))
            send(f"{tag} OK SEARCH completed")
        elif cmd == "UID" and args and args[0].upper() == "FETCH":
            uid = int(args[1].split(":")[0])
            m = dict(mails()).get(uid)
            if m is None:
                send(f"{tag} OK FETCH completed")
                continue
            data = open(m, "rb").read()
            seq = uid
            send(f"* {seq} FETCH (UID {uid} RFC822 {{{len(data)}}}")
            f.write(data)
            f.write(b")\r\n")
            f.flush()
            send(f"{tag} OK FETCH completed")
        elif cmd == "FETCH":
            uid = int(args[0].split(":")[0])
            m = dict(mails()).get(uid)
            if m is None:
                send(f"{tag} OK FETCH completed")
                continue
            data = open(m, "rb").read()
            send(f"* {uid} FETCH (UID {uid} RFC822 {{{len(data)}}}")
            f.write(data)
            f.write(b")\r\n")
            f.flush()
            send(f"{tag} OK FETCH completed")
        elif cmd == "UID" and args and args[0].upper() == "STORE":
            uid = int(args[1].split(":")[0])
            if "\\Deleted" in raw:
                logcmd("DANGER_DELETE", raw)
            if "Seen" in raw:
                ST.seen.add(uid)
            send(f"{tag} OK STORE completed")
        elif cmd == "STORE":
            uid = int(args[0].split(":")[0])
            if "\\Deleted" in raw:
                logcmd("DANGER_DELETE", raw)
            if "Seen" in raw:
                ST.seen.add(uid)
            send(f"{tag} OK STORE completed")
        elif cmd == "UID" and args and args[0].upper() == "COPY":
            target = args[2].strip('"') if len(args) > 2 else ""
            if os.environ.get("IMAP_COPY_FAIL") or target not in FOLDERS:
                logcmd("copy_refused", raw)
                send(f"{tag} NO [TRYCREATE] Mailbox does not exist")
            else:
                ST.moved.add(int(args[1].split(":")[0]))
                send(f"{tag} OK COPY completed")
        elif cmd == "COPY":
            ST.moved.add(int(args[0].split(":")[0]))
            send(f"{tag} OK COPY completed")
        elif cmd == "EXPUNGE":
            logcmd("DANGER_EXPUNGE", raw)
            send(f"{tag} OK EXPUNGE completed")
        elif cmd == "CREATE":
            FOLDERS.append(args[0].strip('"'))
            send(f"{tag} OK CREATE completed")
        elif cmd == "NOOP":
            send(f"{tag} OK NOOP completed")
        elif cmd in ("CLOSE", "UNSELECT"):
            send(f"{tag} OK {cmd} completed")
        elif cmd == "LOGOUT":
            send("* BYE logging out")
            send(f"{tag} OK LOGOUT completed")
            return
        else:
            send(f"{tag} OK {cmd} completed")


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    os.makedirs(MAILDIR, exist_ok=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT)
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", PORT))
    s.listen(20)
    print(f"imap on {PORT} maildir={MAILDIR}", flush=True)
    while True:
        c, _ = s.accept()
        try:
            tc = ctx.wrap_socket(c, server_side=True)
        except Exception as e:
            logcmd("tls_error", str(type(e).__name__))
            continue
        threading.Thread(target=lambda: _safe(tc), daemon=True).start()


def _safe(c):
    try:
        handle(c)
    except Exception as e:
        logcmd("conn_error", f"{type(e).__name__}: {e}")
    finally:
        try:
            c.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
