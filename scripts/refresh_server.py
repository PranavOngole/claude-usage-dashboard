#!/usr/bin/env python3
"""Local one-click refresh helper for the usage dashboard.

Listens on 127.0.0.1:8377. The dashboard's refresh button POSTs /refresh,
which runs refresh_subscription.sh (pull, rebuild, commit, push) so GitHub
Pages republishes with fresh data; GET /ping lets the page detect that it
is being viewed on this Mac. Off this Mac the page can't reach the helper
and the button falls back to reloading the published data.

Kept alive by launchd: com.pranav.claude-usage-refresh-server.
"""
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "refresh_subscription.sh"
ALLOWED_ORIGINS = {
    "https://pranavongole.github.io",
    "http://localhost:8000",  # python3 -m http.server, for local dev
    "http://127.0.0.1:8000",
}

refresh_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome Private Network Access preflight (public https page
        # calling a loopback address) requires this opt-in.
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/ping":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/refresh":
            self._send_json(404, {"error": "not found"})
            return
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            self._send_json(403, {"error": "origin not allowed"})
            return
        if not refresh_lock.acquire(blocking=False):
            self._send_json(409, {"error": "a refresh is already running"})
            return
        try:
            proc = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            tail = (proc.stdout + proc.stderr)[-2000:]
            if proc.returncode == 0:
                self._send_json(200, {"ok": True, "output": tail})
            else:
                self._send_json(
                    500, {"ok": False, "exit": proc.returncode, "output": tail}
                )
        except subprocess.TimeoutExpired:
            self._send_json(500, {"ok": False, "error": "refresh timed out"})
        finally:
            refresh_lock.release()

    def log_message(self, fmt, *args):
        pass  # keep the launchd log to real errors only


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8377), Handler).serve_forever()
