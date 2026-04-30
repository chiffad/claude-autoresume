#!/bin/bash
set -euo pipefail

PLIST_LABEL="com.user.claude-autoresume"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
DAEMON_DEST="$HOME/.local/bin/claude-autoresume-daemon"
LOG_DIR="$HOME/.local/share/claude-autoresume"

CLAUDE_CONFIG="$HOME/.claude.json"

echo "This will remove:"
echo "  $DAEMON_DEST"
echo "  $PLIST_PATH"
echo "  $LOG_DIR/ (logs and auth token)"
echo "  autoresume entry from $CLAUDE_CONFIG (if present)"
echo ""
read -r -p "Continue? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

# Stop and unload the agent (ignore errors if not loaded)
launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true

# Remove installed files
rm -f "$PLIST_PATH"
rm -f "$DAEMON_DEST"
rm -rf "$LOG_DIR"

# Remove MCP server entry from ~/.claude.json
if [ -f "$CLAUDE_CONFIG" ]; then
  python3 -c "
import json, sys, os, tempfile
p = os.path.expanduser('$CLAUDE_CONFIG')
with open(p) as f:
    cfg = json.load(f)
if 'mcpServers' in cfg and 'autoresume' in cfg['mcpServers']:
    del cfg['mcpServers']['autoresume']
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(cfg, f, indent=2)
            f.write('\n')
        os.replace(tmp, p)
        print('Removed autoresume from $CLAUDE_CONFIG')
    except Exception:
        os.unlink(tmp)
        raise
else:
    print('No autoresume entry in $CLAUDE_CONFIG — skipping')
"
fi

echo "claude-autoresume uninstalled."
