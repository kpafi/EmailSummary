#!/usr/bin/env python3
"""Mock-Discord-Webhook-Sink. Schreibt jeden POST als JSON-Zeile nach work/logs/sink.jsonl.

Aufruf: python3 sink_server.py [PORT] [MODE]
MODE: ok (default) | die_after_1 (erster POST ok, danach Verbindungsabbruch) | slow | http500
"""
import http.server, json, os, sys, time, datetime

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8931
MODE = sys.argv[2] if len(sys.argv) > 2 else "ok"
LOG = os.environ.get("SINK_LOG", "/home/kpafi/maildigest-coldtest/work/logs/sink.jsonl")
COUNT = {"n": 0}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _log(self, body):
        COUNT["n"] += 1
        rec = {
            "n": COUNT["n"],
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "raw": body.decode("utf-8", "replace"),
        }
        try:
            rec["json"] = json.loads(body.decode("utf-8"))
        except Exception:
            rec["json"] = None
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        rec = self._log(body)
        if MODE == "die_after_1" and rec["n"] >= 2:
            self.close_connection = True
            try:
                self.connection.close()
            except Exception:
                pass
            return
        if MODE == "http500":
            self.send_response(500)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"boom!")
            return
        if MODE == "slow":
            time.sleep(30)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # Discord-Webhook-Healthcheck
        self._log(b"")
        payload = json.dumps({"id": "1", "type": 1, "name": "coldtest-hook",
                              "channel_id": "42", "guild_id": "43"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    print(f"sink on {PORT} mode={MODE} log={LOG}", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
