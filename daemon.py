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
# Daemon loop
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 10
RESUME_GRACE_SECONDS = 90
MAX_RESUME_ATTEMPTS = 5

log = logging.getLogger(__name__)


@dataclass
class PendingResume:
    session_id: str
    cwd: str
    reset_at: datetime
    notified: bool = False
    resume_attempts: int = 0


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
        try:
            reset_at = parse_reset_time(info["reset_text"])
        except (ValueError, TypeError):
            log.warning("Could not parse reset time %r — skipping", info["reset_text"])
            return
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


def process_pending_resumes(pending: List[PendingResume]) -> None:
    """Process pending resumes: notify, attempt channel POST, handle retries.

    Extracted from run_daemon's inner loop to enable testing.
    """
    for pr in list(pending):
        project = os.path.basename(pr.cwd) or pr.session_id[:8]

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
        if now >= pr.reset_at + timedelta(seconds=RESUME_GRACE_SECONDS):
            if try_post_channel(RESUME_PROMPT):
                log.info("Resumed via channel session=%s", pr.session_id)
                send_notification(
                    "Claude Resuming",
                    f"{project} — resuming now",
                )
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

        # 4. Sleep
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_daemon()
