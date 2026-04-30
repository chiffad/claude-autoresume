# claude-autoresume

A macOS daemon that automatically resumes Claude Code sessions after rate limits expire, using Claude Code's [channels](https://code.claude.com/docs/en/channels) to push a resume prompt into the running session.

## What it does

1. **Watches** Claude Code's JSONL log files (`~/.claude/projects/`) for rate-limit errors
2. **Notifies** you via macOS notification when a rate limit is hit, showing the reset time
3. **Resumes** the session 90 seconds after the limit resets by POSTing a resume prompt to the autoresume channel server, which pushes it natively into the running Claude Code session

## Setup

```bash
./setup.sh
```

This installs everything in one step:
- Channel server npm dependencies
- `autoresume` MCP server entry in `~/.claude.json`
- LaunchAgent daemon (runs at login, restarts automatically)

Logs: `~/.local/share/claude-autoresume/`

After setup, restart Claude Code with the channel:

```bash
claude --dangerously-load-development-channels server:autoresume
```

> **Troubleshooting:** If you get `server:autoresume · no MCP server configured with that name`, re-run `./setup.sh` and verify the `autoresume` entry exists in `~/.claude.json`.

## Testing

### Unit tests

```bash
python3 -m pytest tests/test_daemon.py -v
```

## Uninstall

```bash
./uninstall.sh
```

## Requirements

- macOS
- Python 3.9+
- Claude Code CLI v2.1.80+
- Node.js 18+
