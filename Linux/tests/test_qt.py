"""Unit and regression tests for shellby_qt.py (Section 6 Rectifications)."""

import json
import os
import shutil
import tempfile
from pathlib import Path

# Ensure headless execution works across headless CI runners
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from shellby_qt import (
    fmt_date, fmt_duration, norm_rel_path, base_stem, dir_of,
    is_bare_guide_file, stem_for, render_markdown_html,
    read_library, write_library, update_session_meta,
    delete_session_files, scan_folder, matches_search,
    within_when, sort_sessions, parse_iso, create_vector_icon
)


def test_fmt_date_zero_padded_minutes():
    """Verify Issue 6.6: minute components are never corrupted (e.g., 1:05 PM, not 1: 5 PM)."""
    # 2026-10-15T13:05:00Z -> 1:05 PM
    formatted_05 = fmt_date("2026-10-15T13:05:00Z")
    assert formatted_05 is not None
    assert ":05" in formatted_05
    assert " 5" not in formatted_05
    assert formatted_05.endswith("PM")

    # 2026-10-15T00:00:00Z -> 12:00 AM
    formatted_00 = fmt_date("2026-10-15T00:00:00Z")
    assert formatted_00 is not None
    assert ":00" in formatted_00

    # 2026-10-15T09:07:30Z -> 9:07 AM
    formatted_07 = fmt_date("2026-10-15T09:07:30Z")
    assert formatted_07 is not None
    assert ":07" in formatted_07

    assert fmt_date(None) is None
    assert fmt_date("") is None


def test_fmt_duration():
    assert fmt_duration(None) is None
    assert fmt_duration(45) == "45s"
    assert fmt_duration(125) == "2m 5s"
    assert fmt_duration(0) == "0s"


def test_path_helpers_normalization():
    """Verify Issue 6.7: POSIX path normalization across helper functions."""
    assert norm_rel_path("2026-10-15\\session_1\\session.json") == "2026-10-15/session_1/session.json"
    assert base_stem("2026-10-15/session_1") == "session_1"
    assert dir_of("2026-10-15/session_1/session.json") == "2026-10-15/session_1"
    assert dir_of("session.json") == ""

    assert is_bare_guide_file("guide.md") is True
    assert is_bare_guide_file("guide.pdf") is True
    assert is_bare_guide_file("other.md") is False

    assert stem_for("2026-10-15/session.blocks.json") == "2026-10-15/session"
    assert stem_for("2026-10-15/session.transcript.md") == "2026-10-15/session"
    assert stem_for("2026-10-15/session.transcript.pdf") == "2026-10-15/session"
    assert stem_for("2026-10-15/session.guide.md") == "2026-10-15/session"
    assert stem_for("2026-10-15/session.guide.pdf") == "2026-10-15/session"
    assert stem_for("2026-10-15/session.json") == "2026-10-15/session"


def test_markdown_rendering():
    """Verify CommonMark HTML rendering with code blocks and tables."""
    md = """# Title

Here is a paragraph with **bold** and `inline_code`.

```bash
echo "hello world"
```

| Command | Status |
| --- | --- |
| `ls` | 0 |
"""
    html = render_markdown_html(md)
    assert "Title" in html
    assert "<b>bold</b>" in html or "<strong>bold</strong>" in html
    assert "echo &quot;hello world&quot;" in html or "hello world" in html
    assert "table" in html.lower()


def test_delete_session_files_and_empty_dirs():
    """Verify Issue 6.5: recursive session folder and parent date directory cleanup."""
    with tempfile.TemporaryDirectory() as temp_dir:
        folder = Path(temp_dir)
        date_dir = folder / "2026-10-15"
        session_dir = date_dir / "my_build_session"
        session_dir.mkdir(parents=True)

        session_file = session_dir / "session.json"
        session_file.write_text("{}", encoding="utf-8")
        blocks_file = session_dir / "session.blocks.json"
        blocks_file.write_text("{}", encoding="utf-8")
        events_file = session_dir / "session.events.jsonl"
        events_file.write_text("{}", encoding="utf-8")

        library_file = folder / ".shellby-library.json"
        write_library(str(folder), {"sessions": {"2026-10-15/my_build_session/session": {"title": "Test"}}})

        files = {
            "session": "2026-10-15/my_build_session/session.json",
            "blocks": "2026-10-15/my_build_session/session.blocks.json",
        }

        delete_session_files(str(folder), "2026-10-15/my_build_session/session", files)

        # Confirm session files are removed
        assert not session_file.exists()
        assert not blocks_file.exists()
        assert not events_file.exists()

        # Confirm empty enclosing session directory is cleaned up
        assert not session_dir.exists()

        # Confirm empty parent date directory is cleaned up
        assert not date_dir.exists()

        # Confirm library entry was removed
        lib = read_library(str(folder))
        assert "2026-10-15/my_build_session/session" not in lib["sessions"]


def test_library_crud_operations():
    with tempfile.TemporaryDirectory() as temp_dir:
        folder = temp_dir
        lib = read_library(folder)
        assert lib == {"sessions": {}}

        patch = {"title": "Updated Title", "tags": ["tag1", "tag2"], "favorite": True}
        update_session_meta(folder, "session_stem_1", patch)

        loaded = read_library(folder)
        assert "session_stem_1" in loaded["sessions"]
        assert loaded["sessions"]["session_stem_1"]["title"] == "Updated Title"
        assert loaded["sessions"]["session_stem_1"]["favorite"] is True


