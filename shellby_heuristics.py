"""Shellby Phase 3 - Heuristics.

Groups the Parser's flat list of reconstructed lines into command blocks:
{prompt, command, output, exit_code, cwd}.

Rectifications:
    - Non-greedy, robust prompt splitting that preserves command variables ($VAR),
      redirections (> file), comments (#tag), and format strings (Issue 3.1).
    - Modern prompt glyphs (➜, ❯, ▶, λ, ⚡, », └─$, ╰─❯) and multi-line prompt
      handling so first-line prompt context is not dumped into previous outputs (Issue 3.2).
    - Interactive command input & sub-prompts (sudo, ssh, [y/N], REPLs) attached to
      active command rather than spawning fake command blocks (Issue 3.3).
    - Multi-line command & line-wrapping continuation merging (Issue 3.4).
    - O(M + N log M) bisect timestamp indexing replacing quadratic O(N*M) string parsing (Issue 3.5).
    - Expanded interactive TUI commands list and unclosed span bounds (Issue 3.6).
    - Informative interactive session placeholders with arguments & duration (Issue 3.7).
    - Suppression of empty command blocks from bare Enter keypresses (Issue 3.8).
    - Exit code capture & Semantic Shell Integration (OSC 133) support (Issues 5.1 & 7.1).
    - Working directory (CWD) association per command block (Issue 5.2).

Requires: shellby_parser.py (Phase 2) in the same directory.
"""

from __future__ import annotations

import bisect
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from shellby_parser import parse_session, ParsedSession, ParsedLine

_FAR_FUTURE = "9999-12-31T23:59:59+00:00"

# DEC private modes for alternate screen buffer
_ALT_SCREEN_RE = re.compile(r"\x1b\[\?(?:47|1047|1049)([hl])")

# OSC 133 semantic prompt escapes
_OSC133_RE = re.compile(r"\x1b\]133;([A-D])(?:;([^\x07\x1b]+))?(?:\x07|\x1b\\)")

# OSC 7 working directory escapes
_OSC7_RE = re.compile(r"\x1b\]7;file://(?:[^/]*)(/[^\x07\x1b]*)(?:\x07|\x1b\\)")

# Expanded table of full-screen / interactive programs (Issue 3.6)
_KNOWN_TUI_COMMANDS = {
    "vim", "vi", "nvim", "view", "nvi",
    "nano", "pico", "emacs",
    "less", "more", "most", "man",
    "top", "htop", "btop", "atop", "glances", "iotop", "iftop", "nethogs",
    "watch",
    "tmux", "screen",
    "mc", "ranger", "nnn", "lf", "vifm",
    "tig", "lazygit", "lazydocker", "k9s",
    "ncdu",
    "nmtui", "alsamixer",
    "fzf",
    "python", "python3", "ipython", "node",
    "gdb", "lldb",
    "mysql", "psql", "sqlite3", "redis-cli",
    "sftp", "docker", "podman", "kubectl", "gh",
}

_WRAPPER_COMMANDS = {"sudo", "doas", "env", "nice", "ionice", "time"}

# Interactive sub-prompt patterns that should NOT be treated as new shell commands (Issue 3.3)
_SUBPROMPT_PATTERNS = re.compile(
    r"(?:"
    r"\[sudo\]\s+password\s+for|"
    r"password:|"
    r"enter\s+passphrase|"
    r"\[y/n\]|"
    r"\(yes/no\)|"
    r"username\s+for|"
    r"press\s+\[?enter\]?\s+to\s+continue|"
    r":\s*$"
    r")",
    re.IGNORECASE,
)

# Multi-line prompt continuation prefixes (Starship, Powerlevel10k, Kali, etc.)
_TWO_LINE_PROMPT_PREFIX = re.compile(r"^[┌╭].*")


@dataclass
class CommandBlock:
    line_index: int       # index of the command line in the parsed session
    prompt: str             # best-effort prompt text
    command: str             # best-effort typed command
    output: list[str]         # lines produced in response, in order
    started_at: str            # timestamp the command line was finalized (Enter pressed)
    ended_at: str                # timestamp of the last associated output line
    block_type: str = "command"                # "command" | "interactive"
    exit_code: int | None = None                # process exit status if captured (Issue 5.1 & 7.1)
    cwd: str | None = None                      # working directory if known (Issue 5.2)
    interactive_program: str | None = None      # best-effort program name, e.g. "vim"
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


