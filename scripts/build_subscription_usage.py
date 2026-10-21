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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pricing

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "subscription_usage.json"


def visible_chars(msg):
    """Characters of text and tool input in one logged copy of a message."""
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    n = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            n += len(block.get("text") or "")
        elif block.get("type") == "tool_use":
            n += len(json.dumps(block.get("input") or {}))
    return n


def estimated_output(recorded, chars, final, ratio):
    """Output tokens to count for one message.

    The API's final usage for a message carries `iterations`; until it
    arrives, Claude Code's copies hold the placeholder count from the start
    of the stream (a handful of tokens). Claude Code 2.1.284 to 2.1.286
    often never logged the final copy. When no copy is final and the
    recorded count cannot even cover the message's visible text (~4
    chars/token), estimate it as visible chars x the model's output per
    visible char on finalized messages (thinking included). Tested on
    held-out finalized messages, the estimate's total landed within 6% of
    the actual total for every model with a real gap (2026-10-20)."""
    if final or ratio is None or recorded >= chars / 4:
        return recorded
    return max(recorded, round(chars * ratio))


def iter_usage_records():
    """Yield (timestamp, model, usage, visible_chars, final) once per API
    message.

    Claude Code logs the same message (same id + requestId) once per
    content block while it streams, with output_tokens growing on each
    line. Keeping the first copy undercounts output by roughly a third, so
    keep the copy with the highest output_tokens (the final one). Input and
    cache fields are identical across copies. Each copy holds a different
    block, so visible text is summed over distinct lines."""
    best = {}
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
                    chars = visible_chars(msg)
                    final = "iterations" in usage
                    key = (msg.get("id"), entry.get("requestId"))
                    if key == (None, None):
                        yield ts, model, usage, chars, final
                        continue
                    rec = best.get(key)
                    if rec is None:
                        best[key] = rec = {"ts": ts, "model": model, "usage": usage,
                                           "chars": 0, "final": False, "lines": set()}
                    elif (usage.get("output_tokens") or 0) > (rec["usage"].get("output_tokens") or 0):
                        rec.update(ts=ts, model=model, usage=usage)
                    uid = entry.get("uuid")
                    if uid is None or uid not in rec["lines"]:
                        rec["lines"].add(uid)
                        rec["chars"] += chars
                    rec["final"] = rec["final"] or final
        except OSError:
            continue
    for rec in best.values():
        yield rec["ts"], rec["model"], rec["usage"], rec["chars"], rec["final"]


def output_ratios(records):
    """Per-model output tokens per visible char over finalized messages."""
    out, chars = Counter(), Counter()
    for _, model, usage, n, final in records:
        if final and n > 0:
            out[model] += usage.get("output_tokens") or 0
            chars[model] += n
    return {m: round(out[m] / chars[m], 4) for m in chars}


def measured_output(row):
    return (row.get("output_tokens") or 0) - (row.get("imputed_output_tokens") or 0)


def merge_rows(pub, new):
    """One model's day counted twice: keep the larger of each token field.
    Either count can only fall short (a recount misses deleted transcripts;
    builds before 2026-10-09 kept each message's first streamed output
    count), so a whole-day comparison could keep fuller input but older,
    smaller output (2026-07-07 to 07-09 lost ~$70 that way)."""
    row = dict(new)
    for f in ("uncached_input_tokens", "cache_read_input_tokens"):
        row[f] = max(pub.get(f) or 0, new.get(f) or 0)
    pc, nc = pub.get("cache_creation") or {}, new.get("cache_creation") or {}
    row["cache_creation"] = {f: max(pc.get(f) or 0, nc.get(f) or 0)
                             for f in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens")}
    if measured_output(pub) > measured_output(new):
        row["output_tokens"] = pub.get("output_tokens") or 0
        row["imputed_output_tokens"] = pub.get("imputed_output_tokens") or 0
    return row


def merge_with_published(fresh):
    """Never let history erode. Claude Code deletes old session transcripts
    (retention window), so a pure recount silently loses old days. Merge the
    recount with the previously published file per day and model, field by
    field (merge_rows). Dedup means no count ever overcounts, so the bigger
    number is always the more complete one.

    An unreadable published file stops the build: falling back to the
    recount alone is how 2026-10-01 published a file missing 13 days (a git
    autostash conflict had left markers in it).
    """
    try:
        with open(OUTPUT_PATH) as f:
            published = json.load(f).get("data", [])
    except FileNotFoundError:
        return fresh
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {OUTPUT_PATH} is not valid JSON ({e}). Refusing to "
                 f"overwrite history; restore it with git checkout HEAD -- {OUTPUT_PATH}.")
    by_day = {b["starting_at"][:10]: b for b in published}
    for b in fresh:
        day = b["starting_at"][:10]
        old = {r["model"]: r for r in by_day.get(day, {}).get("results", [])}
        rows = {r["model"]: merge_rows(old.pop(r["model"]), r) if r["model"] in old else r
                for r in b["results"]}
        rows.update(old)  # models only the published side still has
        by_day[day] = {**b, "results": [rows[m] for m in sorted(rows)]}
    return [by_day[d] for d in sorted(by_day)]


def main():
    if not PROJECTS_DIR.is_dir():
        print(f"ERROR: {PROJECTS_DIR} not found.", file=sys.stderr)
        sys.exit(1)

    # Every day the transcripts still cover is recounted, so fixes to the
    # counting reach old days too; the merge keeps whichever side is fuller.
    records = list(iter_usage_records())
    ratios = output_ratios(records)
    days = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    for ts, model, usage, chars, final in records:
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        cc = usage.get("cache_creation") or {}
        recorded = usage.get("output_tokens") or 0
        output = estimated_output(recorded, chars, final, ratios.get(model))
        bucket = days[when.date().isoformat()][model]
        bucket["uncached_input_tokens"] += usage.get("input_tokens") or 0
        bucket["cache_read_input_tokens"] += usage.get("cache_read_input_tokens") or 0
        bucket["output_tokens"] += output
        bucket["imputed_output_tokens"] += output - recorded
        bucket["ephemeral_1h_input_tokens"] += cc.get("ephemeral_1h_input_tokens") or 0
        bucket["ephemeral_5m_input_tokens"] += (
            cc.get("ephemeral_5m_input_tokens")
            if cc
            else (usage.get("cache_creation_input_tokens") or 0)
        ) or 0

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
                "imputed_output_tokens": b["imputed_output_tokens"],
            })
        data.append({"starting_at": f"{day}T00:00:00Z", "results": results})

    data = merge_with_published(data)
    # Stamp point-in-time costs: every bucket is priced at the rates in
    # effect on its day.
    data = [pricing.stamp_bucket(b) for b in data]

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "claude-code-local",
        # Read by the live tracker so today's tile estimates the same way.
        "output_per_visible_char": ratios,
        "data": data,
    }

    os.makedirs(OUTPUT_PATH.parent, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f)

    print(f"Aggregated {len(records)} messages into {len(data)} days -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
