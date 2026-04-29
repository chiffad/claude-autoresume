#!/bin/bash
set -euo pipefail

DAEMON_SRC="$(cd "$(dirname "$0")" && pwd)/daemon.py"
DAEMON_DEST="$HOME/.local/bin/claude-autoresume-daemon"
PLIST_LABEL="com.user.claude-autoresume"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_DIR="$HOME/.local/share/claude-autoresume"
PYTHON3="$(which python3)"

if [ -z "$PYTHON3" ]; then
  echo "Error: python3 not found in PATH" >&2
  exit 1
fi

# Create directories
mkdir -p "$(dirname "$DAEMON_DEST")"
mkdir -p "$LOG_DIR"

# Install daemon
cp "$DAEMON_SRC" "$DAEMON_DEST"
chmod +x "$DAEMON_DEST"

# Expand DAEMON_DEST to absolute path for the plist
DAEMON_DEST_ABS="$DAEMON_DEST"

# Generate LaunchAgent plist
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON3}</string>
    <string>${DAEMON_DEST_ABS}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/daemon.err</string>
</dict>
</plist>
PLIST

# Load the agent
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo "claude-autoresume installed and running."
echo "  Daemon:  $DAEMON_DEST"
echo "  Plist:   $PLIST_PATH"
echo "  Logs:    $LOG_DIR/"
