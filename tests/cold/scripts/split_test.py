#!/usr/bin/env python3
"""Split-Test: platziert Domains/punkthaltige Tokens an variierenden Offsets und prueft,
ob beim Aufteilen der Nachricht (Discord 2000 Zeichen) eine lebende Domain entsteht.

Aufruf: split_test.py   (braucht laufende Mocks im Modus raw + laufenden Sink)
"""
import os, subprocess, json, sys, re

W = "/home/kpafi/maildigest-coldtest/work"
MD = "/home/kpafi/maildigest-coldtest/venv/bin/maildigest"
TEXT = W + "/mocktext.txt"
SINK = W + "/logs/sink.jsonl"
TOKENS = ["grenze.example", "www.grenze.example", "shop.grenze.example.org",
          "datei.pdf", "version 1.2.3", "boese[.]example", "a.b.c.d.example"]


def build(offset, token):
    filler = ("Dies ist ein Fuelltext fuer den Split-Test. " * 200)
    head = filler[:offset]
    return head + token + " " + filler[offset:offset + 2600]


def run_one(offset, token):
    with open(TEXT, "w", encoding="utf-8") as f:
        f.write(build(offset, token))
    before = sum(1 for _ in open(SINK)) if os.path.exists(SINK) else 0
    env = dict(os.environ)
    env["SSL_CERT_FILE"] = W + "/tls/ca.crt"
    subprocess.run([MD, "test", "--config", W + "/cfg/m2.toml", "--eml",
                    W + "/mails/01_normal.eml"], capture_output=True, env=env, timeout=120)
    parts = []
    for i, l in enumerate(open(SINK, encoding="utf-8")):
        if i >= before:
            r = json.loads(l)
            if r.get("json"):
                parts.append(r["json"].get("content", ""))
    return parts


RX_LIVE = re.compile(r"(?i)(?<![\[\w])[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?"
                     r"\.(?:example|org|com|net|de|pdf)\b")
RX_WWW = re.compile(r"(?i)(?<![\w.\[])www\.")


def main():
    bad = 0
    for token in TOKENS:
        for offset in list(range(1880, 2120, 17)) + list(range(3960, 4200, 17)):
            parts = run_one(offset, token)
            for pi, p in enumerate(parts):
                if len(p) > 2000:
                    print(f"!! TEIL ZU LANG: token={token} offset={offset} teil={pi} len={len(p)}")
                    bad += 1
                hits = set(RX_LIVE.findall(p)) | set(RX_WWW.findall(p))
                if hits:
                    print(f"!! LEBENDE DOMAIN: token={token!r} offset={offset} teil={pi}/{len(parts)} "
                          f"-> {sorted(hits)}")
                    ctx = p[max(0, p.find(sorted(hits)[0]) - 60):][:160]
                    print("   Kontext:", repr(ctx))
                    bad += 1
    print("Befunde:", bad)


if __name__ == "__main__":
    main()
