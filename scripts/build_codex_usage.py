"""Aggregate OpenAI Codex CLI token usage from local session rollouts.

Codex writes one rollout JSONL per session under ~/.codex/sessions (and
archived_sessions). Every API call emits an event_msg with payload type
token_count carrying last_token_usage: input_tokens (cached included),
cached_input_tokens, output_tokens (reasoning billed as output). The
model comes from session/turn context lines and can change mid-session,
so it is tracked statefully while streaming each file.

Output: data/codex_usage.json in the dashboard's shared shape. Cached
input maps to cache_read_input_tokens (OpenAI bills it at 10% of input,
matching the pricing engine's cache_read_multiplier); uncached input is
input minus cached; there is no cache-write premium so cache_creation is
always zero. Costs are stamped point-in-time via pricing.py. Days merge
with the previously published file so pruned sessions never erode
history (same guard as the Claude builder).
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pricing

SESSION_DIRS = [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
]
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "codex_usage.json"


def extract_model(payload):
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if model:
        return model
    tc = payload.get("turn_context")
    if isinstance(tc, dict):
        return tc.get("model")
    return None


def iter_usage():
    seen = set()
    for root in SESSION_DIRS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            model = "codex-unknown"
            try:
                with open(path, "r", errors="replace") as f:
                    for line in f:
                        if '"token_count"' in line:
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
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
                            # A resumed session can replay history into a
                            # new rollout file; the cumulative counter at
                            # the same instant identifies duplicates.
                            key = (ts, total)
                            if key in seen:
                                continue
                            seen.add(key)
                            yield ts, model, last
                        elif '"model"' in line:
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            found = extract_model(entry.get("payload"))
                            if found:
                                model = found
            except OSError:
                continue


def bucket_total(bucket):
    total = 0
    for r in bucket.get("results", []):
        cc = r.get("cache_creation") or {}
        total += (
            (r.get("uncached_input_tokens") or 0)
            + (r.get("cache_read_input_tokens") or 0)
            + (r.get("output_tokens") or 0)
            + (cc.get("ephemeral_1h_input_tokens") or 0)
            + (cc.get("ephemeral_5m_input_tokens") or 0)
        )
    return total


def merge_with_published(fresh):
    try:
        published = json.load(open(OUTPUT_PATH)).get("data", [])
    except (OSError, json.JSONDecodeError):
        return fresh
    by_day = {b["starting_at"][:10]: b for b in published}
    for b in fresh:
        day = b["starting_at"][:10]
        if day not in by_day or bucket_total(b) >= bucket_total(by_day[day]):
            by_day[day] = b
    return [by_day[d] for d in sorted(by_day)]


def main():
    days = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    records = 0
    for ts, model, last in iter_usage():
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        inp = last.get("input_tokens") or 0
        cached = last.get("cached_input_tokens") or 0
        b = days[when.date().isoformat()][model]
        b["uncached"] += max(inp - cached, 0)
        b["cache_read"] += cached
        b["output"] += last.get("output_tokens") or 0
        records += 1

    data = []
    for day in sorted(days):
        results = []
        for model in sorted(days[day]):
            b = days[day][model]
            results.append({
                "model": model,
                "uncached_input_tokens": b["uncached"],
                "cache_read_input_tokens": b["cache_read"],
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                },
                "output_tokens": b["output"],
            })
        data.append({"starting_at": f"{day}T00:00:00Z", "results": results})

    data = merge_with_published(data)
    data = [pricing.stamp_bucket(b) for b in data]

    for b in data:
        for r in b["results"]:
            if r.get("est_cost") is None:
                print(f"WARNING: no published rate for model {r['model']} on {b['starting_at'][:10]}")

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "codex-cli-local",
        "data": data,
    }
    os.makedirs(OUTPUT_PATH.parent, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f)
    print(f"Aggregated {records} Codex calls into {len(data)} days -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
