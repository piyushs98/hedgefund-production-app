#!/bin/zsh
# Paper bot for launchd. No Terminal window, no caffeinate, and no tee.
#
# The Nov 6 mid-session restart was gunicorn handling WINCH when the
# Terminal window changed size. This script execs gunicorn with logs on
# a file, so there is no controlling terminal.
#
# Do not bootstrap com.hedgefund.paperbot while the current gunicorn is
# still listening, and bootout the old com.hedgefund.bot watcher first.
# launchd cannot read ~/Desktop until macOS Full Disk Access is granted
# to the service that runs this script. caffeinate does not cover lid-close
# sleep. The AC sleep commands are an operator step, not this script.
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
exec >> "$LOG" 2>&1

print -r -- "----- $(TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z') detached start -----"

if [[ ! -f "$ROOT/.env" ]]; then
  print -r -- "held" > "$SUPPORT/hold"
  print -r -- "Missing $ROOT/.env. Copy GEMINI_API_KEY, DEEPSEEK_API_KEY, and DISCORD_WEBHOOK from Render."
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
  print -r -- "Refusing to start. Empty in .env: ${missing[*]}"
  exit 78
fi
rm -f "$SUPPORT/hold"
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  print -r -- "WARNING: DEEPSEEK_API_KEY is empty. The morning brief still uses Gemini. The midday note and the DeepSeek backup will fail."
fi

flag="${RESET_LEDGER_ON_BOOT:-}"
flag="${flag:l}"
if [[ "$flag" == "1" || "$flag" == "true" || "$flag" == "yes" || "$flag" == "on" ]]; then
  print -r -- "WARNING: RESET_LEDGER_ON_BOOT is true. This start wipes the ledger and open positions and reseeds buying power at ACCOUNT_SIZE."
fi

if [[ ! -x "$ROOT/.venv/bin/gunicorn" ]]; then
  print -r -- "Missing $ROOT/.venv/bin/gunicorn. Create the venv from requirements.txt before launchd starts this."
  exit 78
fi

PIDFILE="$SUPPORT/bot.pid"
if [[ -f "$PIDFILE" ]]; then
  old="$(<"$PIDFILE")"
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    print -r -- "Bot already running (pid $old). Not starting a second worker."
    exit 1
  fi
fi
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  print -r -- "127.0.0.1:${PORT} is already listening. Not starting a second worker."
  exit 1
fi

print -r -- $$ > "$PIDFILE"
print -r -- "exec gunicorn on 127.0.0.1:${PORT} workers=1 threads=8 timeout=120"

exec "$ROOT/.venv/bin/gunicorn" main:app \
  --workers 1 \
  --threads 8 \
  --bind "127.0.0.1:${PORT}" \
  --timeout 120
