#!/usr/bin/env bash
# Run the DeepSeek automation against the chromium-agent profile.
# Prereq: ./launch.sh, then log into chat.deepseek.com once and leave it open.
set -euo pipefail
cd "$(dirname "$0")"
export BU_CDP_URL="${BU_CDP_URL:-http://127.0.0.1:9222}"
exec browser-harness < deepseek_chat.py
