#!/bin/bash
set -euo pipefail

PLIST_LABEL="com.user.claude-autoresume"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
DAEMON_DEST="$HOME/.local/bin/claude-autoresume-daemon"
LOG_DIR="$HOME/.local/share/claude-autoresume"

# Stop and unload the agent (ignore errors if not loaded)
launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true

# Remove installed files
rm -f "$PLIST_PATH"
rm -f "$DAEMON_DEST"
rm -rf "$LOG_DIR"

echo "claude-autoresume uninstalled."
echo "  Removed: $DAEMON_DEST"
echo "  Removed: $PLIST_PATH"
echo "  Removed: $LOG_DIR/"
