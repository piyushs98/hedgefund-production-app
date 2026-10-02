#!/bin/zsh
# Runs outside ~/Desktop so launchd is allowed to read it.
# If the bot is down and not on hold, open it in Terminal, which can
# read the project folder. A stop file means stay down.
set -u

SUPPORT="$HOME/Library/Application Support/hedgefund"
COMMAND="/Users/markanthiny/Desktop/hedgefund-production-app/Start Bot.command"
PIDFILE="$SUPPORT/bot.pid"

if [[ -f "$SUPPORT/stop" || -f "$SUPPORT/hold" ]]; then
  exit 0
fi

if [[ -f "$PIDFILE" ]]; then
  pid="$(<"$PIDFILE")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    exit 0
  fi
fi

open -a Terminal "$COMMAND"
exit 0
