"""Tests for shellby_heuristics.py addressing issues 3.1 through 3.8, 5.1, 5.2, and 7.1."""

import json
from pathlib import Path
from shellby_heuristics import (
    _split_prompt_and_command,
    _is_multiline_continuation,
    classify_lines,
    build_blocks,
    session_blocks_payload,
)


def test_non_greedy_prompt_splitting():
    """Test that commands with $VAR, redirections >, and #comments are not sliced (Issue 3.1)."""
    # 1. Variables
    p, cmd = _split_prompt_and_command('user@host:~$ echo "$USER is $HOME"')
    assert p == "user@host:~$"
    assert cmd == 'echo "$USER is $HOME"'

    # 2. Redirection
    p, cmd = _split_prompt_and_command('user@host:~$ echo "hello" > output.txt')
    assert p == "user@host:~$"
    assert cmd == 'echo "hello" > output.txt'

    # 3. Comment / Issue tag
    p, cmd = _split_prompt_and_command('root@server:~# git commit -m "Fix #42"')
    assert p == "root@server:~#"
    assert cmd == 'git commit -m "Fix #42"'

    # 4. Standalone redirection without prompt
    p, cmd = _split_prompt_and_command('echo "hello" > output.txt')
    assert p == ""
    assert cmd == 'echo "hello" > output.txt'


def test_modern_and_unicode_prompts():
    """Test support for Starship, Oh-My-Zsh, and two-line prompt terminators (Issue 3.2)."""
    p, cmd = _split_prompt_and_command("➜  repo git:(main) awk '{print $1, $2}'")
    assert "➜" in p
    assert cmd == "awk '{print $1, $2}'"

    p, cmd = _split_prompt_and_command("~/project ❯ docker run -d -p 80:80 nginx")
    assert "❯" in p
    assert cmd == "docker run -d -p 80:80 nginx"

    p, cmd = _split_prompt_and_command("└─$ cat file.txt")
    assert p == "└─$"
    assert cmd == "cat file.txt"


def test_multiline_continuation_detection():
    """Test detection of backslashes, quotes, and heredocs for multi-line commands (Issue 3.4)."""
    assert _is_multiline_continuation("docker run -d \\")
    assert _is_multiline_continuation('echo "hello world')
    assert _is_multiline_continuation("cat <<EOF")
    assert not _is_multiline_continuation("ls -la")


def test_two_line_prompt_and_subprompt_session(tmp_path: Path):
    """Test two-line prompt isolation (Issue 3.2), interactive sub-prompts (Issue 3.3),
    empty command suppression (Issue 3.8), and CWD tracking (Issue 5.2)."""
    session_file = tmp_path / "test_session.json"
    session_data = {
        "session_id": "test-uuid-2",
        "shell": "/bin/bash",
        "columns": 80,
        "initial_cwd": "/home/user/project",
        "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:00:10",
        "duration": 10.0,
        "events": [
            # Two line prompt: line 1
            {"time": "2026-01-01T00:00:01", "type": "output", "text": "┌──(user@host)-[~/project]\r\n"},
            # line 2 typed by user
            {"time": "2026-01-01T00:00:02", "type": "input", "text": "sudo apt update\r"},
            {"time": "2026-01-01T00:00:02", "type": "output", "text": "└─$ sudo apt update\r\n"},
            # Sudo sub-prompt (Issue 3.3)
            {"time": "2026-01-01T00:00:03", "type": "output", "text": "[sudo] password for user: \r\n"},
            {"time": "2026-01-01T00:00:03", "type": "input", "text": "secret\r"},
            {"time": "2026-01-01T00:00:04", "type": "output", "text": "Hit:1 http://archive.ubuntu.com\r\n"},
            # Bare enter (Issue 3.8)
            {"time": "2026-01-01T00:00:05", "type": "output", "text": "└─$ \r\n"},
            {"time": "2026-01-01T00:00:05", "type": "input", "text": "\r"},
            # Next command with CWD change (Issue 5.2)
            {"time": "2026-01-01T00:00:06", "type": "cwd", "text": "/home/user/project/build"},
            {"time": "2026-01-01T00:00:07", "type": "input", "text": "make\r"},
            {"time": "2026-01-01T00:00:07", "type": "output", "text": "└─$ make\r\nBuild succeeded\r\n"},
        ],
    }
    session_file.write_text(json.dumps(session_data), encoding="utf-8")

    blocks = build_blocks(session_file)

    # Should have 2 blocks: 'sudo apt update' and 'make' (bare enter filtered out!)
    assert len(blocks) == 2

    # Block 1: sudo
    b1 = blocks[0]
    assert "sudo apt update" in b1.command
    assert "┌──" in b1.prompt  # Prompt line 1 merged into prompt, not previous output!
    assert any("[sudo] password" in out for out in b1.output)  # Sub-prompt kept inside block output!
    assert b1.cwd == "/home/user/project"

    # Block 2: make
    b2 = blocks[1]
    assert "make" in b2.command
    assert b2.cwd == "/home/user/project/build"
    assert "Build succeeded" in b2.output


def test_osc133_exit_codes(tmp_path: Path):
    """Test Semantic Shell Integration (OSC 133) and exit status extraction (Issues 5.1 & 7.1)."""
    session_file = tmp_path / "test_osc133.json"
    session_data = {
        "session_id": "test-osc",
        "shell": "/bin/bash",
        "columns": 80,
        "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:00:10",
        "duration": 10.0,
        "events": [
            {"time": "2026-01-01T00:00:01", "type": "output", "text": "\x1b]133;A\x07user@host:~$ \x1b]133;B\x07"},
            {"time": "2026-01-01T00:00:02", "type": "input", "text": "false\r"},
            {"time": "2026-01-01T00:00:02", "type": "output", "text": "false\r\n\x1b]133;C\x07\x1b]133;D;1\x07"},
        ],
    }
    session_file.write_text(json.dumps(session_data), encoding="utf-8")

    payload = session_blocks_payload(session_file)
    blocks = payload["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["command"] == "false"
    assert blocks[0]["exit_code"] == 1
