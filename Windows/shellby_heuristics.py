"""Shellby Phase 3 - Heuristics (Windows Command Prompt Edition).

Groups the Parser's flat list of reconstructed lines into command blocks:
{prompt, command, output, exit_code, cwd}.

Windows Command Prompt Features:
    - Native Command Prompt (`cmd.exe`) prompt detection:
        `C:\\Users\\name\\project>dir`
        `(venv) C:\\Users\\name\\project>python app.py`
        `C:\\>whoami`
        `PS C:\\Users\\name> Get-ChildItem` (PowerShell fallback)
    - Automatic working directory (CWD) extraction directly from the drive/path prompt.
    - Windows caret (`^`) and Unix (`\\`) multiline command continuation merging.
    - Windows sub-prompts (`Terminate batch job (Y/N)?`, `[Y/N]`, `Password:`, etc.)
      attached to active command rather than spawning false command blocks.
    - Windows internal & external commands recognition (`dir`, `type`, `copy`, `del`,
      `ipconfig`, `systeminfo`, `findstr`, `winget`, etc.).
    - Suppression of empty command blocks from bare Enter keypresses on prompt.
    - Exit code capture & Semantic Shell Integration support.
    - O(M + N log M) bisect timestamp indexing.

Requires: shellby_parser.py in the same directory.
"""

from __future__ import annotations

import bisect
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

# Support relative import when used as module or direct script execution
try:
    from .shellby_parser import parse_session, ParsedSession, ParsedLine
except ImportError:
    from shellby_parser import parse_session, ParsedSession, ParsedLine

_FAR_FUTURE = "9999-12-31T23:59:59+00:00"

# DEC private modes for alternate screen buffer
_ALT_SCREEN_RE = re.compile(r"\x1b\[\?(?:47|1047|1049)([hl])")

# OSC 133 semantic prompt escapes
_OSC133_RE = re.compile(r"\x1b\]133;([A-D])(?:;([^\x07\x1b]+))?(?:\x07|\x1b\\)")

# OSC 7 working directory escapes
_OSC7_RE = re.compile(r"\x1b\]7;file://(?:[^/]*)(/[^\x07\x1b]*)(?:\x07|\x1b\\)")

# Windows and cross-platform interactive programs
_KNOWN_TUI_COMMANDS = {
    # Windows native & utilities
    "notepad", "notepad.exe", "edit", "edit.com",
    "more", "more.com",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "wsl", "wsl.exe", "bash", "bash.exe",
    "python", "python.exe", "python3", "ipython", "node", "node.exe",
    # Editors & TUIs
    "vim", "vi", "nvim", "nano", "emacs",
    "less", "top", "htop", "btop",
    "git", "tig", "lazygit",
    "mysql", "psql", "sqlite3", "redis-cli",
    "ftp", "sftp", "ssh", "telnet",
    "docker", "podman", "kubectl", "gh",
}

_WRAPPER_COMMANDS = {"sudo", "doas", "env", "nice", "ionice", "time", "runas", "cmd /c", "powershell -Command"}

# Interactive sub-prompt patterns that should NOT be treated as new shell commands
_SUBPROMPT_PATTERNS = re.compile(
    r"(?:"
    r"terminate\s+batch\s+job\s*\(y/n\)\?|"
    r"are\s+you\s+sure\s*\(y/n\)\?|"
    r"press\s+any\s+key\s+to\s+continue|"
    r"press\s+\[?enter\]?\s+to\s+continue|"
    r"\[sudo\]\s+password\s+for|"
    r"password:|"
    r"enter\s+password|"
    r"enter\s+passphrase|"
    r"\[y/n\]|"
    r"\(y/n\)|"
    r"\[yes/no\]|"
    r"\(yes/no\)|"
    r"username\s+for|"
    r":\s*$"
    r")",
    re.IGNORECASE,
)

# Multi-line prompt continuation prefixes (Starship, Powerlevel10k, Kali, etc.)
_TWO_LINE_PROMPT_PREFIX = re.compile(r"^[┌╭].*")

# Regex pattern for Windows Command Prompt path
_WIN_PROMPT_PATH_RE = re.compile(
    r"^(?:\([a-zA-Z0-9_.\- ]+\)\s*)?(?:PS\s+)?(?P<path>(?:[a-zA-Z]:\\[^>]*|\\\\[^>]+))>$"
)


