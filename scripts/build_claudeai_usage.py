"""Estimate claude.ai chat token usage from the official data export.

Anthropic exposes no usage API for claude.ai chats, so this reads the
account export zip (claude.ai Settings > Privacy > Export data, arrives
by email) and estimates tokens from message text at ~4 characters per
token. Input context is replayed cumulatively per conversation with the
same cache model the API uses: content sent once bills as a 5-minute
cache write, re-sent context as cache reads. Thinking tokens, system
prompts, attachments, and artifacts are not in the export, so this is a
floor, not an exact figure.

Usage:
    python3 build_claudeai_usage.py [path-to-export.zip-or-conversations.json]

With no argument, the newest ~/Downloads/data-*.zip is used. If no
export is found the script exits 0 without touching the output, so the
nightly job keeps whatever was built last.
"""

import io
import json
import sys
import zipfile

import pricing
from collections import defaultdict
from datetime import datetime
from pathlib import Path

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claudeai_usage.json"
CHARS_PER_TOKEN = 4

# Model slugs in the export that map onto published API pricing keep their
# name; anything else (or missing) folds into the assumed-rate pseudo-model.
KNOWN_PREFIXES = (
    "claude-fable-5", "claude-mythos", "claude-opus", "claude-sonnet", "claude-haiku",
)
FALLBACK_MODEL = "claude-ai-chat"


def est_tokens(text):
    return max(1, round(len(text) / CHARS_PER_TOKEN)) if text else 0


def msg_text(msg):
    parts = []
    if msg.get("text"):
        parts.append(msg["text"])
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("text"):
            parts.append(block["text"])
    return "\n".join(parts)


def find_export():
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser()
    candidates = sorted(
        Path.home().glob("Downloads/data-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_conversations(path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
            if not name:
                raise ValueError(f"{path} has no conversations.json")
            with z.open(name) as f:
                return json.load(io.TextIOWrapper(f, encoding="utf-8"))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_when(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError, TypeError):
        return None


def resolve_model(conv):
    slug = (conv.get("model") or "").strip()
    if slug.startswith(KNOWN_PREFIXES):
        return slug
    return FALLBACK_MODEL


def main():
    export = find_export()
    if not export or not export.exists():
        print("No claude.ai export found (looked for ~/Downloads/data-*.zip); skipping.")
        return

    conversations = load_conversations(export)
    days = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    counted = 0

    for conv in conversations:
        model = resolve_model(conv)
        msgs = sorted(conv.get("chat_messages") or [], key=lambda m: m.get("created_at") or "")

        cached_context = 0   # tokens already sent in a prior request of this conversation
        pending_new = 0      # tokens accumulated since the last assistant reply

        for msg in msgs:
            tokens = est_tokens(msg_text(msg))
            if msg.get("sender") != "assistant":
                pending_new += tokens
                continue

            when = parse_when(msg.get("created_at"))
            if when:
                bucket = days[when.date().isoformat()][model]
                bucket["cache_read"] += cached_context
                bucket["cache_write"] += pending_new
                bucket["output"] += tokens
                counted += 1
            cached_context += pending_new + tokens
            pending_new = 0

    data = []
    for day in sorted(days):
        results = []
        for model in sorted(days[day]):
            b = days[day][model]
            results.append({
                "model": model,
                "uncached_input_tokens": 0,
                "cache_read_input_tokens": b["cache_read"],
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 0,
                    "ephemeral_5m_input_tokens": b["cache_write"],
                },
                "output_tokens": b["output"],
            })
        data.append(pricing.stamp_bucket({"starting_at": f"{day}T00:00:00Z", "results": results}))

    output = {
        "fetched_at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "claude-ai-export",
        "export_name": export.name,
        "estimated": True,
        "data": data,
    }

    # Skip the write when nothing changed besides the timestamp, so the
    # nightly job does not create no-op commits.
    if OUTPUT_PATH.exists():
        try:
            existing = json.load(open(OUTPUT_PATH, encoding="utf-8"))
            if existing.get("data") == data and existing.get("export_name") == export.name:
                print(f"Unchanged since last build of {export.name}; skipping write.")
                return
        except (json.JSONDecodeError, OSError):
            pass

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f)
    print(f"Estimated {counted} assistant replies into {len(data)} days from {export.name} -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
