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
    find_unresolved_rate_limits,
    is_latest_session_in_project,
    has_new_assistant_activity,
    dismiss_interactive_prompt,
    _dismiss_via_tmux,
    _dismiss_via_terminal_app,
    _find_claude_tty_for_cwd,
    PendingResume,
    RESUME_GRACE_SECONDS,
    MAX_RESUME_ATTEMPTS,
    MAX_PROMPT_DISMISS_ATTEMPTS,
    VERIFY_WINDOW_SECONDS,
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


# --- find_unresolved_rate_limits tests ---


def _write_jsonl(tmp_path, filename, entries):
    """Helper: write a list of dicts as JSONL and return the path."""
    path = tmp_path / filename
    with open(path, "w") as f:
        for entry in entries:
            f.write(_json.dumps(entry) + "\n")
    return str(path)


RL_ENTRY = _json.loads((FIXTURES / "rate_limit_entry.jsonl").read_text().strip())
NORMAL_ASSISTANT = {
    "type": "assistant",
    "sessionId": RL_ENTRY["sessionId"],
    "cwd": RL_ENTRY["cwd"],
    "timestamp": "2026-04-07T17:00:00.000Z",
    "message": {"content": [{"text": "Here is the result…"}]},
}
QUEUE_OP = {
    "type": "queue-operation",
    "sessionId": RL_ENTRY["sessionId"],
    "timestamp": "2026-04-07T16:15:00.000Z",
}
USER_ENTRY = {
    "type": "user",
    "sessionId": RL_ENTRY["sessionId"],
    "cwd": RL_ENTRY["cwd"],
    "timestamp": "2026-04-07T16:15:00.000Z",
    "message": {"content": [{"text": "continue"}]},
}


def test_find_unresolved_detects_stale_rate_limit(tmp_path):
    """JSONL ending with a rate_limit entry is detected as unresolved."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [NORMAL_ASSISTANT, RL_ENTRY])
    results = find_unresolved_rate_limits([path])
    assert len(results) == 1
    assert results[0]["session_id"] == RL_ENTRY["sessionId"]
    assert results[0]["jsonl_path"] == path


def test_find_unresolved_ignores_resolved_session(tmp_path):
    """JSONL with assistant activity after rate_limit is not flagged."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [RL_ENTRY, NORMAL_ASSISTANT])
    results = find_unresolved_rate_limits([path])
    assert results == []


def test_find_unresolved_ignores_queue_ops_after_rate_limit(tmp_path):
    """Queue-operation and user entries after rate_limit don't count as recovery."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [RL_ENTRY, QUEUE_OP, USER_ENTRY])
    results = find_unresolved_rate_limits([path])
    assert len(results) == 1


def test_find_unresolved_empty_file(tmp_path):
    """Empty JSONL returns no results."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [])
    results = find_unresolved_rate_limits([path])
    assert results == []


def test_find_unresolved_no_rate_limit_entries(tmp_path):
    """JSONL with only normal entries returns no results."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [NORMAL_ASSISTANT, USER_ENTRY])
    results = find_unresolved_rate_limits([path])
    assert results == []


def test_find_unresolved_multiple_rate_limits_last_resolved(tmp_path):
    """Multiple rate_limit entries — only unresolved if the LAST one has no assistant after."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [
        RL_ENTRY, NORMAL_ASSISTANT,  # first cycle: resolved
        RL_ENTRY, NORMAL_ASSISTANT,  # second cycle: resolved
    ])
    results = find_unresolved_rate_limits([path])
    assert results == []


