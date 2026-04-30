"""Tests for the claude-autoresume daemon."""

import json as _json
import os
import sys
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Allow `import daemon` from tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"

from daemon import (
    is_rate_limit_entry,
    extract_rate_limit_info,
    parse_reset_time,
    FileWatcher,
    try_post_channel,
    _load_auth_token,
    send_notification,
    make_rate_limit_handler,
    process_pending_resumes,
    PendingResume,
    RESUME_GRACE_SECONDS,
    MAX_RESUME_ATTEMPTS,
    UNKNOWN_RESET_FALLBACK_MINUTES,
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


def _make_capture_handler():
    """Create a fresh handler class with isolated state per test."""
    class Handler(BaseHTTPRequestHandler):
        received: list[str] = []
        received_auth: list[str] = []

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            Handler.received.append(body)
            Handler.received_auth.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    return Handler


def test_try_post_channel_unreachable(monkeypatch):
    """POST to a port with no listener returns False."""
    monkeypatch.setattr("daemon.CHANNEL_PORT", 19999)
    assert try_post_channel("test") is False


def test_try_post_channel_success(monkeypatch):
    """POST to a listening server returns True and delivers the prompt."""
    Handler = _make_capture_handler()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    monkeypatch.setattr("daemon.CHANNEL_PORT", port)
    monkeypatch.setattr("daemon.AUTH_TOKEN_PATH", Path("/tmp/nonexistent_token"))
    try:
        assert try_post_channel("test resume prompt") is True
        thread.join(timeout=2)
        assert Handler.received == ["test resume prompt"]
    finally:
        server.server_close()


def test_try_post_channel_delivers_custom_prompt(monkeypatch):
    """Different prompts are delivered verbatim."""
    Handler = _make_capture_handler()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    def serve_two():
        server.handle_request()
        server.handle_request()

    thread = threading.Thread(target=serve_two, daemon=True)
    thread.start()

    monkeypatch.setattr("daemon.CHANNEL_PORT", port)
    monkeypatch.setattr("daemon.AUTH_TOKEN_PATH", Path("/tmp/nonexistent_token"))
    try:
        assert try_post_channel("prompt one") is True
        assert try_post_channel("prompt two") is True
        thread.join(timeout=2)
        assert Handler.received == ["prompt one", "prompt two"]
    finally:
        server.server_close()


def test_try_post_channel_sends_auth_token(monkeypatch, tmp_path):
    """When an auth token file exists, the Bearer header is sent."""
    Handler = _make_capture_handler()
    token_file = tmp_path / "auth-token"
    token_file.write_text("test-secret-token\n")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    monkeypatch.setattr("daemon.CHANNEL_PORT", port)
    monkeypatch.setattr("daemon.AUTH_TOKEN_PATH", token_file)
    try:
        assert try_post_channel("authed prompt") is True
        thread.join(timeout=2)
        assert Handler.received == ["authed prompt"]
        assert Handler.received_auth == ["Bearer test-secret-token"]
    finally:
        server.server_close()


def test_load_auth_token_missing(monkeypatch):
    """Missing token file returns None."""
    monkeypatch.setattr("daemon.AUTH_TOKEN_PATH", Path("/tmp/nonexistent_token"))
    assert _load_auth_token() is None


def test_load_auth_token_reads_file(monkeypatch, tmp_path):
    """Token file contents are returned stripped."""
    token_file = tmp_path / "auth-token"
    token_file.write_text("  my-token-value  \n")
    monkeypatch.setattr("daemon.AUTH_TOKEN_PATH", token_file)
    assert _load_auth_token() == "my-token-value"


# --- send_notification tests ---


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


def test_handler_uses_fallback_for_unparseable_reset_time():
    """Rate-limit entry with garbled reset text uses fallback reset time."""
    raw = _json.loads((FIXTURES / "rate_limit_entry.jsonl").read_text().strip())
    raw["message"]["content"][0]["text"] = "You've hit your limit · resets ???"
    line = _json.dumps(raw)

    pending: list[PendingResume] = []
    handler = make_rate_limit_handler(pending)
    handler(line)
    assert len(pending) == 1
    expected_min = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=UNKNOWN_RESET_FALLBACK_MINUTES - 1)
    expected_max = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=UNKNOWN_RESET_FALLBACK_MINUTES + 1)
    assert expected_min <= pending[0].reset_at <= expected_max


# --- Group limit (no reset time in message) ---


