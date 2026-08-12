#!/bin/bash
# Rebuild the dashboard's local data files and push any changes.
# Invoked by refresh_subscription.sh after the repo is up to date.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 scripts/build_subscription_usage.py
python3 scripts/build_claudeai_usage.py
python3 scripts/build_codex_usage.py

if git diff --quiet -- data/subscription_usage.json data/claudeai_usage.json data/codex_usage.json \
   && [ -z "$(git status --porcelain -- data/claudeai_usage.json data/codex_usage.json)" ]; then
  echo "No changes to commit"
  exit 0
fi

git add data/subscription_usage.json data/claudeai_usage.json data/codex_usage.json
git commit -m "chore: update usage data $(date +%Y-%m-%d)"
git push origin main
