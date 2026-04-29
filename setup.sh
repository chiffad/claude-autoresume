#!/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CHANNEL_DIR="$REPO_DIR/channel"
CHANNEL_SCRIPT="$CHANNEL_DIR/autoresume.mjs"
CLAUDE_CONFIG="$HOME/.claude.json"
DAEMON_SRC="$REPO_DIR/daemon.py"
DAEMON_DEST="$HOME/.local/bin/claude-autoresume-daemon"
PLIST_LABEL="com.user.claude-autoresume"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_DIR="$HOME/.local/share/claude-autoresume"

# --- Preflight checks ---

if ! command -v python3 &>/dev/null; then
  echo "Error: python3 not found in PATH" >&2
  exit 1
fi
PYTHON3="$(command -v python3)"

if ! command -v node &>/dev/null; then
  echo "Error: node not found in PATH" >&2
  exit 1
fi

if ! command -v npm &>/dev/null; then
  echo "Error: npm not found in PATH" >&2
  exit 1
fi

if [ ! -f "$CLAUDE_CONFIG" ]; then
  echo "Error: $CLAUDE_CONFIG not found — run Claude Code at least once first" >&2
  exit 1
fi

# --- 1. Install channel server dependencies ---

echo "Installing channel server dependencies..."
(cd "$CHANNEL_DIR" && npm install --silent)

# --- 2. Register MCP server in ~/.claude.json ---

echo "Configuring MCP server..."
python3 -c "
import json, sys

path = '$CLAUDE_CONFIG'
server_script = '$CHANNEL_SCRIPT'

with open(path) as f:
    config = json.load(f)

servers = config.setdefault('mcpServers', {})

if 'autoresume' in servers:
    print('  autoresume MCP server already configured')
else:
    servers['autoresume'] = {
        'command': 'node',
        'args': [server_script],
    }
    with open(path, 'w') as f:
        json.dump(config, f, indent=2)
        f.write('\n')
    print('  Added autoresume MCP server to ' + path)
"

# --- 3. Install and start the daemon ---

echo "Installing daemon..."
mkdir -p "$(dirname "$DAEMON_DEST")"
mkdir -p "$LOG_DIR"

cp "$DAEMON_SRC" "$DAEMON_DEST"
chmod +x "$DAEMON_DEST"

# Unload existing agent if present
launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true

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
    <string>${DAEMON_DEST}</string>
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

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

# --- Done ---

echo ""
echo "claude-autoresume installed and running."
echo "  MCP server: autoresume (in $CLAUDE_CONFIG)"
echo "  Daemon:     $DAEMON_DEST"
echo "  Logs:       $LOG_DIR/"
echo ""
echo "Restart Claude Code with the channel:"
echo "  claude --dangerously-load-development-channels server:autoresume"
