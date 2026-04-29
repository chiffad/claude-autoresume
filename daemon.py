#!/usr/bin/env python3
"""macOS daemon that auto-resumes Claude Code sessions after rate limits."""

import glob
import json
import logging
import os
import re
import subprocess
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
        reset += timedelta(days=1)

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


def try_post_channel(prompt: str) -> bool:
    """POST the resume prompt to the autoresume channel server."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{CHANNEL_PORT}",
            data=prompt.encode(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def send_notification(title: str, body: str) -> None:
    """Send a macOS notification via osascript."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    subprocess.run(["osascript", "-e", script], check=False)


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 10
RESUME_GRACE_SECONDS = 90

log = logging.getLogger(__name__)


@dataclass
class PendingResume:
    session_id: str
    cwd: str
    reset_at: datetime
    notified: bool = False


def make_rate_limit_handler(pending: List[PendingResume]) -> Callable[[str], None]:
    """Return a callback that filters rate-limit lines and appends to *pending*."""

    def _handler(line: str) -> None:
        if not is_rate_limit_entry(line):
            return
        info = extract_rate_limit_info(line)
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


def run_daemon(projects_root: Optional[Path] = None) -> None:
    """Main loop: watch JSONL files, notify on rate limits, auto-resume."""
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"

    pending: List[PendingResume] = []
    watchers: dict[str, FileWatcher] = {}
    handler = make_rate_limit_handler(pending)

    log.info("Daemon started — watching %s", projects_root)

    while True:
        # 1. Discover JSONL files
        for path in glob.glob(str(projects_root / "*" / "*.jsonl")):
            if path not in watchers:
                log.info("Watching new file: %s", path)
                watchers[path] = FileWatcher(path, handler, skip_existing=True)

        # 2. Scan all watchers, prune deleted files
        for path in list(watchers):
            if not os.path.exists(path):
                del watchers[path]
                continue
            watchers[path].scan()

        # 3. Process pending resumes (iterate over a copy to allow removal)
        for pr in list(pending):
            if not pr.notified:
                send_notification(
                    "Claude Rate Limit",
                    f"Session {pr.session_id[:8]}… resets at {pr.reset_at.strftime('%H:%M %Z')}",
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
                        f"Session {pr.session_id[:8]}… resuming now",
                    )
                else:
                    log.warning(
                        "Channel POST failed for session=%s — is Claude running with --channels?",
                        pr.session_id,
                    )
                    send_notification(
                        "Claude Resume Failed",
                        f"Session {pr.session_id[:8]}… channel not reachable",
                    )
                pending.remove(pr)

        # 4. Sleep
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_daemon()
