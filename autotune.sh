#!/bin/sh
# Runs optimizer.py against a fresh checkout of this repo, and if it
# recommends changes, pushes them to a new branch and opens a PR for
# review -- unlike lowfloat_trader's autotune.sh, this does NOT push
# directly to main. tradingbot's tunable set includes real risk-adjacent
# parameters (see bot/tuning_limits.py's docstring for what's deliberately
# excluded even so), higher stakes than lowfloat's entry-filter-only params,
# so every change goes through the same review any other PR would.
set -eu

REPO_DIR=/tmp/repo
REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/parthjp80/tradingbot"
BRANCH="autotune/$(date -u +%Y-%m-%d)"

rm -rf "$REPO_DIR"
git clone --depth 1 "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"
git config user.name "tradingbot-optimizer-bot"
git config user.email "optimizer-bot@users.noreply.github.com"
git checkout -b "$BRANCH"

export OPTIMIZER_JOURNAL_FILE=/app/data/trade_journal.csv
export OPTIMIZER_OVERRIDES_FILE="$REPO_DIR/config/tuned_overrides.json"
export OPTIMIZER_PR_BODY_FILE="$REPO_DIR/pr_body.md"

cd /app
python3 optimizer.py

cd "$REPO_DIR"
rm -f config/tuned_overrides.json.bak.*

if git diff --quiet -- config/tuned_overrides.json; then
  echo "No config changes to propose."
  exit 0
fi

git add config/tuned_overrides.json
git commit -m "Auto-tune trading parameters ($(date -u +%Y-%m-%d))"
git push origin "$BRANCH"

gh pr create \
  --title "Auto-tune: $(date -u +%Y-%m-%d)" \
  --body-file pr_body.md \
  --base main \
  --head "$BRANCH"

echo "Opened PR for review."
