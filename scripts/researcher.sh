#!/bin/sh
# Researcher container entrypoint.
#   scripts/researcher.sh         install the pinned pi-claude-bridge once, then run the researcher service
#   scripts/researcher.sh login   one-time Claude Pro login, kept in the researcher-home volume
set -eu

if [ "${1:-}" = "login" ]; then
  exec claude auth login
fi

mkdir -p "$PI_CODING_AGENT_DIR"
marker="$PI_CODING_AGENT_DIR/.pi-claude-bridge-$PI_CLAUDE_BRIDGE_VERSION"
if [ ! -f "$marker" ]; then
  pi install "npm:pi-claude-bridge@$PI_CLAUDE_BRIDGE_VERSION"
  touch "$marker"
fi

exec python -m loop.researcher --substrate http://substrate:7070 \
  --interval-min "$RESEARCHER_INTERVAL_MIN" --cap-pct "$RESEARCHER_CAP_PCT" \
  --provider claude-bridge --model "$RESEARCHER_MODEL" \
  --agent-dir "$PI_CODING_AGENT_DIR" --status-out /status/researcher.json