def test_handler_creates_pending_for_group_limit():
    """Group limit entry with no 'resets ...' text creates PendingResume with fallback."""
    line = (FIXTURES / "group_limit_entry.jsonl").read_text().strip()
    pending: list[PendingResume] = []
    handler = make_rate_limit_handler(pending)
    handler(line)
    assert len(pending) == 1
    assert pending[0].session_id == "1a15fe16-fa23-466f-a416-74ed3a13d8ac"
    expected_min = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=UNKNOWN_RESET_FALLBACK_MINUTES - 1)
    expected_max = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=UNKNOWN_RESET_FALLBACK_MINUTES + 1)
    assert expected_min <= pending[0].reset_at <= expected_max


def test_is_rate_limit_entry_group_limit():
    """Group limit JSONL line is detected as rate limit."""
    line = (FIXTURES / "group_limit_entry.jsonl").read_text().strip()
    assert is_rate_limit_entry(line) is True


def test_extract_rate_limit_info_group_limit_no_reset_text():
    """Group limit entry returns reset_text=None."""
    line = (FIXTURES / "group_limit_entry.jsonl").read_text().strip()
    info = extract_rate_limit_info(line)
    assert info["session_id"] == "1a15fe16-fa23-466f-a416-74ed3a13d8ac"
    assert info["reset_text"] is None


# --- CRITICAL: parse_reset_time 12am/12pm boundary tests ---


def test_parse_reset_time_12pm_is_noon():
    """'12pm (Europe/Paris)' → 12:00 (noon), not midnight."""
    paris = ZoneInfo("Europe/Paris")
    now = datetime(2026, 4, 7, 10, 0, tzinfo=paris)
    result = parse_reset_time("12pm (Europe/Paris)", now=now)
    assert result.hour == 12
    assert result == datetime(2026, 4, 7, 12, 0, tzinfo=paris)


def test_parse_reset_time_12am_is_midnight():
    """'12am (America/New_York)' → 00:00 (midnight)."""
    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 4, 7, 22, 0, tzinfo=ny)
    result = parse_reset_time("12am (America/New_York)", now=now)
    assert result.hour == 0
    # 10pm → midnight is in the future same day? No, 22:00 > 00:00 so it rolls to next day.
    assert result == datetime(2026, 4, 8, 0, 0, tzinfo=ny)


# --- CRITICAL: FileWatcher file truncation/rotation test ---


def test_filewatcher_handles_file_truncation():
    """When a file is truncated (size < offset), watcher resets and reads new content."""
    collected = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
        f.write("long-line-that-sets-a-high-offset\n")
        f.flush()

    try:
        watcher = FileWatcher(path, callback=collected.append)
        watcher.scan()
        assert collected == ["long-line-that-sets-a-high-offset"]

        # Truncate and write shorter content
        with open(path, "w") as f:
            f.write("short\n")

        watcher.scan()
        assert collected == ["long-line-that-sets-a-high-offset", "short"]
    finally:
        os.unlink(path)


# --- WARNING: parse_reset_time with non-matching input ---


def test_parse_reset_time_invalid_format_raises():
    """Non-matching reset text raises ValueError."""
    import pytest
    with pytest.raises(ValueError, match="Cannot parse reset time"):
        parse_reset_time("tomorrow")


def test_parse_reset_time_garbage_raises():
    """Completely garbled input raises ValueError."""
    import pytest
    with pytest.raises(ValueError, match="Cannot parse reset time"):
        parse_reset_time("not-a-time-at-all")


# --- WARNING: extract_rate_limit_info when "resets" is absent ---


def test_extract_rate_limit_info_no_resets_keyword():
    """Message without 'resets ...' yields reset_text=None."""
    raw = _json.loads((FIXTURES / "rate_limit_entry.jsonl").read_text().strip())
    raw["message"]["content"][0]["text"] = "You've hit your limit."
    line = _json.dumps(raw)
    info = extract_rate_limit_info(line)
    assert info["reset_text"] is None


# --- WARNING: is_rate_limit_entry with None and empty string ---


def test_is_rate_limit_entry_none_input():
    """None input returns False without raising."""
    assert is_rate_limit_entry(None) is False


def test_is_rate_limit_entry_empty_string():
    """Empty string returns False without raising."""
    assert is_rate_limit_entry("") is False


# --- WARNING: try_post_channel with non-200 response ---


