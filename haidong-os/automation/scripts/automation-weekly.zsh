#!/bin/zsh
# Weekly automation wrapper — delegates to the shared reliability runner.
# All paths are absolute and configurable via environment; no secrets are
# sourced or printed. Extra args (e.g. --dry-run --state-dir /tmp/x) pass through.
set -euo pipefail

export PATH="/Users/jianghaidong/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

: "${SECOND_BRAIN_REPO:=/Users/jianghaidong/hbrain/llm-wiki}"
: "${HBRAIN_LOOP:=/Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py}"
: "${GBRAIN_BIN:=/Users/jianghaidong/.bun/bin/gbrain}"
: "${AUTOMATION_STATE_DIR:=/Users/jianghaidong/.hermes/state/hbrain-automation}"
: "${AUTOMATION_RUNNER:=/Users/jianghaidong/hbrain/haidong-os/automation/runner/automation_runner.py}"

python3 "$AUTOMATION_RUNNER" --mode weekly \
  --state-dir "$AUTOMATION_STATE_DIR" \
  --hbrain-loop "$HBRAIN_LOOP" \
  --repo "$SECOND_BRAIN_REPO" \
  --gbrain "$GBRAIN_BIN" \
  "$@"