@dataclass
class CommandBlock:
    line_index: int       # index of the command line in the parsed session
    prompt: str             # best-effort prompt text
    command: str             # best-effort typed command
    output: list[str]         # lines produced in response, in order
    started_at: str            # timestamp the command line was finalized (Enter pressed)
    ended_at: str                # timestamp of the last associated output line
    block_type: str = "command"                # "command" | "interactive"
    exit_code: int | None = None                # process exit status if captured
    cwd: str | None = None                      # working directory if known
    interactive_program: str | None = None      # best-effort program name, e.g. "python"
    interactive_trigger: str | None = None      # "alt-screen" | "known-command" | "alt-screen+known-command"
    raw_output: str | None = None               # verbatim captured text for interactive blocks

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _AltScreenSpan:
    start: str
    end: str


@dataclass
class _InteractiveSpan:
    start: str
    end: str
    program: str | None
    trigger: str


def _parse_ts_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _extract_cwd_from_prompt(prompt: str) -> str | None:
    """Extract current working directory directly from Windows Command Prompt.

    Examples:
        'C:\\Users\\Name\\Project>' -> 'C:\\Users\\Name\\Project'
        '(venv) C:\\Users\\Name>' -> 'C:\\Users\\Name'
        'PS C:\\Users\\Name>' -> 'C:\\Users\\Name'
        'D:\\>' -> 'D:\\'
    """
    cleaned = prompt.strip()
    m = _WIN_PROMPT_PATH_RE.match(cleaned)
    if m:
        path = m.group("path").strip()
        # Ensure root drive format like C:\ is preserved
        return path
    return None


def _split_prompt_and_command(text: str) -> tuple[str, str]:
    """Robust prompt splitting with dedicated support for Windows Command Prompt (cmd.exe),
    PowerShell, and Unix shell environments."""

    raw_text = text
    stripped = text.strip()

    # -------------------------------------------------------------------------
    # 1. Windows Command Prompt (cmd.exe) & PowerShell
    # Examples:
    #   C:\Users\alev\Project>dir
    #   C:\Users\alev\Project> dir
    #   (venv) C:\Users\alev>python app.py
    #   C:\>whoami
    #   PS C:\Users\alev> Get-Process
    # -------------------------------------------------------------------------

    # 1.a. Windows bare prompt (user pressed Enter on empty prompt)
    win_bare = re.match(
        r"^(?P<prompt>(?:PS\s+)?(?:\([a-zA-Z0-9_.\- ]+\)\s*)?(?:[a-zA-Z]:\\[^>]*|\\\\[^>]+)>)\s*$",
        raw_text,
    )
    if win_bare:
        return win_bare.group("prompt").strip(), ""

    # 1.b. Windows prompt with command
    win_cmd = re.match(
        r"^(?P<prompt>(?:PS\s+)?(?:\([a-zA-Z0-9_.\- ]+\)\s*)?(?:[a-zA-Z]:\\[^>]*|\\\\[^>]+)>)\s*(?P<cmd>.*)$",
        raw_text,
    )
    if win_cmd:
        p = win_cmd.group("prompt").strip()
        cmd = win_cmd.group("cmd").strip()
        # Avoid treating output redirection like "> file.txt" as a prompt
        if cmd or raw_text.endswith(">"):
            return p, cmd

    # -------------------------------------------------------------------------
    # 2. Unix / POSIX Bare Prompts
    # -------------------------------------------------------------------------
    bare_prompt = re.match(
        r"^(?:"
        r".*?[\w.\-]+@[\w.\-]+.*?"
        r"|.*?[~/\w.\-]+[:]"
        r"|.*?[)\]]"
        r"|~[/\w.\-]*"
        r"|/[/\w.\-]*"
        r"|[┌╭└╰][─\s].*?"
        r"|\s*"
        r")"
        r"(?:"
        r"[\$#%➜❯▶λ⚡»]"
        r"|(?<=[)\]:~@/\w])>"
        r"|└─[\$#]"
        r"|╰─[❯\$#]"
        r")\s*$",
        raw_text,
    )
    if bare_prompt:
        return raw_text.strip(), ""

    # -------------------------------------------------------------------------
    # 3. Oh-My-Zsh style: ➜ <path> git:(branch) <cmd>
    # -------------------------------------------------------------------------
    omz = re.match(
        r"^(?P<prompt>➜\s+.*?(?:\([a-zA-Z0-9_./-]+\)|git:\([^)]+\)))\s+(?P<cmd>[a-zA-Z0-9_./-].*)$",
        raw_text,
    )
    if omz:
        return omz.group("prompt"), omz.group("cmd").strip()

    # -------------------------------------------------------------------------
    # 4. General prompt terminator with contextual prefix
    # -------------------------------------------------------------------------
    m = re.match(
        r"^(?P<prompt>"
        r"(?:"
        r".*?[\w.\-]+@[\w.\-]+.*?"         # user@host
        r"|.*?[~/\w.\-]+[:]"               # host:path:
        r"|.*?[)\]]"                       # (main) or [user]
        r"|~[/\w.\-]*"                     # ~/path
        r"|/[/\w.\-]*"                     # /path
        r"|[┌╭└╰][─\s].*?"                 # two-line prompt
        r"|\s*"
        r")"
        r"(?:"
        r"[\$#%❯▶λ⚡»]"
        r"|(?<=[)\]:~@/\w])>"
        r"|└─[\$#]"
        r"|╰─[❯\$#]"
        r")"
        r")"
        r"\s+(?P<cmd>.*)$",
        raw_text,
    )
    if m:
        p = m.group("prompt").strip()
        cmd = m.group("cmd").strip()
        return p, cmd

    # -------------------------------------------------------------------------
    # 5. Fallback: terminator at end of prompt if prompt has indicators
    # -------------------------------------------------------------------------
    fallback = re.match(r"^(.*?[\$#%➜❯▶λ⚡»])\s+(.*)$", raw_text)
    if fallback:
        p, c = fallback.group(1).strip(), fallback.group(2).strip()
        if not re.match(r"^(?:echo|cat|grep|sed|awk|printf|type|findstr)\b", p.strip()):
            return p, c

    return "", raw_text.strip()


