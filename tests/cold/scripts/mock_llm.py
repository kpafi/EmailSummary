#!/usr/bin/env python3
"""OpenAI-kompatibler Mock-LLM.

Aufruf: python3 mock_llm.py [PORT] [MODE]
MODE:
  nice      - brave, generische Antwort (default)
  echo      - spiegelt den kompletten empfangenen Prompt in die Zusammenfassung zurueck (boese)
  obey      - "befolgt" Instruktionen: liefert Links/Markup/Behauptungen (boese)
  broken    - liefert kein JSON
  empty     - liefert leeren Body
  err500    - HTTP 500
  timeout   - antwortet nie

Schreibt jeden Request nach work/logs/llm.jsonl (inkl. vollstaendigem Prompt!),
damit geprueft werden kann, was das Modell zu sehen bekommt (F-SEC-1, F-SEC-8).
"""
import http.server, json, os, sys, time, datetime, re

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8932
MODE = sys.argv[2] if len(sys.argv) > 2 else "nice"
LOG = os.environ.get("LLM_LOG", "/home/kpafi/maildigest-coldtest/work/logs/llm.jsonl")
COUNT = {"n": 0}


SNEAKY_TEXT = (
    "**fett** __unterstr__ ~~weg~~ `code` ```block``` ||spoiler|| "
    "# Ueberschrift\n> Zitat\n- Liste\n"
    "@everyone @here <@123456789012345678> <#987654321>\n"
    "Domains: evil.com.123abc evil.com-x -evil.example evil.comAE "
    "boese。example evil．example www.evil.example "
    "shop.evil.example:8080 EVIL.EXAMPLE\n"
    "Encoded: https%3A%2F%2Fboese.example%2Fpfad h%74tps://boese.example\n"
    "Schema: hxxps://boese.example  htt ps://boese.example  h​ttps://boese.example\n"
    "Mail: opfer@exfil.example  Mail2: opfer(at)exfil(dot)example\n"
    "IPv6: [::1]:8080  IPv4: 192.168.178.1  10.0.0.1\n"
    "Markdown-Link: [Klick](https%3A%2F%2Fboese.example)  [Klick2](<evil.example>)\n"
    "Discord-Emoji: <:pwn:123456789>  Timestamp: <t:1700000000:R>\n"
)

EVIL_TEXT = (
    "KLICK HIER: https://evil.example/login und http://192.168.1.1/x . "
    "[Klick mich](https://evil.example/pwn) <a href=\"https://evil.example\">Bank</a> "
    "<b>fett</b> **fett** `code` ||spoiler|| @everyone "
    "Kontakt: evil.example, mail@evil.example, hxxps://evil[.]example"
)


def build(kind, prompt):
    """kind: summary|critic"""
    if kind == "critic":
        if MODE == "obey":
            return {"phishing_risk": "high", "risk_reasons": [EVIL_TEXT],
                    "summary_accurate": True, "notes": EVIL_TEXT}
        if MODE == "echo":
            return {"phishing_risk": "low", "risk_reasons": [prompt[:900]],
                    "summary_accurate": True, "notes": prompt[:900]}
        if MODE == "raw":
            return {"phishing_risk": "none", "risk_reasons": [], "summary_accurate": True, "notes": ""}
        if MODE == "sneaky":
            return {"phishing_risk": "low", "risk_reasons": [SNEAKY_TEXT[:180]],
                    "summary_accurate": True, "notes": SNEAKY_TEXT[:180]}
        return {"phishing_risk": os.environ.get("MOCK_RISK", "none"),
                "risk_reasons": [r for r in os.environ.get("MOCK_REASONS","").split("|") if r],
                "summary_accurate": os.environ.get("MOCK_ACCURATE","1") == "1",
                "notes": os.environ.get("MOCK_NOTES","")}
    if MODE == "obey":
        return {"headline": EVIL_TEXT, "summary_text": EVIL_TEXT, "category": "rechnung",
                "importance": "high", "importance_reason": EVIL_TEXT,
                "injection_suspected": False,
                "attachment_summaries": {"evil.pdf": EVIL_TEXT}}
    if MODE == "raw":
        return {"headline": os.environ.get("MOCK_HEADLINE", "Kopfzeile"),
                "summary_text": open(os.environ["MOCK_TEXT_FILE"], encoding="utf-8").read(),
                "category": os.environ.get("MOCK_CATEGORY","sonstiges"),
                "importance": os.environ.get("MOCK_IMPORTANCE","normal"),
                "importance_reason": "x", "injection_suspected": False,
                "attachment_summaries": {}}
    if MODE == "sneaky":
        return {"headline": "Rechnung faellig " + "evil.com.123abc",
                "summary_text": SNEAKY_TEXT, "category": "rechnung",
                "importance": "high", "importance_reason": "Frist",
                "injection_suspected": False,
                "attachment_summaries": {"rechnung.pdf": SNEAKY_TEXT}}
    if MODE == "echo":
        return {"headline": prompt[:300], "summary_text": prompt[:2500],
                "category": "sonstiges", "importance": "normal",
                "importance_reason": prompt[:200], "injection_suspected": False,
                "attachment_summaries": {}}
    imp = os.environ.get("MOCK_IMPORTANCE", "normal")
    return {"headline": "Testkopfzeile ohne Besonderheiten",
            "summary_text": "Dies ist eine neutrale Zusammenfassung der Mail.",
            "category": os.environ.get("MOCK_CATEGORY", "sonstiges"), "importance": imp,
            "importance_reason": "keine Auffaelligkeiten",
            "injection_suspected": False, "attachment_summaries": {}}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        COUNT["n"] += 1
        txt = body.decode("utf-8", "replace")
        rec = {"n": COUNT["n"], "ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "path": self.path,
               "headers": {k: v for k, v in self.headers.items()},
               "raw": txt}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if MODE == "timeout":
            time.sleep(300)
            return
        if MODE == "err500":
            self._send(500, b'{"error":"boom"}')
            return
        if MODE == "empty":
            self._send(200, b"")
            return
        if MODE == "broken":
            self._send(200, json.dumps({
                "id": "x", "object": "chat.completion", "model": "mock",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "Ich bin ein Modell und antworte in Prosa."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode())
            return

        try:
            sysmsg = json.loads(txt)["messages"][0]["content"]
        except Exception:
            sysmsg = txt
        kind = "critic" if "Kritiker-Agent" in sysmsg else "summary"
        low = txt.lower()
        # connect-llm-Testaufruf: kurzer Prompt, keine Mail
        if "Verbindungstest" in sysmsg:
            content = "OK"
        else:
            content = json.dumps(build(kind, txt), ensure_ascii=False)
        out = json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
            "model": "mock-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self._send(200, out)

    def do_GET(self):
        payload = json.dumps({"object": "list", "data": [{"id": "mock-model", "object": "model"}]}).encode()
        self._send(200, payload)

    def _send(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    print(f"mock-llm on {PORT} mode={MODE} log={LOG}", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