def test_find_unresolved_multiple_rate_limits_last_unresolved(tmp_path):
    """Multiple rate_limit entries — detected if the LAST one has no assistant after."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [
        RL_ENTRY, NORMAL_ASSISTANT,  # first cycle: resolved
        RL_ENTRY,                    # second cycle: stuck
    ])
    results = find_unresolved_rate_limits([path])
    assert len(results) == 1


def test_find_unresolved_missing_file():
    """Non-existent file is silently skipped."""
    results = find_unresolved_rate_limits(["/tmp/does_not_exist_12345.jsonl"])
    assert results == []


# --- has_new_assistant_activity tests ---


def test_has_activity_after_timestamp(tmp_path):
    """Returns True when an assistant entry exists after the given time."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [RL_ENTRY, NORMAL_ASSISTANT])
    after = datetime(2026, 4, 7, 16, 30, tzinfo=ZoneInfo("UTC"))
    assert has_new_assistant_activity(str(path), after) is True


def test_no_activity_after_timestamp(tmp_path):
    """Returns False when all assistant entries are before the given time."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [NORMAL_ASSISTANT, RL_ENTRY])
    after = datetime(2026, 4, 7, 18, 0, tzinfo=ZoneInfo("UTC"))
    assert has_new_assistant_activity(str(path), after) is False


def test_no_activity_error_assistant_not_counted(tmp_path):
    """Assistant entries with an error field are not counted as activity."""
    path = _write_jsonl(tmp_path, "sess.jsonl", [RL_ENTRY])
    after = datetime(2026, 4, 7, 15, 0, tzinfo=ZoneInfo("UTC"))
    assert has_new_assistant_activity(str(path), after) is False


def test_has_activity_missing_file():
    """Missing file returns False."""
    after = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))
    assert has_new_assistant_activity("/tmp/nope_12345.jsonl", after) is False


# --- process_pending_resumes: post-resume verification tests ---


def test_resume_enters_verify_state_when_jsonl_path_set(monkeypatch):
    """Successful POST with jsonl_path sets resume_sent_at instead of removing."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 5, tzinfo=paris)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        jsonl_path="/some/file.jsonl",
        notified=True,
    )
    pending = [pr]

    monkeypatch.setattr("daemon.try_post_channel", lambda p: True)
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(pending) == 1
    assert pr.resume_sent_at == now_fixed


def test_resume_verified_removes_from_pending(monkeypatch):
    """After verify window, if JSONL has activity, entry is removed."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    resume_sent = datetime(2026, 4, 7, 17, 5, tzinfo=paris)
    now_fixed = resume_sent + timedelta(seconds=VERIFY_WINDOW_SECONDS + 1)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        jsonl_path="/some/file.jsonl",
        notified=True,
        resume_sent_at=resume_sent,
    )
    pending = [pr]

    monkeypatch.setattr("daemon.has_new_assistant_activity", lambda path, after: True)
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(pending) == 0


def test_resume_not_verified_retries(monkeypatch):
    """No activity after verify window → resume_sent_at reset, attempts incremented."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    resume_sent = datetime(2026, 4, 7, 17, 5, tzinfo=paris)
    now_fixed = resume_sent + timedelta(seconds=VERIFY_WINDOW_SECONDS + 1)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        jsonl_path="/some/file.jsonl",
        notified=True,
        resume_sent_at=resume_sent,
        resume_attempts=0,
    )
    pending = [pr]

    monkeypatch.setattr("daemon.has_new_assistant_activity", lambda path, after: False)
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(pending) == 1
    assert pr.resume_sent_at is None
    assert pr.resume_attempts == 1


def test_resume_verify_gives_up_after_max_attempts(monkeypatch):
    """Verification failures exhaust MAX_RESUME_ATTEMPTS → entry removed."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    resume_sent = datetime(2026, 4, 7, 17, 5, tzinfo=paris)
    now_fixed = resume_sent + timedelta(seconds=VERIFY_WINDOW_SECONDS + 1)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        jsonl_path="/some/file.jsonl",
        notified=True,
        resume_sent_at=resume_sent,
        resume_attempts=MAX_RESUME_ATTEMPTS - 1,
    )
    pending = [pr]

    notifications = []
    monkeypatch.setattr("daemon.has_new_assistant_activity", lambda path, after: False)
    monkeypatch.setattr(
        "daemon.send_notification",
        lambda title, body: notifications.append((title, body)),
    )
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(pending) == 0
    assert any("Failed" in t for t, _ in notifications)


def test_resume_without_jsonl_path_removes_immediately(monkeypatch):
    """Successful POST without jsonl_path falls back to immediate removal."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 17, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 5, tzinfo=paris)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=True,
    )
    pending = [pr]

    monkeypatch.setattr("daemon.try_post_channel", lambda p: True)
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert len(pending) == 0


