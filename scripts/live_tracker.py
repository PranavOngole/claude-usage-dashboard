"""In-memory live tracker for today's Claude Code usage.

The full builder rescans every transcript; too heavy to run each second.
This tracker instead tails the JSONL session files incrementally: it
remembers a byte offset per file, reads only newly appended lines on each
tick, and keeps today's per-model token totals in memory. GET /live on the
helper serves the snapshot; the local dashboard polls it every second.

Only complete lines are consumed (a partially written line stays unread
until its newline arrives). Dedup matches the builder: (message.id,
requestId) counted once. Day rollover resets everything at local midnight.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pricing

PROJECTS_DIR = Path.home() / ".claude" / "projects"
LISTING_INTERVAL = 30.0   # how often to look for new session files
REFRESH_INTERVAL = 0.8    # minimum seconds between file tail reads


class LiveTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._day = None
        self._offsets = {}
        self._seen = set()
        self._by_model = {}
        self._files = []
        self._last_listing = 0.0
        self._last_refresh = 0.0

    def _reset_day(self, day):
        self._day = day
        self._offsets = {}
        self._seen = set()
        self._by_model = {}
        self._files = []
        self._last_listing = 0.0

    def _list_files(self):
        day_start = (
            datetime.now()
            .astimezone()
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        found = []
        for path in PROJECTS_DIR.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= day_start:
                    found.append(path)
            except OSError:
                continue
        return found

    def _ingest(self, path):
        try:
            size = path.stat().st_size
        except OSError:
            return
        offset = self._offsets.get(path, 0)
        if size < offset:
            offset = 0  # file replaced/truncated; re-read
        if size == offset:
            return
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError:
            return
        if chunk.endswith(b"\n"):
            complete, new_offset = chunk, size
        else:
            cut = chunk.rfind(b"\n")
            if cut == -1:
                return  # one incomplete line; wait for more
            complete, new_offset = chunk[: cut + 1], offset + cut + 1
        self._offsets[path] = new_offset

        for raw in complete.splitlines():
            if b'"usage"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message") or {}
            usage = msg.get("usage")
            ts = entry.get("timestamp")
            model = msg.get("model") or "unknown"
            if not usage or not ts or model.startswith("<"):
                continue
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            if when.date().isoformat() != self._day:
                continue
            key = (msg.get("id"), entry.get("requestId"))
            if key != (None, None):
                if key in self._seen:
                    continue
                self._seen.add(key)
            cc = usage.get("cache_creation") or {}
            b = self._by_model.setdefault(
                model,
                {"uncached": 0, "cache_read": 0, "cc5m": 0, "cc1h": 0, "output": 0},
            )
            b["uncached"] += usage.get("input_tokens") or 0
            b["cache_read"] += usage.get("cache_read_input_tokens") or 0
            b["cc1h"] += cc.get("ephemeral_1h_input_tokens") or 0
            b["cc5m"] += (
                cc.get("ephemeral_5m_input_tokens")
                if cc
                else (usage.get("cache_creation_input_tokens") or 0)
            ) or 0
            b["output"] += usage.get("output_tokens") or 0

    def snapshot(self):
        with self._lock:
            now = time.time()
            today = datetime.now().astimezone().date().isoformat()
            if today != self._day:
                self._reset_day(today)
            if now - self._last_listing > LISTING_INTERVAL:
                self._files = self._list_files()
                self._last_listing = now
            if now - self._last_refresh > REFRESH_INTERVAL:
                for path in self._files:
                    self._ingest(path)
                self._last_refresh = now

            total_in = total_out = 0
            total_cost = 0.0
            models = {}
            for model, b in self._by_model.items():
                inp = b["uncached"] + b["cache_read"] + b["cc5m"] + b["cc1h"]
                cost = (
                    pricing.cost_for(
                        self._day, model,
                        b["uncached"], b["cache_read"], b["cc5m"], b["cc1h"], b["output"],
                    )
                    or 0.0
                )
                total_in += inp
                total_out += b["output"]
                total_cost += cost
                models[model] = {
                    "tokens": inp + b["output"],
                    "est_cost": round(cost, 4),
                }
            return {
                "date": self._day,
                "total_tokens": total_in + total_out,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "est_cost": round(total_cost, 4),
                "models": models,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
