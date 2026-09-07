"""In-memory live trackers for today's Claude Code and Codex CLI usage.

The full builders rescan every transcript; too heavy to run each second.
These trackers instead tail the JSONL session files incrementally: each
remembers a byte offset per file, reads only newly appended lines on each
tick, and keeps today's per-model token totals in memory. GET /live on the
helper serves a snapshot; the local dashboard polls it every second.

Only complete lines are consumed (a partially written line stays unread
until its newline arrives). Day rollover resets everything at local
midnight. _BaseLiveTracker owns that machinery; each subclass only knows
its own session directories, how to recognize a usage event in a raw
line, and how to dedupe replayed events.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pricing

LISTING_INTERVAL = 30.0   # how often to look for new session files
REFRESH_INTERVAL = 0.8    # minimum seconds between file tail reads


class _BaseLiveTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._day = None
        self._offsets = {}
        self._seen = set()
        self._by_model = {}
        self._files = []
        self._file_state = {}   # per-file parser state, e.g. Codex's current model
        self._last_listing = 0.0
        self._last_refresh = 0.0

    # ---- subclass hooks ----

    def _session_dirs(self):
        raise NotImplementedError

    def _parse_chunk(self, state, complete_bytes):
        """Yield (dedupe_key, timestamp, model, usage_delta) per usage
        event found in this newly-appended chunk. `state` is a dict private
        to this file that persists across ingest calls (e.g. Codex's
        current model, carried from earlier lines in the same file).
        dedupe_key may be None to skip dedup for that event. usage_delta
        keys: uncached, cache_read, cc5m, cc1h, output."""
        raise NotImplementedError

    # ---- shared tailing machinery ----

    def _reset_day(self, day):
        self._day = day
        self._offsets = {}
        self._seen = set()
        self._by_model = {}
        self._files = []
        self._file_state = {}
        self._last_listing = 0.0

    def _list_files(self):
        day_start = (
            datetime.now()
            .astimezone()
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        found = []
        for root in self._session_dirs():
            if not root.is_dir():
                continue
            for path in root.rglob("*.jsonl"):
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
            self._file_state.pop(path, None)
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

        state = self._file_state.setdefault(path, {})
        for key, ts, model, usage in self._parse_chunk(state, complete):
            if not ts or not usage:
                continue
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            if when.date().isoformat() != self._day:
                continue
            if key is not None:
                if key in self._seen:
                    continue
                self._seen.add(key)
            b = self._by_model.setdefault(
                model or "unknown",
                {"uncached": 0, "cache_read": 0, "cc5m": 0, "cc1h": 0, "output": 0},
            )
            b["uncached"] += usage.get("uncached", 0)
            b["cache_read"] += usage.get("cache_read", 0)
            b["cc5m"] += usage.get("cc5m", 0)
            b["cc1h"] += usage.get("cc1h", 0)
            b["output"] += usage.get("output", 0)

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


class ClaudeLiveTracker(_BaseLiveTracker):
    """Tails ~/.claude/projects/**/*.jsonl. Dedup matches the subscription
    builder: (message.id, requestId) counted once."""

    def _session_dirs(self):
        return [Path.home() / ".claude" / "projects"]

    def _parse_chunk(self, state, complete_bytes):
        for raw in complete_bytes.splitlines():
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
            cc = usage.get("cache_creation") or {}
            key = (msg.get("id"), entry.get("requestId"))
            if key == (None, None):
                key = None
            yield key, ts, model, {
                "uncached": usage.get("input_tokens") or 0,
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "cc1h": cc.get("ephemeral_1h_input_tokens") or 0,
                "cc5m": (
                    cc.get("ephemeral_5m_input_tokens")
                    if cc
                    else (usage.get("cache_creation_input_tokens") or 0)
                ) or 0,
                "output": usage.get("output_tokens") or 0,
            }


class CodexLiveTracker(_BaseLiveTracker):
    """Tails ~/.codex/{sessions,archived_sessions}/**/*.jsonl. Model comes
    from session/turn context lines, not the usage event itself, so it's
    tracked per-file in `state` while streaming (same as the full Codex
    builder). Dedup matches the builder: a resumed session can replay
    history into a new rollout file, so (timestamp, cumulative total)
    identifies duplicates rather than an id."""

    def _session_dirs(self):
        home = Path.home() / ".codex"
        return [home / "sessions", home / "archived_sessions"]

    @staticmethod
    def _extract_model(payload):
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        if model:
            return model
        tc = payload.get("turn_context")
        if isinstance(tc, dict):
            return tc.get("model")
        return None

    def _parse_chunk(self, state, complete_bytes):
        for raw in complete_bytes.splitlines():
            if b'"token_count"' in raw:
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = entry.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                total = (info.get("total_token_usage") or {}).get("total_tokens")
                ts = entry.get("timestamp")
                if not ts or not last:
                    continue
                inp = last.get("input_tokens") or 0
                cached = last.get("cached_input_tokens") or 0
                model = state.get("model", "codex-unknown")
                yield (ts, total), ts, model, {
                    "uncached": max(inp - cached, 0),
                    "cache_read": cached,
                    "cc1h": 0,
                    "cc5m": 0,
                    "output": last.get("output_tokens") or 0,
                }
            elif b'"model"' in raw:
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                found = self._extract_model(entry.get("payload"))
                if found:
                    state["model"] = found
