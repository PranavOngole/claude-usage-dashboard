"""Live-usage relay for the public dashboard.

GitHub Pages is static, so the public page polls this tiny service
instead. Pranav's Mac POSTs the latest usage snapshot (/push, token
gated); anyone may GET /live. State is in memory only: deployed with
max-instances=1, and after a cold start the next push repopulates it
within seconds.
"""
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUSH_TOKEN = os.environ["PUSH_TOKEN"]
ALLOWED_ORIGINS = {
    "https://pranavongole.github.io",
    "http://localhost:8377",
    "http://127.0.0.1:8377",
}

state = {"snapshot": None}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Push-Token")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/live":
            with lock:
                snap = state["snapshot"]
            if snap is None:
                self._json(200, {"total_tokens": 0, "est_cost": 0, "updated_at": None})
            else:
                self._json(200, snap)
        else:
            self._json(200, {"ok": True})

    def do_POST(self):
        if self.path != "/push":
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("X-Push-Token") != PUSH_TOKEN:
            self._json(403, {"error": "forbidden"})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 100_000:
            self._json(413, {"error": "too large"})
            return
        try:
            snap = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "bad json"})
            return
        snap["relayed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with lock:
            state["snapshot"] = snap
        self._json(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
