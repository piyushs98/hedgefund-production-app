#!/bin/zsh
# Start the paper bot the way Render does: one gunicorn worker on main:app.
# Loads .env, keeps the Mac awake while the process lives, and tees logs.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

export PYTHONUNBUFFERED=1
export PORT="${PORT:-10000}"

mkdir -p "$ROOT/logs"
SUPPORT="$HOME/Library/Application Support/hedgefund"
mkdir -p "$SUPPORT"
LOG_DATE="$(TZ=America/Chicago date +%F)"
LOG="$ROOT/logs/bot-${LOG_DATE}.log"

say() {
  print -r -- "$1" | tee -a "$LOG"
}

if [[ ! -f "$ROOT/.env" ]]; then
  print -r -- "held" > "$SUPPORT/hold"
  say "Missing $ROOT/.env. Copy GEMINI_API_KEY, DEEPSEEK_API_KEY, and DISCORD_WEBHOOK from Render."
  exit 78
fi

set -a
source "$ROOT/.env"
set +a

missing=()
[[ -z "${GEMINI_API_KEY:-}" ]] && missing+=(GEMINI_API_KEY)
[[ -z "${DISCORD_WEBHOOK:-}" ]] && missing+=(DISCORD_WEBHOOK)
if (( ${#missing} )); then
  print -r -- "held" > "$SUPPORT/hold"
  say "Refusing to start. Empty in .env: ${missing[*]}"
  exit 78
fi
rm -f "$SUPPORT/hold"
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  say "WARNING: DEEPSEEK_API_KEY is empty. The morning brief still uses Gemini. The midday note and the DeepSeek backup will fail."
fi

flag="${RESET_LEDGER_ON_BOOT:-}"
flag="${flag:l}"
if [[ "$flag" == "1" || "$flag" == "true" || "$flag" == "yes" || "$flag" == "on" ]]; then
  say "WARNING: RESET_LEDGER_ON_BOOT is true. This start wipes the ledger and open positions and reseeds buying power at ACCOUNT_SIZE."
fi

if [[ ! -x "$ROOT/.venv/bin/gunicorn" ]]; then
  say "Creating .venv and installing requirements.txt"
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

PIDFILE="$SUPPORT/bot.pid"
if [[ -f "$PIDFILE" ]]; then
  old="$(<"$PIDFILE")"
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    say "Bot already running (pid $old). Not starting a second worker."
    exit 0
  fi
fi
print -r -- $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

say "----- $(TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z') starting one worker on 127.0.0.1:${PORT} -----"

# pipefail is set, so a gunicorn crash is the script's exit status.
caffeinate -ims "$ROOT/.venv/bin/gunicorn" main:app \
  --workers 1 \
  --threads 8 \
  --bind "127.0.0.1:${PORT}" \
  --timeout 120 \
  2>&1 | tee -a "$LOG"
