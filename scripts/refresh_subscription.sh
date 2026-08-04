#!/bin/bash
# Rebuild data/subscription_usage.json from local Claude Code transcripts
# and push it so GitHub Pages picks it up. Run manually or via launchd
# (com.pranav.claude-usage-refresh).
set -euo pipefail
cd "$(dirname "$0")/.."

python3 scripts/build_subscription_usage.py

git pull --rebase --quiet origin main
if git diff --quiet -- data/subscription_usage.json; then
  echo "No changes to commit"
  exit 0
fi

git add data/subscription_usage.json
git commit -m "chore: update Claude Code usage data $(date +%Y-%m-%d)"
git push origin main