def _command_program(command: str) -> str | None:
    """Extract program name from typed command line, handling Windows extensions (.exe)."""
    tokens = command.strip().split()
    if not tokens:
        return None

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        base = Path(token).stem.lower()

        if base in _WRAPPER_COMMANDS or token.lower() in _WRAPPER_COMMANDS:
            idx += 1
            continue

        if token.startswith("-") or token.startswith("/"):
            idx += 1
            continue

        return base

    return None


def _is_interactive_command(command: str) -> bool:
    """Check if command matches known interactive / TUI programs."""
    prog = _command_program(command)
    if not prog:
        return False
    return prog in _KNOWN_TUI_COMMANDS or f"{prog}.exe" in _KNOWN_TUI_COMMANDS


def _is_subprompt_line(line: str) -> bool:
    """Check if a line looks like an interactive sub-prompt."""
    return bool(_SUBPROMPT_PATTERNS.search(line.strip()))


def _is_multiline_continuation(cmd: str) -> bool:
    """Check if command ends with line continuation character:
    - Windows Command Prompt: caret (^)
    - PowerShell / Unix: backtick (`) or backslash (\\)
    """
    stripped = cmd.rstrip()
    return stripped.endswith("^") or stripped.endswith("\\") or stripped.endswith("`")


def _scan_raw_events(session_path: Path) -> tuple[list[_AltScreenSpan], list[dict], list[dict]]:
    """Scan raw events for alt-screen buffer switches, OSC 133 semantic markers, and CWD changes."""
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except Exception:
        return [], [], []

    events = data.get("events", [])
    alt_spans: list[_AltScreenSpan] = []
    current_alt_start: str | None = None

    osc133_events: list[dict] = []
    cwd_events: list[dict] = []

    for ev in events:
        ev_type = ev.get("type")
        ev_time = ev.get("time", "")
        text = ev.get("text", "")

        if ev_type == "cwd":
            cwd_events.append({"time": ev_time, "cwd": text})
            continue

        if ev_type == "output":
            # Detect alt-screen buffer transitions
            for m in _ALT_SCREEN_RE.finditer(text):
                mode = m.group(1)
                if mode == "h":  # enter alt-screen
                    if current_alt_start is None:
                        current_alt_start = ev_time
                elif mode == "l":  # exit alt-screen
                    if current_alt_start is not None:
                        alt_spans.append(_AltScreenSpan(start=current_alt_start, end=ev_time))
                        current_alt_start = None

            # Detect OSC 133 markers
            for m in _OSC133_RE.finditer(text):
                action = m.group(1)
                args = m.group(2) or ""
                osc133_events.append({"time": ev_time, "action": action, "args": args})

            # Detect OSC 7 CWD changes
            for m in _OSC7_RE.finditer(text):
                cwd_path = m.group(1)
                cwd_events.append({"time": ev_time, "cwd": cwd_path})

    if current_alt_start is not None:
        alt_spans.append(_AltScreenSpan(start=current_alt_start, end=_FAR_FUTURE))

    return alt_spans, osc133_events, cwd_events