# --- is_latest_session_in_project tests ---


def test_latest_session_true_when_newest(tmp_path):
    """Most recently modified JSONL returns True."""
    _write_jsonl(tmp_path, "old.jsonl", [NORMAL_ASSISTANT])
    import time
    time.sleep(0.05)
    new = _write_jsonl(tmp_path, "new.jsonl", [NORMAL_ASSISTANT])
    old = str(tmp_path / "old.jsonl")
    assert is_latest_session_in_project(new) is True
    assert is_latest_session_in_project(old) is False


def test_latest_session_single_file(tmp_path):
    """Single JSONL file is always the latest."""
    path = _write_jsonl(tmp_path, "only.jsonl", [NORMAL_ASSISTANT])
    assert is_latest_session_in_project(path) is True


# --- dismiss_interactive_prompt tests ---


def test_dismiss_via_tmux_success(monkeypatch):
    """tmux pane matching cwd gets Enter sent to it."""
    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "tmux" and "list-panes" in cmd:
            result = type("R", (), {
                "returncode": 0,
                "stdout": "sess:0.0 claude /some/project\nsess:0.1 zsh /other\n",
                "stderr": "",
            })()
            return result
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("daemon.subprocess.run", mock_run)
    assert _dismiss_via_tmux("/some/project") is True
    assert any("send-keys" in str(c) for c in calls)


def test_dismiss_via_tmux_no_match(monkeypatch):
    """tmux pane with different cwd is not targeted."""
    def mock_run(cmd, **kwargs):
        if cmd[0] == "tmux" and "list-panes" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": "sess:0.0 claude /other/project\n",
                "stderr": "",
            })()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("daemon.subprocess.run", mock_run)
    assert _dismiss_via_tmux("/some/project") is False


def test_dismiss_via_tmux_not_installed(monkeypatch):
    """Missing tmux binary returns False without raising."""
    def mock_run(cmd, **kwargs):
        raise FileNotFoundError("tmux not found")

    monkeypatch.setattr("daemon.subprocess.run", mock_run)
    assert _dismiss_via_tmux("/some/project") is False


def test_find_claude_tty_for_cwd_match(monkeypatch):
    """Finds the TTY for a Claude process matching the target cwd."""
    def mock_run(cmd, **kwargs):
        if cmd[0] == "ps":
            return type("R", (), {
                "returncode": 0,
                "stdout": "  PID TTY      COMMAND\n12345 ttys001  claude --continue\n",
                "stderr": "",
            })()
        if cmd[0] == "lsof":
            return type("R", (), {
                "returncode": 0,
                "stdout": "p12345\nn/some/project\n",
                "stderr": "",
            })()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("daemon.subprocess.run", mock_run)
    assert _find_claude_tty_for_cwd("/some/project") == "/dev/ttys001"


def test_find_claude_tty_for_cwd_no_match(monkeypatch):
    """Returns None when no Claude process matches the target cwd."""
    def mock_run(cmd, **kwargs):
        if cmd[0] == "ps":
            return type("R", (), {
                "returncode": 0,
                "stdout": "  PID TTY      COMMAND\n12345 ttys001  claude --continue\n",
                "stderr": "",
            })()
        if cmd[0] == "lsof":
            return type("R", (), {
                "returncode": 0,
                "stdout": "p12345\nn/other/project\n",
                "stderr": "",
            })()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("daemon.subprocess.run", mock_run)
    assert _find_claude_tty_for_cwd("/some/project") is None


