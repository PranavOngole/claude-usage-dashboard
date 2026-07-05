import os
import json
import sys
import requests
from datetime import datetime, timedelta, timezone

API_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"
DAYS = 31


def fetch_all(admin_key: str) -> list:
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = now - timedelta(days=DAYS)

    headers = {
        "anthropic-version": "2023-06-01",
        "x-api-key": admin_key,
    }
    base_params = [
        ("starting_at", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("ending_at",   now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("bucket_width", "1d"),
        ("limit", str(DAYS)),
        ("group_by[]", "model"),
    ]

    buckets = []
    page = None

    while True:
        params = list(base_params)
        if page:
            params.append(("page", page))

        resp = requests.get(API_URL, headers=headers, params=params, timeout=30)

        if resp.status_code == 403:
            print("ERROR: 403 Forbidden. Check that you are using an Admin API key "
                  "(sk-ant-admin01-...) and that your account is an Organization, "
                  "not an individual account.", file=sys.stderr)
            sys.exit(1)

        resp.raise_for_status()
        body = resp.json()
        buckets.extend(body.get("data", []))

        if not body.get("has_more"):
            break
        page = body.get("next_page")

    return buckets


def main():
    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if not admin_key:
        print("ERROR: ANTHROPIC_ADMIN_KEY env var is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching last {DAYS} days of usage...")
    buckets = fetch_all(admin_key)

    total_results = sum(len(b.get("results", [])) for b in buckets)
    print(f"Got {len(buckets)} time buckets, {total_results} usage records")

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": buckets,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/usage.json", "w") as f:
        json.dump(output, f)

    print("Wrote data/usage.json")


if __name__ == "__main__":
    main()
