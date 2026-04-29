"""Tests for the claude-autoresume daemon."""

import sys
from pathlib import Path

# Allow `import daemon` from tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"

from datetime import datetime
from zoneinfo import ZoneInfo

from daemon import (
    is_rate_limit_entry,
    extract_rate_limit_info,
    parse_reset_time,
    FileWatcher,
    try_post_channel,
    send_notification,
    make_rate_limit_handler,
    PendingResume,
)


# --- is_rate_limit_entry tests ---


def test_is_rate_limit_entry_true_positive():
    """Rate-limit JSONL line is detected."""
    line = (FIXTURES / "rate_limit_entry.jsonl").read_text().strip()
    assert is_rate_limit_entry(line) is True


def test_is_rate_limit_entry_true_negative():
    """Normal user entry is not flagged as rate limit."""
    line = (FIXTURES / "normal_entry.jsonl").read_text().strip()
    assert is_rate_limit_entry(line) is False


def test_is_rate_limit_entry_invalid_json():
    """Invalid JSON returns False, never raises."""
    assert is_rate_limit_entry("not json at all{{{") is False


def test_is_rate_limit_entry_missing_fields():
    """JSON with error but missing isApiErrorMessage returns False."""
    assert is_rate_limit_entry('{"error":"rate_limit"}') is False


# --- extract_rate_limit_info tests ---


def test_extract_rate_limit_info_fields():
    """All expected fields are extracted correctly."""
    line = (FIXTURES / "rate_limit_entry.jsonl").read_text().strip()
    info = extract_rate_limit_info(line)
    assert info["session_id"] == "2649ba30-abcd-4321-beef-123456789abc"
    assert info["cwd"] == "/Users/dprokofiev/prj/exploring/claude-autoresume"
    assert info["reset_text"] == "7pm (Europe/Paris)"
    assert info["timestamp"] == "2026-04-07T16:08:42.193Z"


# --- parse_reset_time tests ---


def test_parse_reset_time_simple_pm():
    """'7pm (Europe/Paris)' with now at 3pm Paris → same day 19:00 Paris."""
    paris = ZoneInfo("Europe/Paris")
    now = datetime(2026, 4, 7, 15, 0, tzinfo=paris)
    result = parse_reset_time("7pm (Europe/Paris)", now=now)
    assert result == datetime(2026, 4, 7, 19, 0, tzinfo=paris)


def test_parse_reset_time_past_rolls_to_tomorrow():
    """'7pm (Europe/Paris)' with now at 8pm Paris → next day 19:00 Paris."""
    paris = ZoneInfo("Europe/Paris")
    now = datetime(2026, 4, 7, 20, 0, tzinfo=paris)
    result = parse_reset_time("7pm (Europe/Paris)", now=now)
    assert result == datetime(2026, 4, 8, 19, 0, tzinfo=paris)


def test_parse_reset_time_with_minutes():
    """'7:30pm (Europe/Paris)' → 19:30 Paris."""
    paris = ZoneInfo("Europe/Paris")
    now = datetime(2026, 4, 7, 15, 0, tzinfo=paris)
    result = parse_reset_time("7:30pm (Europe/Paris)", now=now)
    assert result == datetime(2026, 4, 7, 19, 30, tzinfo=paris)


def test_parse_reset_time_am():
    """'9am (America/New_York)' → 09:00 New York."""
    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 4, 7, 7, 0, tzinfo=ny)
    result = parse_reset_time("9am (America/New_York)", now=now)
    assert result == datetime(2026, 4, 7, 9, 0, tzinfo=ny)


# --- FileWatcher tests ---

import tempfile
import os


def test_filewatcher_append_detection():
    """Write a line, scan → 1 callback. Append another, scan → 2 total."""
    collected = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
        f.write("line1\n")
        f.flush()

    try:
        watcher = FileWatcher(path, callback=collected.append)
        watcher.scan()
        assert collected == ["line1"]

        with open(path, "a") as f:
            f.write("line2\n")

        watcher.scan()
        assert collected == ["line1", "line2"]
    finally:
        os.unlink(path)


def test_filewatcher_no_duplicate_reads():
    """Write a line, scan twice with no new content → still only 1 callback."""
    collected = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
        f.write("only-once\n")
        f.flush()

    try:
        watcher = FileWatcher(path, callback=collected.append)
        watcher.scan()
        watcher.scan()
        assert collected == ["only-once"]
    finally:
        os.unlink(path)