def test_try_post_channel_non_200_returns_false(monkeypatch):
    """Server returning 500 causes try_post_channel to return False.

    Note: urllib raises HTTPError for non-2xx responses, so this exercises
    the except-Exception branch, not resp.status != 200.
    """
    class ErrorHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal error")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), ErrorHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    monkeypatch.setattr("daemon.CHANNEL_PORT", port)
    monkeypatch.setattr("daemon.AUTH_TOKEN_PATH", Path("/tmp/nonexistent_token"))
    try:
        result = try_post_channel("test prompt")
        thread.join(timeout=2)
        assert result is False
    finally:
        server.server_close()


# --- process_pending_resumes tests ---


def _mock_datetime_now(monkeypatch, fixed_now):
    """Patch daemon.datetime.now to return a fixed timestamp."""
    monkeypatch.setattr("daemon.datetime", type("MockDT", (), {
        "now": staticmethod(lambda tz: fixed_now),
    }))


def test_process_pending_resumes_triggers_channel_post_after_grace(monkeypatch):
    """After reset_at + grace period, try_post_channel is called and entry is removed."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 5, tzinfo=paris)  # 5 min after reset

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=True,
    )
    pending = [pr]

    post_calls = []

    def mock_try_post(prompt):
        post_calls.append(prompt)
        return True

    monkeypatch.setattr("daemon.try_post_channel", mock_try_post)
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(post_calls) == 1
    assert len(pending) == 0


def test_process_pending_resumes_not_triggered_before_grace(monkeypatch):
    """Before reset_at + grace, no resume is attempted."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 0, 30, tzinfo=paris)  # only 30s after reset

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=True,
    )
    pending = [pr]

    post_calls = []

    monkeypatch.setattr("daemon.try_post_channel", lambda p: (post_calls.append(p), True)[-1])
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(post_calls) == 0
    assert len(pending) == 1  # still pending


def test_process_pending_resumes_max_attempts_gives_up(monkeypatch):
    """After MAX_RESUME_ATTEMPTS failed channel POSTs, the entry is removed."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 5, tzinfo=paris)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=True,
        resume_attempts=MAX_RESUME_ATTEMPTS - 1,
    )
    pending = [pr]

    notifications = []
    monkeypatch.setattr("daemon.try_post_channel", lambda p: False)
    monkeypatch.setattr(
        "daemon.send_notification",
        lambda title, body: notifications.append((title, body)),
    )
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(pending) == 0  # removed after max attempts
    assert any("Failed" in t for t, _ in notifications)


def test_process_pending_resumes_retries_on_failure(monkeypatch):
    """Failed channel POST increments resume_attempts but keeps entry in pending."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 5, tzinfo=paris)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=True,
        resume_attempts=0,
    )
    pending = [pr]

    monkeypatch.setattr("daemon.try_post_channel", lambda p: False)
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(pending) == 1
    assert pending[0].resume_attempts == 1


def test_process_pending_resumes_sends_notification_on_first_seen(monkeypatch):
    """Unnotified entry triggers a macOS notification and sets notified=True."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 19, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 0, tzinfo=paris)  # before reset, no resume attempt

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=False,
    )
    pending = [pr]

    notifications = []
    monkeypatch.setattr(
        "daemon.send_notification",
        lambda title, body: notifications.append((title, body)),
    )
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert pr.notified is True
    assert len(notifications) == 1
    assert "Rate Limit" in notifications[0][0]


# --- WARNING: extract_rate_limit_info with malformed input ---


def test_extract_rate_limit_info_missing_message_key():
    """JSON missing 'message' key raises KeyError."""
    import pytest

    line = _json.dumps({"sessionId": "abc", "cwd": "/x", "timestamp": "t"})
    with pytest.raises(KeyError):
        extract_rate_limit_info(line)


def test_extract_rate_limit_info_missing_content():
    """JSON with empty content list raises IndexError."""
    import pytest

    line = _json.dumps({
        "sessionId": "abc",
        "cwd": "/x",
        "timestamp": "t",
        "message": {"content": []},
    })
    with pytest.raises(IndexError):
        extract_rate_limit_info(line)


# --- WARNING: FileWatcher with empty lines ---


def test_filewatcher_skips_empty_lines():
    """Empty lines in the file are not delivered to the callback."""
    collected = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
        f.write("line1\n\n\nline2\n\n")
        f.flush()

    try:
        watcher = FileWatcher(path, callback=collected.append)
        watcher.scan()
        assert collected == ["line1", "line2"]
    finally:
        os.unlink(path)
