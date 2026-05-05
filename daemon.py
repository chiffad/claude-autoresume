#!/usr/bin/env python3
"""macOS daemon that auto-resumes Claude Code sessions after rate limits."""

import glob
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo


def is_rate_limit_entry(line: str) -> bool:
    """Return True when the JSONL line represents a rate-limit API error."""
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return False
    return entry.get("error") == "rate_limit" and entry.get("isApiErrorMessage") is True


def extract_rate_limit_info(line: str) -> dict:
    """Extract session_id, cwd, reset_text, and timestamp from a rate-limit JSONL line."""
    entry = json.loads(line)
    text = entry["message"]["content"][0]["text"]
    match = re.search(r"resets (.+)", text)
    return {
        "session_id": entry["sessionId"],
        "cwd": entry["cwd"],
        "reset_text": match.group(1) if match else None,
        "timestamp": entry["timestamp"],
    }


def parse_reset_time(reset_text: str, now: Optional[datetime] = None) -> datetime:
    """Parse a reset-time string like '7pm (Europe/Paris)' into an aware datetime.

    If the parsed time is at or before `now`, advance by one day.
    """
    match = re.match(r"(\d+)(?::(\d+))?(am|pm)\s+\((.+?)\)", reset_text)
    if not match:
        raise ValueError(f"Cannot parse reset time: {reset_text!r}")

    hour = int(match.group(1))
    minutes = int(match.group(2)) if match.group(2) else 0
    ampm = match.group(3)
    tz_name = match.group(4)

    # 12h → 24h conversion
    if ampm == "am":
        hour = 0 if hour == 12 else hour
    else:  # pm
        hour = hour if hour == 12 else hour + 12

    tz = ZoneInfo(tz_name)
    if now is None:
        now = datetime.now(tz)

    reset = now.astimezone(tz).replace(hour=hour, minute=minutes, second=0, microsecond=0)

    if reset <= now.astimezone(tz):
        tomorrow = reset.date() + timedelta(days=1)
        reset = reset.replace(year=tomorrow.year, month=tomorrow.month, day=tomorrow.day)

    return reset


class FileWatcher:
    """Poll a file for new lines appended since last scan.

    Tracks a byte offset so each scan only reads newly-appended content.
    """

    def __init__(
        self,
        path: str,
        callback: Callable[[str], None],
        skip_existing: bool = False,
    ) -> None:
        self._path = path
        self._callback = callback
        self._offset: int = 0

        if skip_existing:
            try:
                with open(path, "r") as f:
                    f.seek(0, 2)
                    self._offset = f.tell()
            except FileNotFoundError:
                pass

    def scan(self) -> None:
        """Read new lines from the file and invoke callback for each."""
        try:
            with open(self._path, "r") as f:
                f.seek(0, 2)
                size = f.tell()
                if size < self._offset:
                    self._offset = 0
                f.seek(self._offset)
                data = f.read()
                self._offset = f.tell()
        except FileNotFoundError:
            return

        for line in data.splitlines():
            if line:
                self._callback(line)


RESUME_PROMPT = "continue the task from where it was interrupted by the usage limit"
CHANNEL_PORT = int(os.environ.get("AUTORESUME_PORT", "18963"))
AUTH_TOKEN_PATH = Path(
    os.environ.get(
        "AUTORESUME_TOKEN_FILE",
        Path.home() / ".local" / "share" / "claude-autoresume" / "auth-token",
    )
)


def _load_auth_token() -> Optional[str]:
    try:
        return AUTH_TOKEN_PATH.read_text().strip()
    except FileNotFoundError:
        return None


def try_post_channel(prompt: str) -> bool:
    """POST the resume prompt to the autoresume channel server.

    Returns True on a 200 response, False on any error.
    Note: urllib raises HTTPError for non-2xx, so failures go through except.
    """
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{CHANNEL_PORT}",
            data=prompt.encode(),
            method="POST",
        )
        token = _load_auth_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as exc:
        log.debug("Channel POST failed: %s", exc)
        return False