def test_filewatcher_skip_existing():
    """skip_existing=True ignores pre-existing content, picks up new lines."""
    collected = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
        f.write("old-line\n")
        f.flush()

    try:
        watcher = FileWatcher(path, callback=collected.append, skip_existing=True)
        watcher.scan()
        assert collected == []

        with open(path, "a") as f:
            f.write("new-line\n")

        watcher.scan()
        assert collected == ["new-line"]
    finally:
        os.unlink(path)


def test_filewatcher_file_not_found():
    """Watcher with non-existent path: scan → no error, 0 callbacks."""
    collected = []
    watcher = FileWatcher("/tmp/nonexistent_filewatcher_test.jsonl", callback=collected.append)
    watcher.scan()  # should not raise
    assert collected == []


# --- try_post_channel tests ---

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


def test_try_post_channel_unreachable():
    """POST to a port with no listener returns False."""
    assert try_post_channel("test") is False


class _CaptureHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures POST bodies for test assertions."""

    received: list[str] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        _CaptureHandler.received.append(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_try_post_channel_success(monkeypatch):
    """POST to a listening server returns True and delivers the prompt."""
    _CaptureHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    monkeypatch.setattr("daemon.CHANNEL_PORT", port)
    try:
        assert try_post_channel("test resume prompt") is True
        thread.join(timeout=2)
        assert _CaptureHandler.received == ["test resume prompt"]
    finally:
        server.server_close()


def test_try_post_channel_delivers_custom_prompt(monkeypatch):
    """Different prompts are delivered verbatim."""
    _CaptureHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    port = server.server_address[1]

    def serve_two():
        server.handle_request()
        server.handle_request()

    thread = threading.Thread(target=serve_two, daemon=True)
    thread.start()

    monkeypatch.setattr("daemon.CHANNEL_PORT", port)
    try:
        assert try_post_channel("prompt one") is True
        assert try_post_channel("prompt two") is True
        thread.join(timeout=2)
        assert _CaptureHandler.received == ["prompt one", "prompt two"]
    finally:
        server.server_close()


# --- send_notification tests ---

from unittest.mock import patch


def test_send_notification():
    """send_notification calls osascript with the title in the command."""
    with patch("daemon.subprocess.run") as mock_run:
        send_notification("Rate Limit Hit", "Resuming in 5 minutes")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]  # first positional arg (the command list)
        assert "osascript" in args[0] if isinstance(args, list) else "osascript" in args
        # Title should appear in the osascript command
        call_str = str(mock_run.call_args)
        assert "Rate Limit Hit" in call_str


# --- make_rate_limit_handler tests ---


def test_handler_appends_pending_for_rate_limit():
    """Handler creates a PendingResume from a valid rate-limit line."""
    pending: list[PendingResume] = []
    handler = make_rate_limit_handler(pending)
    line = (FIXTURES / "rate_limit_entry.jsonl").read_text().strip()
    handler(line)
    assert len(pending) == 1
    assert pending[0].session_id == "2649ba30-abcd-4321-beef-123456789abc"
    assert pending[0].cwd == "/Users/dprokofiev/prj/exploring/claude-autoresume"


def test_handler_deduplicates_by_session_id():
    """Same session_id twice → only one PendingResume."""
    pending: list[PendingResume] = []
    handler = make_rate_limit_handler(pending)
    line = (FIXTURES / "rate_limit_entry.jsonl").read_text().strip()
    handler(line)
    handler(line)
    assert len(pending) == 1


def test_handler_ignores_normal_entries():
    """Non-rate-limit lines are silently skipped."""
    pending: list[PendingResume] = []
    handler = make_rate_limit_handler(pending)
    line = (FIXTURES / "normal_entry.jsonl").read_text().strip()
    handler(line)
    assert len(pending) == 0


def test_handler_skips_unparseable_reset_time():
    """Rate-limit entry with garbled reset text is skipped with a warning."""
    import json as _json

    raw = _json.loads((FIXTURES / "rate_limit_entry.jsonl").read_text().strip())
    raw["message"]["content"][0]["text"] = "You've hit your limit · resets ???"
    line = _json.dumps(raw)

    pending: list[PendingResume] = []
    handler = make_rate_limit_handler(pending)
    handler(line)
    assert len(pending) == 0