def _split_prompt_and_command(text: str) -> tuple[str, str]:
    """Non-greedy prompt splitting supporting modern unicode symbols,
    while preserving variables ($VAR), redirections (> file), and comments (Issue 3.1 & 3.2)."""

    # 1. Bare prompt line without command (e.g. user pressed enter on empty prompt)
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
        text,
    )
    if bare_prompt:
        return text.strip(), ""

    # 2. Oh-My-Zsh style: ➜ <path> git:(branch) <cmd>
    omz = re.match(
        r"^(?P<prompt>➜\s+.*?(?:\([a-zA-Z0-9_./-]+\)|git:\([^)]+\)))\s+(?P<cmd>[a-zA-Z0-9_./-].*)$",
        text,
    )
    if omz:
        return omz.group("prompt"), omz.group("cmd")

    # 3. General prompt terminator with contextual prefix
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
        text,
    )
    if m:
        p = m.group("prompt")
        cmd = m.group("cmd")
        return p, cmd

    # 4. Fallback: terminator at end of prompt if prompt has indicators
    fallback = re.match(r"^(.*?[\$#%➜❯▶λ⚡»])\s+(.*)$", text)
    if fallback:
        p, c = fallback.group(1), fallback.group(2)
        if not re.match(r"^(?:echo|cat|grep|sed|awk|printf)\b", p.strip()):
            return p, c

    return "", text


def _command_program(command: str) -> str | None:
    """Extract program name from typed command line."""
    tokens = command.strip().split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        if tok in _WRAPPER_COMMANDS:
            i += 1
            continue
        return Path(tok).name
    return None


def _is_multiline_continuation(cmd: str) -> bool:
    """Detect if command expects continuation on next line (Issue 3.4)."""
    trimmed = cmd.rstrip()
    if trimmed.endswith("\\"):
        return True
    if re.search(r"<<\s*['\"]?([A-Za-z0-9_]+)['\"]?", trimmed):
        return True
    # Odd count of unescaped double or single quotes
    unescaped_double = len(re.findall(r'(?<!\\)"', trimmed)) % 2 != 0
    unescaped_single = len(re.findall(r"(?<!\\)'", trimmed)) % 2 != 0
    return unescaped_double or unescaped_single


def classify_lines(
    session_path: Path, raw: dict | None = None
) -> tuple[ParsedSession, list[bool]]:
    """O(M + N log M) line classifier using bisect over pre-parsed timestamps (Issue 3.5)."""
    if raw is None:
        raw = json.loads(session_path.read_text(encoding="utf-8"))

    # Pre-parse input timestamps once into sorted float array (Issue 3.5)
    input_ts = [
        _parse_ts_epoch(e["time"])
        for e in raw.get("events", [])
        if e.get("type") == "input" and "time" in e
    ]
    input_ts.sort()

    parsed = parse_session(session_path)

    is_command: list[bool] = []
    prev_epoch = _parse_ts_epoch(raw.get("started_at", ""))

    for line in parsed.lines:
        curr_epoch = _parse_ts_epoch(line.timestamp)
        if curr_epoch > prev_epoch and input_ts:
            idx_start = bisect.bisect_right(input_ts, prev_epoch)
            idx_end = bisect.bisect_right(input_ts, curr_epoch)
            has_input = idx_end > idx_start
        else:
            has_input = False

        is_command.append(has_input)
        prev_epoch = curr_epoch

    return parsed, is_command


def _find_alt_screen_spans(events: list[dict]) -> list[_AltScreenSpan]:
    """Scan raw output events for DEC alt-screen-buffer escape codes."""
    output_events = [e for e in events if e.get("type") == "output"]
    if not output_events:
        return []

    offsets: list[int] = []
    parts: list[str] = []
    pos = 0
    for ev in output_events:
        offsets.append(pos)
        text = ev.get("text", "")
        parts.append(text)
        pos += len(text)
    full_text = "".join(parts)

    def _time_at(offset: int) -> str:
        idx = bisect.bisect_right(offsets, offset) - 1
        idx = max(0, min(idx, len(output_events) - 1))
        return output_events[idx]["time"]

    spans: list[_AltScreenSpan] = []
    depth = 0
    open_start: str | None = None

    for m in _ALT_SCREEN_RE.finditer(full_text):
        mode = m.group(1)
        t = _time_at(m.start())
        if mode == "h":
            if depth == 0:
                open_start = t
            depth += 1
        else:
            if depth > 0:
                depth -= 1
                if depth == 0 and open_start is not None:
                    spans.append(_AltScreenSpan(start=open_start, end=t))
                    open_start = None

    if depth > 0 and open_start is not None:
        spans.append(_AltScreenSpan(start=open_start, end=""))

    return spans


