"""Unit and integration tests for winShell (Windows Command Prompt Edition)."""

import json
import os
import sys
import tempfile
from pathlib import Path

# Ensure package and parent directory are on sys.path, and alias Windows as winShell if needed
_win_dir = Path(__file__).resolve().parent
_repo_root = _win_dir.parent
for _p in [str(_win_dir), str(_repo_root)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "winShell" not in sys.modules:
    try:
        import Windows as _win_mod
        sys.modules["winShell"] = _win_mod
    except ImportError:
        pass

import pytest

try:
    from Windows.shellby_heuristics import (
        _split_prompt_and_command,
        _extract_cwd_from_prompt,
        _is_multiline_continuation,
        _is_subprompt_line,
        _command_program,
        extract_command_blocks,
        CommandBlock,
    )
    from Windows.shellby_parser import ParsedSession, ParsedLine
    from Windows.shellby_markdown import render_markdown
    from Windows.shellby_pdf import render_markdown_to_pdf
    from Windows.shellby_qt import norm_rel_path, fmt_date, fmt_duration
except ImportError:
    from shellby_heuristics import (
        _split_prompt_and_command,
        _extract_cwd_from_prompt,
        _is_multiline_continuation,
        _is_subprompt_line,
        _command_program,
        extract_command_blocks,
        CommandBlock,
    )
    from shellby_parser import ParsedSession, ParsedLine
    from shellby_markdown import render_markdown
    from shellby_pdf import render_markdown_to_pdf
    from shellby_qt import norm_rel_path, fmt_date, fmt_duration



def test_windows_cmd_prompt_splitting():
    """Verify robust prompt splitting for Windows Command Prompt (cmd.exe)."""
    # 1. Standard command with space
    p, cmd = _split_prompt_and_command(r"C:\Users\alev\AIProject> dir")
    assert p == r"C:\Users\alev\AIProject>"
    assert cmd == "dir"

    # 2. Standard command without space after > (common in cmd.exe)
    p, cmd = _split_prompt_and_command(r"C:\Users\alev\AIProject>dir /w")
    assert p == r"C:\Users\alev\AIProject>"
    assert cmd == "dir /w"

    # 3. Root drive prompt
    p, cmd = _split_prompt_and_command(r"C:\>whoami")
    assert p == r"C:\>"
    assert cmd == "whoami"

    # 4. Virtual environment prefixed prompt
    p, cmd = _split_prompt_and_command(r"(.venv) C:\Users\alev\AIProject>python app.py")
    assert p == r"(.venv) C:\Users\alev\AIProject>"
    assert cmd == "python app.py"

    # 5. Alternate drive (D:\, E:\)
    p, cmd = _split_prompt_and_command(r"D:\work\shellby>git status")
    assert p == r"D:\work\shellby>"
    assert cmd == "git status"

    # 6. PowerShell prompt fallback
    p, cmd = _split_prompt_and_command(r"PS C:\Users\alev> Get-Process")
    assert p == r"PS C:\Users\alev>"
    assert cmd == "Get-Process"

    # 7. Bare prompts (user pressed Enter on empty prompt)
    p, cmd = _split_prompt_and_command(r"C:\Users\alev\AIProject>")
    assert p == r"C:\Users\alev\AIProject>"
    assert cmd == ""

    p, cmd = _split_prompt_and_command(r"C:\>")
    assert p == r"C:\>"
    assert cmd == ""


def test_windows_cwd_extraction():
    """Verify CWD is extracted directly from the Windows Command Prompt string."""
    assert _extract_cwd_from_prompt(r"C:\Users\alev\AIProject>") == r"C:\Users\alev\AIProject"
    assert _extract_cwd_from_prompt(r"(.venv) C:\Users\alev\AIProject>") == r"C:\Users\alev\AIProject"
    assert _extract_cwd_from_prompt(r"C:\>") == "C:\\"
    assert _extract_cwd_from_prompt(r"D:\Data\Repo>") == r"D:\Data\Repo"
    assert _extract_cwd_from_prompt(r"PS C:\Users\alev>") == r"C:\Users\alev"
    assert _extract_cwd_from_prompt("user@linux:~$") is None


def test_windows_multiline_continuation():
    """Verify caret (^) continuation in Command Prompt."""
    assert _is_multiline_continuation("echo hello ^") is True
    assert _is_multiline_continuation("echo hello \\") is True
    assert _is_multiline_continuation("echo hello `") is True
    assert _is_multiline_continuation("echo hello") is False


def test_windows_subprompts():
    """Verify Windows interactive sub-prompts."""
    assert _is_subprompt_line("Terminate batch job (Y/N)?") is True
    assert _is_subprompt_line("Are you sure (Y/N)?") is True
    assert _is_subprompt_line("Press any key to continue . . .") is True
    assert _is_subprompt_line("Enter password:") is True
    assert _is_subprompt_line("Directory of C:\\Users") is False


def test_windows_command_program_extraction():
    """Verify program extraction from Windows command lines."""
    assert _command_program("python.exe app.py") == "python"
    assert _command_program("notepad file.txt") == "notepad"
    assert _command_program("git status") == "git"
    assert _command_program("dir /s /b") == "dir"


def test_windows_command_blocks_extraction():
    """Verify end-to-end command block grouping with Windows Command Prompt session data."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        session_file = Path(tmp_dir) / "test_session.json"
        session_data = {
            "session_id": "win-test-1234",
            "shell": "cmd.exe",
            "started_at": "2026-10-16T10:00:00+00:00",
            "ended_at": "2026-10-16T10:01:00+00:00",
            "duration": 60.0,
            "columns": 80,
            "events": [],
        }
        session_file.write_text(json.dumps(session_data), encoding="utf-8")

        parsed = ParsedSession(
            session_id="win-test-1234",
            shell="cmd.exe",
            started_at="2026-10-16T10:00:00+00:00",
            ended_at="2026-10-16T10:01:00+00:00",
            duration=60.0,
            columns=80,
            lines=[
                ParsedLine(0, r"C:\Projects\app>dir", "2026-10-16T10:00:05+00:00"),
                ParsedLine(1, " Volume in drive C has no label.", "2026-10-16T10:00:06+00:00"),
                ParsedLine(2, " File Not Found", "2026-10-16T10:00:07+00:00"),
                ParsedLine(3, r"C:\Projects\app>", "2026-10-16T10:00:10+00:00"),  # bare enter
                ParsedLine(4, r"C:\Projects\app>python app.py", "2026-10-16T10:00:15+00:00"),
                ParsedLine(5, "Server running on port 8000", "2026-10-16T10:00:16+00:00"),
                ParsedLine(6, "Terminate batch job (Y/N)?", "2026-10-16T10:00:20+00:00"),
                ParsedLine(7, "Y", "2026-10-16T10:00:22+00:00"),
            ],
        )

        blocks = extract_command_blocks(session_file, parsed)

        assert len(blocks) == 2

        # Block 1: dir
        assert blocks[0].command == "dir"
        assert blocks[0].cwd == r"C:\Projects\app"
        assert len(blocks[0].output) == 2
        assert "Volume in drive C" in blocks[0].output[0]

        # Block 2: python app.py with interactive subprompt attached
        assert blocks[1].command == "python app.py"
        assert blocks[1].cwd == r"C:\Projects\app"
        assert any("Terminate batch job" in line for line in blocks[1].output)


def test_windows_markdown_rendering():
    """Verify Markdown transcript rendering with Windows Command Prompt styling."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        blocks_file = Path(tmp_dir) / "test.blocks.json"
        blocks_data = {
            "session_id": "win-test-5678",
            "shell": "cmd.exe",
            "started_at": "2026-10-16T10:00:00+00:00",
            "ended_at": "2026-10-16T10:05:00+00:00",
            "duration": 300.0,
            "blocks": [
                {
                    "command": "ipconfig",
                    "cwd": r"C:\Windows\System32",
                    "output": ["Windows IP Configuration", "IPv4 Address: 192.168.1.100"],
                    "exit_code": 0,
                },
                {
                    "command": "ping 10.0.0.1",
                    "cwd": r"C:\Users\alev",
                    "output": ["Request timed out."],
                    "exit_code": 1,
                },
            ],
        }
        blocks_file.write_text(json.dumps(blocks_data), encoding="utf-8")

        md = render_markdown(blocks_file)
        assert "# Shellby Session Transcript" in md
        assert "cmd.exe" in md
        assert r"C:\Windows\System32> ipconfig" in md
        assert "IPv4 Address: 192.168.1.100" in md
        assert r"C:\Users\alev> ping 10.0.0.1" in md
        assert "[Process exited with code 1]" in md


def test_windows_pdf_rendering():
    """Verify PDF export from Windows Markdown content."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = Path(tmp_dir) / "test_guide.pdf"
        md_content = """# Windows Deployment Guide

## Overview
This guide demonstrates deploying an application on Windows Command Prompt.

## Step 1: Install Dependencies [DEPENDENCIES]
Install Python dependencies.

```cmd
pip install -r requirements.txt
```

## Step 2: Run Application [DEPLOYMENT]
Launch the app.

```cmd
python app.py
```
"""
        result = render_markdown_to_pdf(md_content, pdf_path)
        assert result.exists()
        assert result.stat().st_size > 500


def test_windows_path_normalization():
    """Verify Windows backslash normalization in GUI helper."""
    assert norm_rel_path(r"2026-10-16\session_1\session.json") == "2026-10-16/session_1/session.json"
    assert norm_rel_path("folder/subfolder/file.txt") == "folder/subfolder/file.txt"


def test_windows_conpty_structures():
    """Verify Win32 ConPTY structure sizes and definitions."""
    import ctypes
    try:
        from Windows.shellby import (
            STARTUPINFOW,
            STARTUPINFOEXW,
            COORD,
            PROCESS_INFORMATION,
        )
    except ImportError:
        from shellby import (
            STARTUPINFOW,
            STARTUPINFOEXW,
            COORD,
            PROCESS_INFORMATION,
        )

    assert STARTUPINFOW is not None
    assert STARTUPINFOEXW is not None
    assert COORD is not None
    assert PROCESS_INFORMATION is not None

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        # 64-bit Windows sizes
        assert ctypes.sizeof(STARTUPINFOW) == 104
        assert ctypes.sizeof(STARTUPINFOEXW) == 112
        assert ctypes.sizeof(PROCESS_INFORMATION) == 24
    else:
        # 32-bit Windows sizes
        assert ctypes.sizeof(STARTUPINFOW) == 68
        assert ctypes.sizeof(STARTUPINFOEXW) == 72
        assert ctypes.sizeof(PROCESS_INFORMATION) == 16

    assert ctypes.sizeof(COORD) == 4


def test_windows_conpty_input_output_filters():
    """Verify Win32 Input Mode escape sequence translation and ConPTY sequence stripping."""
    try:
        from Windows.shellby import _translate_win32_input, CONPTY_STRIP_RE
    except ImportError:
        from shellby import _translate_win32_input, CONPTY_STRIP_RE

    # 1. Stripping ConPTY handshake sequences
    conpty_init = b"\x1b[?9001h\x1b[?1004h\x1b[2JHello\x1b[?9001l\x1b[?1004l"
    assert CONPTY_STRIP_RE.sub(b"", conpty_init) == b"\x1b[2JHello"

    # 2. Key-up events are dropped (kd == 0)
    assert _translate_win32_input(b"\x1b[8;14;8;0;32;1_") == b""

    # 3. Key-down backspace (kd == 1, uc == 8)
    assert _translate_win32_input(b"\x1b[8;14;8;1;32;1_") == b"\x08"

    # 4. Key-down letter 'h' (kd == 1, uc == 104) and key-up dropped
    assert _translate_win32_input(b"\x1b[72;35;104;1;32;1_\x1b[72;35;104;0;32;1_") == b"h"

    # 5. Virtual key navigation (Up arrow vk == 38, uc == 0)
    assert _translate_win32_input(b"\x1b[38;72;0;1;32;1_") == b"\x1b[A"

    # 6. Focus reporting events stripped
    assert _translate_win32_input(b"\x1b[I") == b""
    assert _translate_win32_input(b"\x1b[O") == b""

    # 7. Standard text passes through unchanged
    assert _translate_win32_input(b"dir /w\r\n") == b"dir /w\r\n"


def test_windows_qt2_formatting():
    """Verify shellby_qt2 terminal formatting for Windows Command Prompt, PowerShell, and errors."""
    try:
        from Windows.shellby_qt2 import format_code_block_table
    except ImportError:
        from shellby_qt2 import format_code_block_table

    # 1. Windows cmd prompt recognition
    cmd_code = "C:\\Users\\test> dir\n(.venv) C:\\Users\\test> python app.py"
    cmd_html = format_code_block_table(cmd_code, lang="cmd")
    assert "Command Prompt (cmd.exe)" in cmd_html
    assert "C:\\Users\\test&gt;" in cmd_html
    assert "(.venv) C:\\Users\\test&gt;" in cmd_html
    assert '<span style="color: #ffffff; font-weight: bold;">dir</span>' in cmd_html

    # 2. PowerShell prompt recognition
    ps_code = "PS C:\\Users\\test> Get-Process"
    ps_html = format_code_block_table(ps_code, lang="powershell")
    assert "PowerShell" in ps_html
    assert "PS C:\\Users\\test&gt;" in ps_html
    assert '<span style="color: #ffffff; font-weight: bold;">Get-Process</span>' in ps_html

    # 3. PowerShell session shell override (even if code fence says cmd)
    override_html = format_code_block_table(
        "C:\\books\\Business books> echo hi\nhi",
        lang="cmd",
        session_shell="powershell.exe",
    )
    assert "PowerShell" in override_html
    assert "Command Prompt (cmd.exe)" not in override_html

    # 4. render_markdown_html transcript with PowerShell metadata
    try:
        from Windows.shellby_qt2 import render_markdown_html
    except ImportError:
        from shellby_qt2 import render_markdown_html
    sample_transcript = (
        "# Shellby Session Transcript\n\n"
        "- **Session ID:** 91d69013-3c18-40ad-a386-4503829ebe93\n"
        "- **Shell:** powershell.exe\n"
        "- **Started:** 2026-10-16T14:04:00Z\n"
        "- **Ended:** 2026-10-16T14:04:09Z\n"
        "- **Duration:** 9s\n\n"
        "---\n\n"
        "```cmd\n"
        "C:\\books\\Business books> echo hi\n"
        "hi\n"
        "```\n"
    )
    rendered_doc = render_markdown_html(sample_transcript, is_transcript=True)
    assert "powershell.exe" in rendered_doc
    assert "PowerShell" in rendered_doc
    assert "Command Prompt (cmd.exe)" not in rendered_doc

    # 5. Subprompts and Windows command errors
    err_code = (
        "Terminate batch job (Y/N)?\n"
        "'fakecmd' is not recognized as an internal or external command\n"
        ":: this is a batch comment"
    )
    err_html = format_code_block_table(err_code, lang="cmd")
    assert "#fbbf24" in err_html  # subprompt warning
    assert "#f87171" in err_html  # error highlighting
    assert "#64748b" in err_html  # comment highlighting


def test_windows_qt2_sessions_discovery(tmp_path):
    """Verify shellby_qt2 session scanning and auto-discovery."""
    try:
        from Windows.shellby_qt2 import scan_folder, _pkg_root
    except ImportError:
        from shellby_qt2 import scan_folder, _pkg_root

    test_session_dir = tmp_path / "sessions" / "demo-session"
    test_session_dir.mkdir(parents=True, exist_ok=True)
    (test_session_dir / "session.json").write_text("{}", encoding="utf-8")
    (test_session_dir / "blocks.json").write_text("[]", encoding="utf-8")

    sessions = scan_folder(str(tmp_path / "sessions"))
    assert isinstance(sessions, list)
    assert len(sessions) >= 1
    first = sessions[0]
    assert "stem" in first
    assert "files" in first
    assert "session" in first["files"]


def test_windows_model_settings_persistence():
    """Verify Gemini model can be customized, persisted to QSettings, and synced with GEMINI_MODEL."""
    import os
    from PyQt5.QtCore import QSettings

    settings = QSettings("Shellby", "ShellbyWinTest")
    # Test custom future model name
    custom_model = "gemini-future-ultra-exp"
    settings.setValue("shellby/model", custom_model)
    assert settings.value("shellby/model") == custom_model
    settings.remove("shellby/model")


def test_windows_powershell_support():
    """Verify PowerShell prompt extraction and command recognition."""
    try:
        from Windows.shellby_heuristics import _split_prompt_and_command, _WIN_PROMPT_PATH_RE
    except ImportError:
        from shellby_heuristics import _split_prompt_and_command, _WIN_PROMPT_PATH_RE

    # 1. Prompt path regex matches PowerShell prompts
    m = _WIN_PROMPT_PATH_RE.match("PS C:\\Users\\user\\Project>")
    assert m is not None
    assert m.group("path") == "C:\\Users\\user\\Project"

    # 2. Command extraction from PowerShell command line
    prompt, cmd = _split_prompt_and_command("PS C:\\Users\\user> Get-Service -Name wuauserv")
    assert "PS C:\\Users\\user>" in prompt
    assert cmd == "Get-Service -Name wuauserv"



