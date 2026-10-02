#!/bin/zsh
# Double-click or login entry. Terminal can read this folder; launchd cannot.
cd "$(dirname "$0")"
exec ./run_local.sh
