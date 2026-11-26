#!/bin/zsh
# Executor only. Does not start or stop the paper bot.
# EXECUTION_MODE defaults to sandbox when .env does not set it.
# The kill switch writes EXECUTION_MODE=off and that value wins on restart.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
export PYTHONUNBUFFERED=1
mkdir -p "$ROOT/logs" "$ROOT/data"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

if [[ -z "${EXECUTION_MODE:-}" ]]; then
  export EXECUTION_MODE=sandbox
fi
unset LIVE_TRADING_ENABLED

exec "$ROOT/.venv-webull/bin/python" -m webull_exec