def test_search_and_filter_logic():
    session = {
        "stem": "2026-10-15/deploy/session",
        "sessionId": "sess_123",
        "title": "Deploy Cluster",
        "shell": "/bin/bash",
        "tags": ["prod", "docker"],
        "commands": ["docker build -t app .", "kubectl apply -f app.yaml"],
        "startedAt": "2026-10-15T12:00:00Z",
        "duration": 120,
        "commandCount": 2,
        "favorite": True,
        "mtimeMs": 1000,
    }

    assert matches_search(session, "deploy") is True
    assert matches_search(session, "docker") is True
    assert matches_search(session, "kubectl") is True
    assert matches_search(session, "nonexistent") is False

    assert within_when(session, "all") is True

    sessions = [
        {"stem": "s1", "title": "B", "startedAt": "2026-10-14T10:00:00Z", "duration": 10, "commandCount": 5, "mtimeMs": 1},
        {"stem": "s2", "title": "A", "startedAt": "2026-10-15T10:00:00Z", "duration": 50, "commandCount": 2, "mtimeMs": 2},
    ]

    by_date_desc = sort_sessions(sessions, "date-desc")
    assert by_date_desc[0]["stem"] == "s2"

    by_title_asc = sort_sessions(sessions, "title-asc")
    assert by_title_asc[0]["title"] == "A"

    by_duration_desc = sort_sessions(sessions, "duration-desc")
    assert by_duration_desc[0]["duration"] == 50


def test_markdown_rendering_guide_badges_and_cards():
    """Verify that Guide headers with [MAINTENANCE] tags render as styled badges."""
    guide_md = """# Guide Title

## Step 1: Update Package Lists [MAINTENANCE]

Refresh packages with `sudo apt update`.

```bash
sudo apt update
```

## Result
Completed.
"""
    html = render_markdown_html(guide_md, is_transcript=False)
    assert "Guide Title" in html
    assert "MAINTENANCE" in html
    assert "border-radius: 10px" in html or "border-radius" in html
    assert "sudo apt update" in html
    # Verify code is inside contiguous table card
    assert "background-color: #0f172a" in html
    assert "●" in html


def test_markdown_rendering_transcript_metadata_and_prompts():
    """Verify that Transcript metadata renders as summary card with highlighted prompts."""
    transcript_md = """# Shellby Session Transcript

- **Session ID:** 8aab9c22-7974-4672-a4f5-403586df6060
- **Shell:** /bin/bash
- **Started:** 2026-10-15T13:06:20.909443+00:00
- **Ended:** 2026-10-15T13:07:11.087096+00:00
- **Duration:** 50.177653s

---

```console
(/home/alev/AIProject) $ echo hi
hi
```
"""
    html = render_markdown_html(transcript_md, is_transcript=True)
    assert "Shellby Session Transcript" in html
    assert "8aab9c22" in html
    assert "50s" in html
    # Check prompt highlighting
    assert "(/home/alev/AIProject) $" in html
    assert "color: #818cf8" in html
    assert "echo hi" in html
    assert "background-color: #0f172a" in html


def test_create_vector_icons():
    """Verify that all UI vector icons generate valid, non-null QIcon objects."""
    import sys
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtGui import QIcon

    app = QApplication.instance()
    if app is None:
        app = QApplication(["shellby_test"])

    icon_names = [
        "record", "parse", "ai", "pdf", "pipeline", "open_pdf",
        "reveal", "delete", "star", "star_outline", "refresh", "key"
    ]
    for name in icon_names:
        icon = create_vector_icon(name, "#475569", 16)
        assert isinstance(icon, QIcon)
        assert not icon.isNull()


def test_api_key_remembering_logic():
    """Verify that Gemini API key is remembered in QSettings and can be updated/cleared."""
    from PyQt5.QtCore import QSettings
    from shellby_qt import APP_ORG, APP_NAME, ShellbyWindow
    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(["shellby_test"])

    settings = QSettings(APP_ORG, APP_NAME)
    original_key = settings.value("shellby/api_key", None)

    try:
        win = ShellbyWindow()

        # Test setting key
        win.set_api_key("AIzaSyTestKey12345")
        assert win.get_api_key() == "AIzaSyTestKey12345"
        assert settings.value("shellby/api_key") == "AIzaSyTestKey12345"
        assert os.environ.get("GEMINI_API_KEY") == "AIzaSyTestKey12345"

        # Test updating key
        win.set_api_key("AIzaSyUpdatedKey67890")
        assert win.get_api_key() == "AIzaSyUpdatedKey67890"
        assert settings.value("shellby/api_key") == "AIzaSyUpdatedKey67890"
        assert os.environ.get("GEMINI_API_KEY") == "AIzaSyUpdatedKey67890"

        # Test clearing key
        win.set_api_key(None)
        assert settings.value("shellby/api_key", "") == ""

        win.close()
    finally:
        if original_key:
            settings.setValue("shellby/api_key", original_key)
        else:
            settings.remove("shellby/api_key")


