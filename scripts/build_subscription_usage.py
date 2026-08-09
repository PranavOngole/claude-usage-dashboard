"""Aggregate Claude Code token usage from local session transcripts.

Claude Code writes one JSONL file per session under ~/.claude/projects/.
Each assistant message carries a usage block (input, output, cache tokens)
plus the model and a timestamp. This script sums them per local day per
model and writes data/subscription_usage.json in the same shape as the
Admin API usage report, so index.html can render both sources with the
same code.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "subscription_usage.json"
MAX_DAYS = 90


def iter_usage_records():
    seen = set()
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    msg = entry.get("message") or {}
                    usage = msg.get("usage")
                    ts = entry.get("timestamp")
                    model = msg.get("model") or "unknown"
                    if not usage or not ts or model.startswith("<"):
                        continue
                    # The same message can appear in multiple files when a
                    # session is resumed or forked; count it once.
                    key = (msg.get("id"), entry.get("requestId"))
                    if key != (None, None):
                        if key in seen:
                            continue
                        seen.add(key)
                    yield ts, model, usage
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
    """Never let history erode. Claude Code deletes old session transcripts
    (retention window), so a pure recount silently loses old days. Merge the
    recount with the previously published file: per day, keep whichever side
    counted more tokens. Dedup means a recount never overcounts, so the
    bigger number is always the more complete one.
    """
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
    if not PROJECTS_DIR.is_dir():
        print(f"ERROR: {PROJECTS_DIR} not found.", file=sys.stderr)
        sys.exit(1)

    cutoff = (datetime.now().astimezone() - timedelta(days=MAX_DAYS)).date()
    days = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    records = 0

    for ts, model, usage in iter_usage_records():
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        day = when.date()
        if day < cutoff:
            continue
        cc = usage.get("cache_creation") or {}
        bucket = days[day.isoformat()][model]
        bucket["uncached_input_tokens"] += usage.get("input_tokens") or 0
        bucket["cache_read_input_tokens"] += usage.get("cache_read_input_tokens") or 0
        bucket["output_tokens"] += usage.get("output_tokens") or 0
        bucket["ephemeral_1h_input_tokens"] += cc.get("ephemeral_1h_input_tokens") or 0
        bucket["ephemeral_5m_input_tokens"] += (
            cc.get("ephemeral_5m_input_tokens")
            if cc
            else (usage.get("cache_creation_input_tokens") or 0)
        ) or 0
        records += 1

    data = []
    for day in sorted(days):
        results = []
        for model in sorted(days[day]):
            b = days[day][model]
            results.append({
                "model": model,
                "uncached_input_tokens": b["uncached_input_tokens"],
                "cache_read_input_tokens": b["cache_read_input_tokens"],
                "cache_creation": {
                    "ephemeral_1h_input_tokens": b["ephemeral_1h_input_tokens"],
                    "ephemeral_5m_input_tokens": b["ephemeral_5m_input_tokens"],
                },
                "output_tokens": b["output_tokens"],
            })
        data.append({"starting_at": f"{day}T00:00:00Z", "results": results})

    data = merge_with_published(data)

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "claude-code-local",
        "data": data,
    }

    os.makedirs(OUTPUT_PATH.parent, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f)

    print(f"Aggregated {records} messages into {len(data)} days -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