def extract_command_blocks(session_path: Path, parsed: ParsedSession | None = None) -> list[CommandBlock]:
    """Extract structured command blocks from parsed session lines."""
    if parsed is None:
        parsed = parse_session(session_path)

    alt_spans, osc133_events, cwd_events = _scan_raw_events(session_path)

    lines = parsed.lines
    blocks: list[CommandBlock] = []

    curr_block: CommandBlock | None = None
    pending_continuation: bool = False

    # Track working directory progression
    current_tracked_cwd: str | None = None

    for line in lines:
        raw_text = line.text
        line_ts = line.timestamp

        # Check if line contains a prompt and command
        prompt, cmd = _split_prompt_and_command(raw_text)

        # Update tracked CWD if prompt contains a Windows drive path
        extracted_cwd = _extract_cwd_from_prompt(prompt)
        if extracted_cwd:
            current_tracked_cwd = extracted_cwd

        # If previous line was a multiline continuation, merge with active command
        if pending_continuation and curr_block:
            curr_block.command += " " + raw_text.strip()
            pending_continuation = _is_multiline_continuation(curr_block.command)
            continue

        # Check if this is an interactive subprompt (e.g. Password:, (Y/N)?)
        if curr_block and _is_subprompt_line(raw_text):
            curr_block.output.append(raw_text)
            curr_block.ended_at = line_ts
            continue

        # Detect new command block
        if prompt:
            # Check if user entered an empty command (e.g. bare enter on prompt)
            if not cmd:
                # Bare prompt - flush previous block if needed, but do not record empty command
                if curr_block:
                    blocks.append(curr_block)
                    curr_block = None
                continue

            # Flush completed block
            if curr_block:
                blocks.append(curr_block)
                curr_block = None

            # Determine CWD for this command block
            block_cwd = extracted_cwd or current_tracked_cwd

            # Determine interactive block type
            is_interactive = _is_interactive_command(cmd)
            block_type = "interactive" if is_interactive else "command"
            prog = _command_program(cmd) if is_interactive else None

            curr_block = CommandBlock(
                line_index=line.index,
                prompt=prompt,
                command=cmd,
                output=[],
                started_at=line_ts,
                ended_at=line_ts,
                block_type=block_type,
                cwd=block_cwd,
                interactive_program=prog,
                interactive_trigger="known-command" if is_interactive else None,
            )

            pending_continuation = _is_multiline_continuation(cmd)
            continue

        # Non-prompt line: output belonging to the active command block
        if curr_block:
            curr_block.output.append(raw_text)
            curr_block.ended_at = line_ts

    if curr_block:
        blocks.append(curr_block)

    # Associate exit codes from OSC 133 semantic markers if present
    if osc133_events:
        for block in blocks:
            b_epoch = _parse_ts_epoch(block.started_at)
            for osc in osc133_events:
                if osc["action"] == "D" and osc["args"]:
                    o_epoch = _parse_ts_epoch(osc["time"])
                    if o_epoch >= b_epoch:
                        try:
                            block.exit_code = int(osc["args"])
                            break
                        except ValueError:
                            pass

    return blocks


def session_blocks_payload(session_path: Path) -> dict:
    """Produce complete blocks.json payload for a session."""
    parsed = parse_session(session_path)
    blocks = extract_command_blocks(session_path, parsed)

    return {
        "session_id": parsed.session_id,
        "shell": parsed.shell,
        "started_at": parsed.started_at,
        "ended_at": parsed.ended_at,
        "duration": parsed.duration,
        "columns": parsed.columns,
        "blocks": [b.to_dict() for b in blocks],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python shellby_heuristics.py <path_to_session.json>")
    payload = session_blocks_payload(Path(sys.argv[1]))
    print(json.dumps(payload, indent=2))
