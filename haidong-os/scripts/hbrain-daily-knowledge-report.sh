#!/usr/bin/env bash
set -euo pipefail

growth_script="${HBRAIN_KNOWLEDGE_GROWTH:-/Users/jianghaidong/hbrain/haidong-os/automation/knowledge_growth.py}"
exec python3 "$growth_script" daily-report
