"""Point-in-time cost stamping for the usage builders.

Rates live in data/pricing.json as dated periods. cost_for(day, ...) prices
a day's usage with the rates in effect ON that day, and the builders stamp
the result into the data file as est_cost. The page displays the stamp, so
a future rate change (a new period appended to pricing.json) never
re-prices history.
"""

import json
from pathlib import Path

PRICING_PATH = Path(__file__).resolve().parent.parent / "data" / "pricing.json"

_cfg = json.load(open(PRICING_PATH))
# Newest first, so the first period at-or-before the day wins.
_periods = sorted(_cfg["periods"], key=lambda p: p["effective_from"], reverse=True)


def _rates_for(day, model):
    """Return [input, output] $/M for the period covering `day` (YYYY-MM-DD)."""
    for period in _periods:
        if period["effective_from"] <= day:
            rates = period["rates"]
            # Longest matching prefix wins (claude-opus-4-8 over claude-opus-4).
            best = None
            for prefix, r in rates.items():
                if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                    best = (prefix, r)
            if best:
                return best[1]
    return None


def cost_for(day, model, uncached, cache_read, cc_5m, cc_1h, output):
    """Estimated cost in dollars for one day/model bucket; None if the model
    has no published rate (the page falls back to its own estimate)."""
    r = _rates_for(day, model)
    if r is None:
        return None
    inp, out = r
    cost = (
        uncached * inp
        + cache_read * inp * _cfg["cache_read_multiplier"]
        + cc_5m * inp * _cfg["cache_write_5m_multiplier"]
        + cc_1h * inp * _cfg["cache_write_1h_multiplier"]
        + output * out
    ) / 1e6
    return round(cost, 6)


def stamp_bucket(bucket):
    """Add est_cost to every result in a day bucket that lacks one."""
    day = bucket["starting_at"][:10]
    for r in bucket.get("results", []):
        if r.get("est_cost") is None:
            cc = r.get("cache_creation") or {}
            r["est_cost"] = cost_for(
                day,
                r.get("model") or "unknown",
                r.get("uncached_input_tokens") or 0,
                r.get("cache_read_input_tokens") or 0,
                cc.get("ephemeral_5m_input_tokens") or 0,
                cc.get("ephemeral_1h_input_tokens") or 0,
                r.get("output_tokens") or 0,
            )
    return bucket
