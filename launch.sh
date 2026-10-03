#!/usr/bin/env bash
# Launch the chromium-agent profile with CDP so the automation can attach.
# Run once, log into chat.deepseek.com, then leave it running.
set -euo pipefail
PROFILE="${CHROMIUM_AGENT_PROFILE:-$HOME/.config/chromium-agent}"
PORT="${CDP_PORT:-9222}"
if pgrep -f "user-data-dir=$PROFILE" >/dev/null; then
  echo "already running (profile=$PROFILE port=$PORT)"
  exit 0
fi
[ $# -eq 0 ] && set -- about:blank
exec chromium --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
  --remote-allow-origins='*' --no-first-run --no-default-browser-check "$@"
