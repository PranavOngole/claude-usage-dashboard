"""Fetch Anthropic API usage and billed cost from the Admin API.

The usage report gives tokens per UTC day per model; the cost report gives
what Anthropic actually billed (USD, per day and line item), which already
reflects fast mode, batch, data residency and web search. Each day/model row
carries both, so the API tab shows billed dollars rather than a list-price
estimate. Requires an Admin API key (sk-ant-admin01-...), which only
organization accounts can create. Writes data/usage.json in the same shape
as the other sources.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pricing

API = "https://api.anthropic.com/v1/organizations"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "usage.json"
BACKFILL_FROM = "2025-01-01"  # first run: everything the reports still hold
REFRESH_DAYS = 35             # later runs: re-read recent days, which can still change


def get_report(path, params, key):
    """Every page of one report, oldest bucket first."""
    buckets, page = [], None
    while True:
        query = list(params) + [("limit", "31")] + ([("page", page)] if page else [])
        req = urllib.request.Request(
            f"{API}/{path}?{urllib.parse.urlencode(query)}",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            hint = (" It needs an Admin API key (sk-ant-admin01-...), which only organization"
                    " accounts can create." if e.code in (401, 403) else "")
            sys.exit(f"ERROR: {path} returned HTTP {e.code}.{hint}")
        buckets.extend(body.get("data", []))
        if not body.get("has_more"):
            return buckets
        page = body["next_page"]


def empty_row(name):
    return {
        "model": name,
        "uncached_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
        "output_tokens": 0,
        "web_search_requests": 0,
    }


def build_days(usage, costs):
    """Join the two reports into day buckets: one row per model, plus one
    per non-token charge (web_search, code_execution, session_usage)."""
    rows = defaultdict(dict)
    for b in usage:
        day = b["starting_at"][:10]
        for r in b.get("results", []):
            name = r.get("model") or "unknown"
            row = rows[day].setdefault(name, empty_row(name))
            cc = r.get("cache_creation") or {}
            row["uncached_input_tokens"] += r.get("uncached_input_tokens") or 0
            row["cache_read_input_tokens"] += r.get("cache_read_input_tokens") or 0
            row["cache_creation"]["ephemeral_1h_input_tokens"] += cc.get("ephemeral_1h_input_tokens") or 0
            row["cache_creation"]["ephemeral_5m_input_tokens"] += cc.get("ephemeral_5m_input_tokens") or 0
            row["output_tokens"] += r.get("output_tokens") or 0
            row["web_search_requests"] += (r.get("server_tool_use") or {}).get("web_search_requests") or 0

    billed = defaultdict(float)
    for b in costs:
        day = b["starting_at"][:10]
        for r in b.get("results", []):
            if r.get("cost_type") == "tokens":
                name = r.get("model") or "unknown"
            else:
                name = r.get("cost_type") or r.get("description") or "other"
            # Amounts are decimal strings in cents.
            billed[(day, name)] += float(r.get("amount") or 0) / 100

    for day, name in billed:
        rows[day].setdefault(name, empty_row(name))

    data = []
    for day in sorted(rows):
        for name, row in rows[day].items():
            if (day, name) in billed:
                row["est_cost"] = round(billed[(day, name)], 6)
                row["cost_basis"] = "billed"
            else:
                # Not in the cost report yet (it can lag the usage report).
                cc = row["cache_creation"]
                row["est_cost"] = pricing.cost_for(
                    day, name, row["uncached_input_tokens"], row["cache_read_input_tokens"],
                    cc["ephemeral_5m_input_tokens"], cc["ephemeral_1h_input_tokens"], row["output_tokens"])
                row["cost_basis"] = "list_price"
        data.append({"starting_at": f"{day}T00:00:00Z",
                     "results": [rows[day][n] for n in sorted(rows[day])]})
    return data


def merge_with_published(fresh):
    """The reports are the truth for every day they return, so those days
    replace the published ones. Days they no longer return (retention) stay
    as published, so history never shrinks."""
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
        by_day[b["starting_at"][:10]] = b
    return [by_day[d] for d in sorted(by_day)]


def has_history():
    try:
        with open(OUTPUT_PATH) as f:
            return bool(json.load(f).get("data"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def main():
    key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if not key:
        sys.exit("ERROR: ANTHROPIC_ADMIN_KEY is not set (repo secret on GitHub).")

    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=REFRESH_DAYS)).isoformat() if has_history() else BACKFILL_FROM
    window = [("starting_at", f"{start}T00:00:00Z"),
              ("ending_at", f"{today + timedelta(days=1)}T00:00:00Z"),
              ("bucket_width", "1d")]
    usage = get_report("usage_report/messages", window + [("group_by[]", "model")], key)
    costs = get_report("cost_report", window + [("group_by[]", "description")], key)
    fresh = build_days(usage, costs)
    data = merge_with_published(fresh)

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "anthropic-admin-api",
        "data": data,
    }
    os.makedirs(OUTPUT_PATH.parent, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f)
    print(f"Fetched {start} to {today}: {len(fresh)} days with usage; {len(data)} days total -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