def _label_alt_screen_spans(
    alt_spans: list[_AltScreenSpan], blocks: list[CommandBlock]
) -> list[_InteractiveSpan]:
    labeled: list[_InteractiveSpan] = []
    for a in alt_spans:
        program: str | None = None
        for block in blocks:
            if block.started_at <= a.start:
                program = _command_program(block.command) or program
            else:
                break
        labeled.append(
            _InteractiveSpan(start=a.start, end=a.end, program=program, trigger="alt-screen")
        )
    return labeled


def _find_known_command_spans(
    blocks: list[CommandBlock], alt_spans: list[_AltScreenSpan]
) -> list[_InteractiveSpan]:
    """Identify interactive sessions from known commands, preventing unclosed span leakage (Issue 3.6)."""
    spans: list[_InteractiveSpan] = []

    for i, block in enumerate(blocks):
        program = _command_program(block.command)
        if program not in _KNOWN_TUI_COMMANDS:
            continue
        if any(a.start <= block.started_at < (a.end or _FAR_FUTURE) for a in alt_spans):
            continue

        end = ""
        for later in blocks[i + 1 :]:
            if later.prompt or later.exit_code is not None:
                end = later.started_at
                break

        spans.append(
            _InteractiveSpan(start=block.started_at, end=end, program=program, trigger="known-command")
        )

    return spans


def _merge_spans(spans: list[_InteractiveSpan]) -> list[_InteractiveSpan]:
    if not spans:
        return []

    ordered = sorted(spans, key=lambda s: s.start)
    merged = [ordered[0]]

    for s in ordered[1:]:
        last = merged[-1]
        last_end = last.end or _FAR_FUTURE
        if s.start <= last_end:
            new_end = "" if (not last.end or not s.end) else max(last.end, s.end)
            triggers = sorted(set(last.trigger.split("+")) | set(s.trigger.split("+")))
            merged[-1] = _InteractiveSpan(
                start=last.start,
                end=new_end,
                program=last.program or s.program,
                trigger="+".join(triggers),
            )
        else:
            merged.append(s)

    return merged


def _collapse_interactive_spans(
    blocks: list[CommandBlock], spans: list[_InteractiveSpan], events: list[dict]
) -> list[CommandBlock]:
    """Fold blocks inside interactive spans into informative blocks (Issue 3.7)."""
    if not spans:
        return blocks

    output_events = [e for e in events if e.get("type") == "output"]

    def _raw_text_in(start: str, end: str) -> str:
        end_key = end or _FAR_FUTURE
        return "".join(e.get("text", "") for e in output_events if start <= e["time"] < end_key)

    result: list[CommandBlock] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]

        span = next(
            (s for s in spans if s.start <= block.started_at < (s.end or _FAR_FUTURE)),
            None,
        )

        if span is None:
            result.append(block)
            i += 1
            continue

        end_key = span.end or _FAR_FUTURE
        group: list[CommandBlock] = []
        while i < len(blocks) and span.start <= blocks[i].started_at < end_key:
            group.append(blocks[i])
            i += 1

        span_end = span.end or (group[-1].ended_at if group else span.start)
        raw_output = _raw_text_in(span.start, span_end)

        duration_note = ""
        try:
            secs = _parse_ts_epoch(span_end) - _parse_ts_epoch(span.start)
            if secs > 0:
                duration_note = f", {secs:.1f}s"
        except Exception:
            pass

        program_label = span.program or _command_program(group[0].command if group else "") or "interactive tool"
        cmd_summary = group[0].command if group else program_label

        placeholder = (
            f"[interactive session: {cmd_summary}{duration_note} "
            f"({span.trigger}) -- output omitted; verbatim capture preserved in raw_output]"
        )

        result.append(
            CommandBlock(
                line_index=group[0].line_index,
                prompt=group[0].prompt,
                command=group[0].command,
                output=[placeholder],
                started_at=span.start,
                ended_at=span_end,
                block_type="interactive",
                exit_code=group[0].exit_code if group else None,
                cwd=group[0].cwd if group else None,
                interactive_program=program_label,
                interactive_trigger=span.trigger,
                raw_output=raw_output,
            )
        )

    return result


def _find_active_cwd(timestamp: str, cwd_events: list[dict], default_cwd: str = "") -> str:
    """Find current working directory effective at a given timestamp (Issue 5.2)."""
    if not cwd_events:
        return default_cwd
    active = default_cwd
    for ev in cwd_events:
        if ev.get("time", "") <= timestamp:
            active = ev.get("text", active)
        else:
            break
    return active


