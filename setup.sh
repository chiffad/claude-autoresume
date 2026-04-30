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

# Require Python 3.9+ (zoneinfo module)
if ! "$PYTHON3" -c 'import sys; exit(0 if sys.version_info >= (3, 9) else 1)'; then
  PY_VERSION="$("$PYTHON3" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "Error: Python 3.9+ required, found $PY_VERSION" >&2
  exit 1
fi

if ! command -v node &>/dev/null; then
  echo "Error: node not found in PATH" >&2
  exit 1
fi

# Require Node.js 18+
NODE_MAJOR="$(node -e 'console.log(process.versions.node.split(".")[0])')"
if [ "$NODE_MAJOR" -lt 18 ]; then
  echo "Error: Node.js 18+ required, found $(node --version)" >&2
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

# --- 2. Generate auth token ---

AUTH_TOKEN_FILE="$LOG_DIR/auth-token"
mkdir -p "$LOG_DIR"
if [ ! -f "$AUTH_TOKEN_FILE" ]; then
  python3 -c "import secrets; print(secrets.token_hex(32))" > "$AUTH_TOKEN_FILE"
  chmod 600 "$AUTH_TOKEN_FILE"
  echo "Generated auth token: $AUTH_TOKEN_FILE"
else
  echo "Auth token already exists: $AUTH_TOKEN_FILE"
fi

# --- 3. Register MCP server in ~/.claude.json ---

echo "Configuring MCP server..."
CLAUDE_CONFIG="$CLAUDE_CONFIG" CHANNEL_SCRIPT="$CHANNEL_SCRIPT" python3 -c "
import json, os, sys, tempfile

path = os.environ['CLAUDE_CONFIG']
server_script = os.environ['CHANNEL_SCRIPT']

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
    # Write to a temp file and atomically rename to avoid corruption
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(config, f, indent=2)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
    print('  Added autoresume MCP server to ' + path)
"

# --- 4. Install and start the daemon ---

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
