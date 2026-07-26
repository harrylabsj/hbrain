#!/bin/zsh
# Daily automation wrapper — delegates to the shared reliability runner.
# All paths are absolute and configurable via environment; no secrets are
# sourced or printed. Extra args (e.g. --dry-run --state-dir /tmp/x) pass through.
set -euo pipefail

export PATH="/Users/jianghaidong/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

: "${SECOND_BRAIN_REPO:=/Users/jianghaidong/hbrain/llm-wiki}"
: "${HBRAIN_LOOP:=/Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py}"
: "${GBRAIN_BIN:=/Users/jianghaidong/.bun/bin/gbrain}"
: "${AUTOMATION_STATE_DIR:=/Users/jianghaidong/.hermes/state/hbrain-automation}"
: "${AUTOMATION_RUNNER:=/Users/jianghaidong/hbrain/haidong-os/automation/runner/automation_runner.py}"
: "${EXPERIENCE_REVIEW:=/Users/jianghaidong/hbrain/haidong-os/automation/experience_review.py}"
: "${FIVE_DOMAIN_DAILY:=/Users/jianghaidong/hbrain/haidong-os/automation/five_domain_daily.py}"
: "${FACTS_ROOT:=/Users/jianghaidong/hbrain/facts}"
: "${PROJECTS_ROOT:=/Users/jianghaidong/hbrain/haidong-os/projects}"
: "${RECEIPTS_ROOT:=/Users/jianghaidong/hbrain/haidong-os/receipts}"
: "${EXPERIENCE_INBOX_ROOT:=/Users/jianghaidong/hbrain/haidong-os/experience-review}"

python3 "$AUTOMATION_RUNNER" --mode daily \
  --state-dir "$AUTOMATION_STATE_DIR" \
  --hbrain-loop "$HBRAIN_LOOP" \
  --repo "$SECOND_BRAIN_REPO" \
  --gbrain "$GBRAIN_BIN" \
  --experience-review "$EXPERIENCE_REVIEW" \
  --five-domain-daily "$FIVE_DOMAIN_DAILY" \
  --facts-root "$FACTS_ROOT" \
  --projects-root "$PROJECTS_ROOT" \
  --receipts-root "$RECEIPTS_ROOT" \
  --experience-inbox-root "$EXPERIENCE_INBOX_ROOT" \
  "$@"
