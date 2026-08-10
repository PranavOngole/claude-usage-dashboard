#!/usr/bin/env python3
"""Local one-click refresh helper for the usage dashboard.

Listens on 127.0.0.1:8377 and does two jobs:

1. Serves the dashboard itself (http://localhost:8377). Same-origin, so
   the refresh button works with zero browser security friction, and the
   data files are read straight from disk (no GitHub Pages wait).
2. POST /refresh runs refresh_subscription.sh (pull, rebuild, commit,
   push) so the public GitHub Pages copy is republished too; GET /ping
   lets the public page detect it is being viewed on this Mac.

The public https://pranavongole.github.io copy may also call /ping and
/refresh cross-origin where the browser allows it (Chrome's Local
Network Access rules apply there; the localhost URL never needs them).

Kept alive by launchd: com.pranav.claude-usage-refresh-server.
"""
import json
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from live_tracker import LiveTracker

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "refresh_subscription.sh"
ALLOWED_ORIGINS = {
    "https://pranavongole.github.io",
    "http://localhost:8377",
    "http://127.0.0.1:8377",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

refresh_lock = threading.Lock()
live_tracker = LiveTracker()


class Handler(BaseHTTPRequestHandler):
    def _log(self, note):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        origin = self.headers.get("Origin", "-")
        print(f"{stamp} {self.command} {self.path} origin={origin} {note}", flush=True)

    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome Private Network / Local Network Access preflight opt-in
        # (older and newer header names; harmless to send both).
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Local-Network", "true")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel):
        target = (REPO / rel).resolve()
        ctype = CONTENT_TYPES.get(target.suffix.lower())
        # Only files inside the repo, only known static types.
        if not (str(target).startswith(str(REPO) + "/") and ctype and target.is_file()):
            self._send_json(404, {"error": "not found"})
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._log("preflight")
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/live":
            # Polled every second by the local page; deliberately unlogged.
            self._send_json(200, live_tracker.snapshot())
        elif path == "/ping":
            self._log("")
            self._send_json(200, {"ok": True})
        elif path in ("/", "/index.html"):
            self._send_file("index.html")
        elif path.startswith("/data/"):
            self._send_file(path.lstrip("/"))
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path != "/refresh":
            self._send_json(404, {"error": "not found"})
            return
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            self._log("refresh REJECTED")
            self._send_json(403, {"error": "origin not allowed"})
            return
        self._log("refresh requested")
        if not refresh_lock.acquire(blocking=False):
            self._send_json(409, {"error": "a refresh is already running"})
            return
        try:
            # mode=local rebuilds the data files in place without the git
            # commit/push, for the local page's hands-free auto-refresh.
            if query == "mode=local":
                cmd = [
                    "/bin/bash", "-c",
                    "python3 scripts/build_subscription_usage.py"
                    " && python3 scripts/build_claudeai_usage.py",
                ]
            else:
                cmd = ["/bin/bash", str(SCRIPT)]
            proc = subprocess.run(
                cmd,
                cwd=str(REPO),
                capture_output=True,
                text=True,
                timeout=300,
            )
            tail = (proc.stdout + proc.stderr)[-2000:]
            if proc.returncode == 0:
                self._log("refresh ok")
                self._send_json(200, {"ok": True, "output": tail})
            else:
                self._log(f"refresh FAILED exit={proc.returncode}")
                self._send_json(
                    500, {"ok": False, "exit": proc.returncode, "output": tail}
                )
        except subprocess.TimeoutExpired:
            self._log("refresh TIMED OUT")
            self._send_json(500, {"ok": False, "error": "refresh timed out"})
        finally:
            refresh_lock.release()

    def log_message(self, fmt, *args):
        pass  # default per-line noise off; _log records what matters


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8377), Handler).serve_forever()
