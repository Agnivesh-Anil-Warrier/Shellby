"""Tests for shellby_markdown.py addressing issues 5.1 and 5.2."""

import json
from pathlib import Path
from shellby_markdown import render_markdown, _safe_fence


def test_safe_fence():
    """Test that safe_fence creates fences longer than inner backticks."""
    content = "Here is ```triple``` and ````quad```` backticks"
    fence = _safe_fence(content)
    assert len(fence) >= 5
    assert fence == "`````"


def test_render_markdown_with_cwd_and_exit_code(tmp_path: Path):
    """Test that Markdown export includes working directory (Issue 5.2) and exit code (Issue 5.1)."""
    blocks_file = tmp_path / "test.blocks.json"
    payload = {
        "session_id": "md-test",
        "shell": "/bin/bash",
        "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:00:10",
        "duration": 10.0,
        "blocks": [
            {
                "line_index": 0,
                "prompt": "$",
                "command": "make test",
                "cwd": "/home/user/project",
                "exit_code": 2,
                "output": ["FAILED test_one"],
            }
        ],
    }
    blocks_file.write_text(json.dumps(payload), encoding="utf-8")

    md = render_markdown(blocks_file)
    assert "(/home/user/project) $ make test" in md
    assert "[Process exited with code 2]" in md
    assert "FAILED test_one" in md
