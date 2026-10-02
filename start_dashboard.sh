#!/bin/zsh
# Read-only dashboard on 127.0.0.1:8050. Does not start or stop the bot.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  print -r -- "Missing $PY. Run ./run_local.sh once so the virtualenv exists, then start the dashboard again."
  exit 1
fi

exec "$PY" -m ops.dashboard
