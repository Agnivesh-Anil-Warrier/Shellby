"""Tests for shellby_parser.py addressing issues 2.1, 2.2, 2.3, and 1.2."""

import json
from pathlib import Path
import pyte
from shellby_parser import RecordingScreen, parse_session, ParsedLine


def test_recording_screen_scrollback_preservation(tmp_path: Path):
    """Test that lines scrolling off the viewport are preserved in committed scrollback (Issue 2.1 & 2.3)."""
    screen = RecordingScreen(columns=80, lines=4)
    stream = pyte.Stream(screen)

    for i in range(10):
        screen.current_event_time = f"2026-01-01T00:00:0{i}"
        stream.feed(f"line {i}\r\n")

    lines = screen.finalize("2026-01-01T00:00:10")
    assert len(lines) == 10
    for idx, line in enumerate(lines):
        assert line.index == idx
        assert line.text == f"line {idx}"
        assert line.timestamp == f"2026-01-01T00:00:0{idx}"


def test_recording_screen_clear_preservation():
    """Test that clear screen (\\x1b[2J) does NOT destroy preceding transcript (Issue 2.2)."""
    screen = RecordingScreen(columns=80, lines=10)
    stream = pyte.Stream(screen)

    screen.current_event_time = "2026-01-01T00:00:01"
    stream.feed("echo before clear\r\noutput 1\r\n")

    # Clear screen
    screen.current_event_time = "2026-01-01T00:00:02"
    stream.feed("\x1b[2J\x1b[H")

    screen.current_event_time = "2026-01-01T00:00:03"
    stream.feed("echo after clear\r\noutput 2\r\n")

    lines = screen.finalize("2026-01-01T00:00:04")
    texts = [l.text for l in lines]
    assert "echo before clear" in texts
    assert "output 1" in texts
    assert "echo after clear" in texts
    assert "output 2" in texts


def test_recording_screen_inplace_carriage_return():
    """Test in-place updates (\\r progress bars) receive accurate timestamps (Issue 2.3)."""
    screen = RecordingScreen(columns=80, lines=5)
    stream = pyte.Stream(screen)

    screen.current_event_time = "2026-01-01T00:00:01"
    stream.feed("Progress: 50%\r")

    screen.current_event_time = "2026-01-01T00:00:05"
    stream.feed("Progress: 100%\r\n")

    lines = screen.finalize("2026-01-01T00:00:06")
    assert any("Progress: 100%" in l.text and l.timestamp == "2026-01-01T00:00:05" for l in lines)


def test_parse_session_with_resize_event(tmp_path: Path):
    """Test that dynamic terminal resize events mid-session adjust columns (Issue 1.2)."""
    session_file = tmp_path / "test_session.json"
    session_data = {
        "session_id": "test-uuid",
        "shell": "/bin/bash",
        "columns": 80,
        "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:00:10",
        "duration": 10.0,
        "events": [
            {"time": "2026-01-01T00:00:01", "type": "output", "text": "hello\r\n"},
            {"time": "2026-01-01T00:00:02", "type": "resize", "text": "120x30"},
            {"time": "2026-01-01T00:00:03", "type": "output", "text": "resized\r\n"},
        ],
    }
    session_file.write_text(json.dumps(session_data), encoding="utf-8")

    parsed = parse_session(session_file)
    assert len(parsed.lines) >= 2
    assert parsed.lines[0].text == "hello"
    assert parsed.lines[1].text == "resized"
