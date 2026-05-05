# claude-autoresume

A macOS daemon that automatically resumes Claude Code sessions after rate limits expire, using Claude Code's [channels](https://code.claude.com/docs/en/channels) to push a resume prompt into the running session.

## What it does

1. **Watches** Claude Code's JSONL log files (`~/.claude/projects/`) for rate-limit errors
2. **Dismisses** the interactive "What do you want to do?" prompt automatically (selects "Stop and wait for limit to reset")
3. **Notifies** you via macOS notification when a rate limit is hit, showing the reset time
4. **Resumes** the session 90 seconds after the limit resets by POSTing a resume prompt to the autoresume channel server, which pushes it natively into the running Claude Code session
5. **Verifies** the resume by watching for new assistant activity in the JSONL log; retries up to 5 times if needed, then notifies you if it gives up
6. **Recovers stale sessions** on startup: if Claude was rate-limited before the daemon was running, it detects this and resumes automatically (as long as the Claude process is still active)

## Setup

```bash
./setup.sh
```

This installs everything in one step:
- Channel server npm dependencies
- `autoresume` MCP server entry in `~/.claude.json`
- LaunchAgent daemon (runs at login, restarts automatically)

Logs and auth token: `~/.local/share/claude-autoresume/`

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

## Prompt dismissal

When Claude Code hits a rate limit, it may show an interactive prompt asking you to choose between "Stop and wait" or "Request more". The daemon auto-dismisses this by sending Enter (selecting option 1). Two methods are tried in order:

| Method | When used | Tradeoffs |
|--------|-----------|-----------|
| **tmux** `send-keys` | Claude is running inside tmux | Works in background, no extra permissions, supports multiple sessions |
| **Terminal.app** AppleScript | Fallback when tmux is unavailable | Briefly brings the tab to front, requires Accessibility permissions for System Events |

No configuration needed — the daemon auto-detects the environment. Both methods match by working directory, so multiple Claude sessions are handled correctly.

**tmux** is recommended for unattended use. Claude Code has native `--tmux` support:

```bash
claude --tmux --dangerously-load-development-channels server:autoresume
```

**Terminal.app** works out of the box but requires granting Accessibility access to Terminal.app (System Settings → Privacy & Security → Accessibility).

### Known limitations

- **Assumes option 1 is pre-selected.** The daemon sends Enter without navigating the menu, relying on "Stop and wait" being the default selection. If Claude Code changes the prompt ordering or default, the wrong option may be selected silently.
- **Timing gap.** The JSONL rate-limit entry may appear before the interactive prompt renders, so the keystroke can arrive too early and be a no-op. The daemon retries up to 3 times across poll cycles (~30 s total), which provides reasonable tolerance but doesn't guarantee the prompt is visible.
- **Terminal.app method is inherently fragile.** It briefly steals window focus, uses a hardcoded 0.3 s delay before firing `key code 36`, and sends the keystroke to whatever has focus at that instant — a race condition if another window grabs focus in the gap. It also only works with Terminal.app; iTerm2, Ghostty, and other terminal emulators are not supported. Use tmux to avoid all of this.

## Uninstall

```bash
./uninstall.sh
```

## Requirements

- macOS
- Python 3.9+
- Claude Code CLI v2.1.80+
- Node.js 18+
