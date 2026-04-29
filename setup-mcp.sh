#!/bin/bash
set -euo pipefail

CHANNEL_SCRIPT="$(cd "$(dirname "$0")" && pwd)/channel/autoresume.mjs"
CLAUDE_CONFIG="$HOME/.claude.json"

if [ ! -f "$CHANNEL_SCRIPT" ]; then
  echo "Error: $CHANNEL_SCRIPT not found" >&2
  exit 1
fi

if [ ! -f "$CLAUDE_CONFIG" ]; then
  echo "Error: $CLAUDE_CONFIG not found — run Claude Code at least once first" >&2
  exit 1
fi

python3 -c "
import json, sys

path = '$CLAUDE_CONFIG'
server_script = '$CHANNEL_SCRIPT'

with open(path) as f:
    config = json.load(f)

servers = config.setdefault('mcpServers', {})

if 'autoresume' in servers:
    print('autoresume MCP server already configured in ' + path)
    sys.exit(0)

servers['autoresume'] = {
    'command': 'node',
    'args': [server_script],
}

with open(path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

print('Added autoresume MCP server to ' + path)
print('  command: node')
print('  args: [' + server_script + ']')
print()
print('Start Claude Code with:')
print('  claude --dangerously-load-development-channels server:autoresume')
"