def test_dismiss_via_terminal_app_success(monkeypatch):
    """Terminal.app AppleScript returning 'ok' counts as success."""
    monkeypatch.setattr("daemon._find_claude_tty_for_cwd", lambda cwd: "/dev/ttys001")

    def mock_run(cmd, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

    monkeypatch.setattr("daemon.subprocess.run", mock_run)
    assert _dismiss_via_terminal_app("/some/project") is True


def test_dismiss_via_terminal_app_not_found(monkeypatch):
    """No matching TTY returns False without running AppleScript."""
    monkeypatch.setattr("daemon._find_claude_tty_for_cwd", lambda cwd: None)
    assert _dismiss_via_terminal_app("/some/project") is False


def test_dismiss_interactive_prompt_tries_tmux_first(monkeypatch):
    """tmux is tried first; Terminal.app is skipped if tmux succeeds."""
    order = []
    monkeypatch.setattr("daemon._dismiss_via_tmux", lambda cwd: (order.append("tmux"), True)[-1])
    monkeypatch.setattr("daemon._dismiss_via_terminal_app", lambda cwd: (order.append("terminal"), True)[-1])
    assert dismiss_interactive_prompt("/p") is True
    assert order == ["tmux"]


def test_dismiss_interactive_prompt_falls_back_to_terminal(monkeypatch):
    """If tmux fails, Terminal.app is tried."""
    order = []
    monkeypatch.setattr("daemon._dismiss_via_tmux", lambda cwd: (order.append("tmux"), False)[-1])
    monkeypatch.setattr("daemon._dismiss_via_terminal_app", lambda cwd: (order.append("terminal"), True)[-1])
    assert dismiss_interactive_prompt("/p") is True
    assert order == ["tmux", "terminal"]


def test_dismiss_interactive_prompt_both_fail(monkeypatch):
    """Both methods failing returns False."""
    monkeypatch.setattr("daemon._dismiss_via_tmux", lambda cwd: False)
    monkeypatch.setattr("daemon._dismiss_via_terminal_app", lambda cwd: False)
    assert dismiss_interactive_prompt("/p") is False


def test_process_pending_resumes_dismisses_prompt(monkeypatch):
    """Prompt dismissal is attempted before notification."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 19, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 0, tzinfo=paris)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=False,
    )
    pending = [pr]

    dismiss_calls = []
    monkeypatch.setattr(
        "daemon.dismiss_interactive_prompt",
        lambda cwd: (dismiss_calls.append(cwd), True)[-1],
    )
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert dismiss_calls == ["/some/project"]
    assert pr.prompt_dismissed is True
    assert pr.prompt_dismiss_attempts == 1


def test_process_pending_resumes_stops_dismissing_after_success(monkeypatch):
    """Once prompt_dismissed is True, no further attempts are made."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 19, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 0, tzinfo=paris)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=True,
        prompt_dismissed=True,
    )
    pending = [pr]

    dismiss_calls = []
    monkeypatch.setattr(
        "daemon.dismiss_interactive_prompt",
        lambda cwd: (dismiss_calls.append(cwd), True)[-1],
    )
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert dismiss_calls == []


def test_process_pending_resumes_gives_up_dismissing(monkeypatch):
    """After MAX_PROMPT_DISMISS_ATTEMPTS, no more dismiss calls are made."""
    paris = ZoneInfo("Europe/Paris")
    reset_at = datetime(2026, 4, 7, 19, 0, tzinfo=paris)
    now_fixed = datetime(2026, 4, 7, 17, 0, tzinfo=paris)

    pr = PendingResume(
        session_id="sess-1",
        cwd="/some/project",
        reset_at=reset_at,
        notified=True,
        prompt_dismiss_attempts=MAX_PROMPT_DISMISS_ATTEMPTS,
    )
    pending = [pr]

    dismiss_calls = []
    monkeypatch.setattr(
        "daemon.dismiss_interactive_prompt",
        lambda cwd: (dismiss_calls.append(cwd), False)[-1],
    )
    monkeypatch.setattr("daemon.send_notification", lambda *a, **kw: None)
    _mock_datetime_now(monkeypatch, now_fixed)

    process_pending_resumes(pending)

    assert dismiss_calls == []