def build_blocks(session_path: Path) -> list[CommandBlock]:
    """Reconstruct command blocks with prompt handling, multi-line merging,
    exit codes, and CWD tracking."""
    raw = json.loads(session_path.read_text(encoding="utf-8"))
    events = raw.get("events", [])
    initial_cwd = raw.get("initial_cwd", "")

    # Extract CWD events
    cwd_events = [e for e in events if e.get("type") == "cwd"]

    # Check for Semantic Shell Integration OSC 133 (Issue 7.1)
    full_output = "".join(e.get("text", "") for e in events if e.get("type") == "output")
    has_osc133 = bool(_OSC133_RE.search(full_output))

    parsed, is_command = classify_lines(session_path, raw=raw)

    blocks: list[CommandBlock] = []
    current: CommandBlock | None = None
    prev_prompt_line1: str | None = None

    for idx, (line, cmd_flag) in enumerate(zip(parsed.lines, is_command)):
        line_text = line.text

        # Detect two-line prompt header (Issue 3.2)
        if _TWO_LINE_PROMPT_PREFIX.match(line_text.strip()):
            prev_prompt_line1 = line_text.strip()
            # If current command was accumulating output, do not pollute it
            continue

        if cmd_flag:
            prompt, command = _split_prompt_and_command(line_text)

            # Check if this is an interactive sub-prompt (Issue 3.3)
            is_subprompt = bool(_SUBPROMPT_PATTERNS.search(line_text))
            if not prompt and is_subprompt and current is not None:
                current.output.append(line_text)
                current.ended_at = line.timestamp
                continue

            # Check multi-line command continuation (Issue 3.4)
            if current is not None and _is_multiline_continuation(current.command):
                continuation_cmd = command if prompt else line_text
                # Strip trailing backslash from previous line
                current.command = current.command.rstrip().rstrip("\\").rstrip() + "\n" + continuation_cmd.strip()
                current.ended_at = line.timestamp
                continue

            # Merge previous prompt line 1 if present (Issue 3.2)
            if prev_prompt_line1:
                prompt = f"{prev_prompt_line1}\n{prompt}" if prompt else prev_prompt_line1
                prev_prompt_line1 = None

            # Skip empty commands from bare Enter keypresses (Issue 3.8)
            if not command.strip():
                continue

            if current is not None:
                if current.command.strip():
                    blocks.append(current)
                elif current.output:
                    blocks.append(current)

            active_cwd = _find_active_cwd(line.timestamp, cwd_events, initial_cwd)

            current = CommandBlock(
                line_index=line.index,
                prompt=prompt,
                command=command,
                output=[],
                started_at=line.timestamp,
                ended_at=line.timestamp,
                cwd=active_cwd or None,
            )
        else:
            prev_prompt_line1 = None
            if current is None:
                continue
            is_trailing_blank = (line is parsed.lines[-1] and line.text == "")
            if not is_trailing_blank:
                current.output.append(line.text)
            current.ended_at = line.timestamp

    if current is not None and (current.command.strip() or current.output):
        blocks.append(current)

    # Extract exit codes from OSC 133;D if available (Issue 5.1 & 7.1)
    if has_osc133:
        exit_codes: list[tuple[str, int]] = []
        for m in _OSC133_RE.finditer(full_output):
            if m.group(1) == "D":
                code_str = m.group(2)
                code_val = int(code_str) if code_str and code_str.isdigit() else 0
                exit_codes.append((m.group(0), code_val))

        # Assign exit codes sequentially to command blocks
        for b, (_, code) in zip(blocks, exit_codes):
            b.exit_code = code

    alt_spans = _find_alt_screen_spans(events)
    interactive_spans = _merge_spans(
        _label_alt_screen_spans(alt_spans, blocks)
        + _find_known_command_spans(blocks, alt_spans)
    )
    blocks = _collapse_interactive_spans(blocks, interactive_spans, events)

    return blocks


def session_blocks_payload(session_path: Path) -> dict:
    parsed = parse_session(session_path)
    blocks = build_blocks(session_path)
    return {
        "session_id": parsed.session_id,
        "shell": parsed.shell,
        "started_at": parsed.started_at,
        "ended_at": parsed.ended_at,
        "duration": parsed.duration,
        "columns": parsed.columns,
        "blocks": [b.to_dict() for b in blocks],
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"Usage: {sys.argv[0]} <session.json> [--out blocks.json]")

    session_path = Path(sys.argv[1])
    out_path = None
    if "--out" in sys.argv:
        out_path = Path(sys.argv[sys.argv.index("--out") + 1])

    payload = session_blocks_payload(session_path)

    if out_path:
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Blocks written to {out_path}")
    else:
        for b in payload["blocks"]:
            cwd_str = f" [{b['cwd']}]" if b.get("cwd") else ""
            exit_str = f" (exit {b['exit_code']})" if b.get("exit_code") is not None else ""
            print(f"${cwd_str} {b['command']}{exit_str}")
            for line in b["output"]:
                print(f"  {line}")
            print()


if __name__ == "__main__":
    main()