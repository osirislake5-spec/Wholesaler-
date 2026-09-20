#!/usr/bin/env bash
# Wrapper so cron/systemd get a clean venv + env vars + a dated log file.
# Usage: set REPO_DIR below (or export it) to wherever this repo is cloned.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_DIR"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -d .venv ]; then
  source .venv/bin/activate
fi

mkdir -p logs
python3 satx_daily_pull.py >> "logs/$(date +%F).log" 2>&1
