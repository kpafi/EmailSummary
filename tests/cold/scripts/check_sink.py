#!/usr/bin/env python3
"""Prueft alle im Sink angekommenen Nachrichten auf verbotene Inhalte.

Bewertet den `content` so, wie DISCORD ihn rendern wuerde (Markdown aktiv).

Aufruf: check_sink.py [ab-zeile]
"""
import json, re, sys, unicodedata

LOG = "/home/kpafi/maildigest-coldtest/work/logs/sink.jsonl"
START = int(sys.argv[1]) if len(sys.argv) > 1 else 0

SECRETS = ["CTSECRETIMAP9f3a1b", "CTSECRETLLMKEY7c2e55", "CTSECRETTGTOKENa11ce0",
           "CTSECRETHOOKd0d0d0", "CTSECRET"]

TLDS = ("example|org|com|net|de|io|info|xyz|pdf|exe|zip|html|txt|docx|"
        "beispielsehrlangetopleveldomain|invalid|local|onion")

CHECKS = [
    ("URL-Schema", re.compile(r"(?i)\b(https?|ftp|file|data|javascript|mailto)\s*:", re.U)),
    ("www-Prefix", re.compile(r"(?i)(?<![\w.\[])www\.", re.U)),
    ("Markdown-Link", re.compile(r"\[[^\]\n]{1,80}\]\([^)\n]{1,200}\)")),
    ("HTML-Tag", re.compile(r"</?[A-Za-z][A-Za-z0-9]{0,15}(\s[^<>\n]{0,200})?/?>")),
    ("HTML-Entity", re.compile(r"&(#\d{2,6}|[a-zA-Z]{2,10});")),
    ("Markdown-Fett/Kursiv", re.compile(r"(\*\*|__|~~|\|\|)")),
    ("Backtick/Codefence", re.compile(r"`")),
    ("Discord-Massenping", re.compile(r"@(everyone|here)|<@[!&]?\d+>")),
    ("Markdown-Ueberschrift/Zitat", re.compile(r"(?m)^\s*(#{1,6}\s|>\s)")),
    # lebende Domain: punkt NICHT gebrochen, gefolgt von bekannter TLD
    ("lebende Domain", re.compile(r"(?i)(?<![\[\w])[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?"
                                  r"\.(?:" + TLDS + r")\b", re.U)),
    ("Mailadresse", re.compile(r"(?i)[\w.+-]+@[\w-]+\.[a-z]{2,}")),
    ("Zero-Width/Bidi", re.compile("[​-‏‪-‮⁦-⁩﻿]")),
    ("Steuerzeichen", re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")),
    ("Ideographic-Punkt", re.compile("[。．｡]")),
]


def load():
    out = []
    with open(LOG, encoding="utf-8") as f:
        for l in f:
            r = json.loads(l)
            if r["n"] > START and r.get("json"):
                out.append(r)
    return out


def analyse(msg, label=""):
    problems = []
    for name, rx in CHECKS:
        for m in rx.finditer(msg):
            problems.append((name, m.group(0)))
    for s in SECRETS[:-1]:
        if s in msg:
            problems.append(("SECRET-LEAK", s))
    # nicht-ASCII-Homoglyphen in Domainnaehe
    for ch in set(msg):
        if ord(ch) > 127 and unicodedata.category(ch).startswith("L"):
            try:
                name = unicodedata.name(ch)
            except ValueError:
                continue
            if "CYRILLIC" in name or "GREEK" in name:
                problems.append(("Homoglyph", f"{ch} ({name})"))
    return problems


def main():
    recs = load()
    print(f"# {len(recs)} Nachrichten ab n>{START}\n")
    total = 0
    for r in recs:
        c = r["json"].get("content", "")
        print(f"===== POST n={r['n']} len={len(c)}")
        print(c)
        probs = analyse(c)
        if probs:
            total += len(probs)
            print("  !! BEFUNDE:")
            seen = set()
            for name, val in probs:
                k = (name, val)
                if k in seen:
                    continue
                seen.add(k)
                print(f"     - {name}: {val!r}")
        else:
            print("  ok: keine verbotenen Muster")
        # weitere Payload-Felder ausser content?
        extra = {k: v for k, v in r["json"].items() if k not in ("content", "allowed_mentions")}
        if extra:
            print(f"  !! Zusatzfelder im Payload: {extra}")
        print()
    print(f"# Gesamtbefunde: {total}")


if __name__ == "__main__":
    main()