def send_notification(title: str, body: str) -> None:
    """Send a macOS notification via osascript."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    result = subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    if result.returncode != 0:
        log.debug("osascript failed (rc=%d): %s", result.returncode, result.stderr.decode().strip())


# ---------------------------------------------------------------------------
# Prompt dismissal — auto-select "Stop and wait" on rate-limit prompt
# ---------------------------------------------------------------------------

MAX_PROMPT_DISMISS_ATTEMPTS = 3


def _dismiss_via_tmux(cwd: str) -> bool:
    """Send Enter to a tmux pane running claude in *cwd*."""
    try:
        result = subprocess.run(
            [
                "tmux", "list-panes", "-a", "-F",
                "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_current_path}",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pane_target, pane_cmd, pane_path = parts
            if "claude" in pane_cmd.lower() and os.path.abspath(pane_path) == os.path.abspath(cwd):
                subprocess.run(
                    ["tmux", "send-keys", "-t", pane_target, "Enter"],
                    check=True, timeout=5,
                )
                log.info("Dismissed prompt via tmux pane %s", pane_target)
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        log.debug("tmux dismiss failed: %s", exc)
    return False


def _find_claude_tty_for_cwd(cwd: str) -> Optional[str]:
    """Return the TTY device path of a Claude process whose cwd matches."""
    try:
        ps = subprocess.run(
            ["ps", "-eo", "pid,tty,command"],
            capture_output=True, text=True, timeout=10,
        )
        candidates = []
        for line in ps.stdout.splitlines()[1:]:
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid, tty, cmd = parts
            if "claude" in cmd and "autoresume" not in cmd and tty != "??":
                candidates.append((pid, tty))

        for pid, tty in candidates:
            lsof = subprocess.run(
                ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=10,
            )
            for lsof_line in lsof.stdout.splitlines():
                if lsof_line.startswith("n") and len(lsof_line) > 1:
                    proc_cwd = lsof_line[1:]
                    if os.path.abspath(proc_cwd) == os.path.abspath(cwd):
                        return f"/dev/{tty}" if not tty.startswith("/dev/") else tty
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("TTY lookup failed: %s", exc)
    return None


def _dismiss_via_terminal_app(cwd: str) -> bool:
    """Bring the Terminal.app tab running claude in *cwd* to front and send Enter."""
    target_tty = _find_claude_tty_for_cwd(cwd)
    if not target_tty:
        log.debug("No Claude TTY found for cwd=%s", cwd)
        return False

    safe_tty = target_tty.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''\
tell application "Terminal"
    repeat with w in every window
        repeat with t in every tab of w
            if tty of t is "{safe_tty}" then
                set frontmost to true
                set index of w to 1
                set selected tab of w to t
                delay 0.3
                tell application "System Events"
                    key code 36
                end tell
                return "ok"
            end if
        end repeat
    end repeat
end tell
return "not_found"
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip() == "ok":
            log.info("Dismissed prompt via Terminal.app (tty=%s)", target_tty)
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("Terminal.app dismiss failed: %s", exc)
    return False


def dismiss_interactive_prompt(cwd: str) -> bool:
    """Auto-dismiss the rate-limit "What do you want to do?" prompt.

    Tries tmux first (works in background), then Terminal.app AppleScript.
    """
    if _dismiss_via_tmux(cwd):
        return True
    if _dismiss_via_terminal_app(cwd):
        return True
    log.debug("Could not dismiss interactive prompt for cwd=%s", cwd)
    return False


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 10
RESUME_GRACE_SECONDS = 90
MAX_RESUME_ATTEMPTS = 5
UNKNOWN_RESET_FALLBACK_MINUTES = 5
STALE_SCAN_INTERVAL_SECONDS = 600
STALE_SCAN_MAX_AGE_SECONDS = 72 * 3600
VERIFY_WINDOW_SECONDS = 120

log = logging.getLogger(__name__)


@dataclass
class PendingResume:
    session_id: str
    cwd: str
    reset_at: datetime
    jsonl_path: Optional[str] = None
    notified: bool = False
    resume_attempts: int = 0
    resume_sent_at: Optional[datetime] = None
    prompt_dismissed: bool = False
    prompt_dismiss_attempts: int = 0


def make_rate_limit_handler(pending: List[PendingResume]) -> Callable[[str], None]:
    """Return a callback that filters rate-limit lines and appends to *pending*."""

    def _handler(line: str) -> None:
        if not is_rate_limit_entry(line):
            return
        try:
            info = extract_rate_limit_info(line)
        except (KeyError, IndexError, TypeError) as exc:
            log.warning("Malformed rate-limit entry — skipping: %s", exc)
            return
        # Deduplicate by session_id
        if any(p.session_id == info["session_id"] for p in pending):
            return
        if info["reset_text"]:
            try:
                reset_at = parse_reset_time(info["reset_text"])
            except ValueError:
                log.warning("Could not parse reset time %r — using fallback", info["reset_text"])
                reset_at = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=UNKNOWN_RESET_FALLBACK_MINUTES)
        else:
            log.info("No reset time in message — will retry in %d min", UNKNOWN_RESET_FALLBACK_MINUTES)
            reset_at = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=UNKNOWN_RESET_FALLBACK_MINUTES)
        pending.append(
            PendingResume(
                session_id=info["session_id"],
                cwd=info["cwd"],
                reset_at=reset_at,
            )
        )
        log.info(
            "Rate limit detected: session=%s resets at %s",
            info["session_id"],
            reset_at.isoformat(),
        )

    return _handler


def find_unresolved_rate_limits(jsonl_paths: List[str]) -> List[dict]:
    """Scan JSONL files for sessions stuck at a rate limit.

    A session is "stuck" if its last ``rate_limit`` entry has no subsequent
    ``assistant`` activity (queue-operation / user entries from a daemon
    resume attempt don't count).
    """
    results: List[dict] = []
    for path in jsonl_paths:
        try:
            with open(path) as f:
                lines = f.readlines()
        except (FileNotFoundError, PermissionError):
            continue

        last_rl_line: Optional[str] = None
        has_assistant_after = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if is_rate_limit_entry(stripped):
                last_rl_line = stripped
                has_assistant_after = False
            elif last_rl_line:
                try:
                    entry = json.loads(stripped)
                    if entry.get("type") == "assistant" and not entry.get("error"):
                        has_assistant_after = True
                except (json.JSONDecodeError, TypeError):
                    pass

        if last_rl_line and not has_assistant_after:
            try:
                info = extract_rate_limit_info(last_rl_line)
                info["jsonl_path"] = path
                results.append(info)
            except (KeyError, IndexError, TypeError):
                pass

    return results


def find_claude_session_cwds() -> set:
    """Return cwds of all running Claude CLI processes (two subprocess calls)."""
    try:
        ps = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10
        )
        pids = []
        for line in ps.stdout.splitlines():
            parts = line.split()
            if len(parts) < 11:
                continue
            cmd = " ".join(parts[10:])
            if "claude" in cmd and "autoresume" not in cmd:
                pids.append(parts[1])
        if not pids:
            return set()

        lsof = subprocess.run(
            ["lsof", "-a", "-p", ",".join(pids), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=10,
        )
        return {
            line[1:]
            for line in lsof.stdout.splitlines()
            if line.startswith("n") and len(line) > 1 and line[1] == "/"
        }
    except Exception as exc:
        log.debug("Failed to enumerate Claude processes: %s", exc)
        return set()


def is_latest_session_in_project(jsonl_path: str) -> bool:
    """True if *jsonl_path* is the most recently modified JSONL in its project dir.

    Older sessions in the same directory are almost certainly dead — a newer
    session has taken over.  Subagent files are excluded from comparison.
    """
    project_dir = os.path.dirname(jsonl_path)
    try:
        siblings = [
            p
            for p in glob.glob(os.path.join(project_dir, "*.jsonl"))
            if "/subagents/" not in p
        ]
        if not siblings:
            return False
        most_recent = max(siblings, key=os.path.getmtime)
        return os.path.abspath(jsonl_path) == os.path.abspath(most_recent)
    except (OSError, ValueError):
        return False


def has_new_assistant_activity(jsonl_path: str, after: datetime) -> bool:
    """True if *jsonl_path* contains an assistant entry (without error) after *after*."""
    after_iso = after.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(jsonl_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                    if (
                        entry.get("type") == "assistant"
                        and not entry.get("error")
                        and entry.get("timestamp", "") > after_iso
                    ):
                        return True
                except (json.JSONDecodeError, TypeError):
                    pass
    except (FileNotFoundError, PermissionError):
        pass
    return False


def process_pending_resumes(pending: List[PendingResume]) -> None:
    """Process pending resumes: notify, attempt channel POST, verify activity.

    Three states per entry:
      1. Waiting — ``resume_sent_at is None`` and reset time not reached yet.
      2. Ready — ``resume_sent_at is None`` and reset+grace has passed → POST.
      3. Verifying — ``resume_sent_at`` is set → wait for assistant activity in JSONL.
    """
    for pr in list(pending):
        project = os.path.basename(pr.cwd) or pr.session_id[:8]

        # --- Dismiss the interactive "What do you want to do?" prompt ---
        if not pr.prompt_dismissed and pr.prompt_dismiss_attempts < MAX_PROMPT_DISMISS_ATTEMPTS:
            pr.prompt_dismiss_attempts += 1
            if dismiss_interactive_prompt(pr.cwd):
                pr.prompt_dismissed = True
            elif pr.prompt_dismiss_attempts >= MAX_PROMPT_DISMISS_ATTEMPTS:
                log.warning(
                    "Could not dismiss prompt for session=%s after %d attempts",
                    pr.session_id,
                    pr.prompt_dismiss_attempts,
                )

        if not pr.notified:
            send_notification(
                "Claude Rate Limit",
                f"{project} — resets at {pr.reset_at.strftime('%H:%M %Z')}",
            )
            pr.notified = True
            log.info(
                "Waiting for reset: session=%s at %s",
                pr.session_id,
                pr.reset_at.isoformat(),
            )

        now = datetime.now(pr.reset_at.tzinfo)

        # --- State 3: verifying a previous resume attempt ---
        if pr.resume_sent_at is not None:
            if now < pr.resume_sent_at + timedelta(seconds=VERIFY_WINDOW_SECONDS):
                continue
            if pr.jsonl_path and has_new_assistant_activity(pr.jsonl_path, pr.resume_sent_at):
                log.info("Resume verified: session=%s", pr.session_id)
                send_notification("Claude Resumed", f"{project} — confirmed active")
                pending.remove(pr)
            else:
                pr.resume_attempts += 1
                pr.resume_sent_at = None
                if pr.resume_attempts >= MAX_RESUME_ATTEMPTS:
                    log.warning(
                        "Resume not verified after %d attempts for session=%s — giving up",
                        pr.resume_attempts,
                        pr.session_id,
                    )
                    send_notification(
                        "Claude Resume Failed",
                        f"{project} — no activity after {pr.resume_attempts} attempts",
                    )
                    pending.remove(pr)
                else:
                    log.info(
                        "No activity after resume for session=%s — attempt %d/%d, retrying",
                        pr.session_id,
                        pr.resume_attempts,
                        MAX_RESUME_ATTEMPTS,
                    )
            continue

        # --- State 1: waiting for reset + grace ---
        if now < pr.reset_at + timedelta(seconds=RESUME_GRACE_SECONDS):
            continue

        # --- State 2: ready to POST ---
        if try_post_channel(RESUME_PROMPT):
            if pr.jsonl_path:
                log.info("Resume sent via channel session=%s, verifying…", pr.session_id)
                pr.resume_sent_at = now
            else:
                log.info("Resumed via channel session=%s", pr.session_id)
                send_notification("Claude Resuming", f"{project} — resuming now")
                pending.remove(pr)
        else:
            pr.resume_attempts += 1
            if pr.resume_attempts >= MAX_RESUME_ATTEMPTS:
                log.warning(
                    "Channel POST failed %d times for session=%s — giving up",
                    pr.resume_attempts,
                    pr.session_id,
                )
                send_notification(
                    "Claude Resume Failed",
                    f"{project} — channel not reachable after {pr.resume_attempts} attempts",
                )
                pending.remove(pr)
            else:
                log.info(
                    "Channel POST failed for session=%s — attempt %d/%d, will retry",
                    pr.session_id,
                    pr.resume_attempts,
                    MAX_RESUME_ATTEMPTS,
                )


def run_daemon(projects_root: Optional[Path] = None) -> None:
    """Main loop: watch JSONL files, notify on rate limits, auto-resume."""
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"

    pending: List[PendingResume] = []
    watchers: dict[str, FileWatcher] = {}
    handler = make_rate_limit_handler(pending)

    running = True

    def _shutdown(signum, _frame):
        nonlocal running
        log.info("Received signal %s — shutting down", signal.Signals(signum).name)
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Daemon started — watching %s", projects_root)
    last_stale_scan: float = float("-inf")

    while running:
        # 1. Discover JSONL files
        for path in glob.glob(str(projects_root / "**" / "*.jsonl"), recursive=True):
            if path not in watchers:
                log.info("Watching new file: %s", path)
                watchers[path] = FileWatcher(path, handler, skip_existing=True)

        # 2. Scan all watchers, prune deleted files
        for path in list(watchers):
            if not os.path.exists(path):
                del watchers[path]
                continue
            watchers[path].scan()

        # 3. Process pending resumes
        process_pending_resumes(pending)

        # 4. Periodic stale-session scan (first run is immediate)
        now_mono = time.monotonic()
        if now_mono - last_stale_scan >= STALE_SCAN_INTERVAL_SECONDS:
            last_stale_scan = now_mono
            active_cwds = find_claude_session_cwds()
            scan_paths = [p for p in watchers if "/subagents/" not in p]
            stale = find_unresolved_rate_limits(scan_paths)
            now_utc = datetime.now(ZoneInfo("UTC"))
            for info in stale:
                entry_age = (
                    now_utc - datetime.fromisoformat(info["timestamp"].replace("Z", "+00:00"))
                ).total_seconds()
                if entry_age > STALE_SCAN_MAX_AGE_SECONDS:
                    continue
                if any(p.session_id == info["session_id"] for p in pending):
                    continue
                if not is_latest_session_in_project(info["jsonl_path"]):
                    log.debug(
                        "Stale session=%s superseded by newer session — skipping",
                        info["session_id"][:8],
                    )
                    continue
                if info["cwd"] not in active_cwds:
                    log.debug(
                        "Stale rate limit for inactive session=%s (cwd=%s) — skipping",
                        info["session_id"][:8],
                        info["cwd"],
                    )
                    continue
                log.info(
                    "Stale rate limit found: session=%s cwd=%s — adding to pending",
                    info["session_id"],
                    info["cwd"],
                )
                pending.append(
                    PendingResume(
                        session_id=info["session_id"],
                        cwd=info["cwd"],
                        reset_at=datetime.now(ZoneInfo("UTC"))
                        - timedelta(seconds=RESUME_GRACE_SECONDS + 1),
                        jsonl_path=info["jsonl_path"],
                        notified=True,
                    )
                )

        # 5. Sleep
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_daemon()
