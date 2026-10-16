"""Shellby - Terminal Session Recorder and Guide Generator (Windows Command Prompt Edition).

Shellby pipeline for Windows Command Prompt (cmd.exe):

    Phase 1: Windows Console Recorder (ConPTY / cmd.exe)
        ↓
    session.json & session.events.jsonl
        ↓
    Phase 2: Parser (ANSI emulation via pyte)
        ↓
    reconstructed terminal data
        ↓
    Phase 3: Windows Heuristics (cmd prompt parsing, CWD extraction, blocks.json)
        ↓
    blocks.json
        ├──────────────→ Phase 4: Markdown Export (transcript.md / transcript.pdf)
        │
        └──────────────→ Phase 5: AI Curator (Gemini curation tailored for cmd.exe)
                               ↓
                          guide.md
                               ↓
                         Phase 6: PDF Export (guide.pdf)
"""

from __future__ import annotations

import codecs
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Optional Windows-specific imports
if sys.platform == "win32":
    import msvcrt
    import ctypes
    from ctypes import wintypes

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", wintypes.LPBYTE),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]
else:
    msvcrt = None  # type: ignore
    ctypes = None  # type: ignore
    wintypes = None  # type: ignore
    COORD = None  # type: ignore
    STARTUPINFOW = None  # type: ignore
    STARTUPINFOEXW = None  # type: ignore
    PROCESS_INFORMATION = None  # type: ignore


# ---------------------------------------------------------------------------
# Dependencies & Environment Discovery
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def _find_venv() -> Path | None:
    """Find a project virtual environment across standard Windows/Unix conventions."""
    for env_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if env_var in os.environ:
            candidate = Path(os.environ[env_var])
            for subdir in ("Scripts", "bin"):
                for exe in ("python.exe", "python", "python3.exe", "python3"):
                    p = candidate / subdir / exe
                    if p.exists():
                        return p

    curr = Path(__file__).resolve().parent

    for path in [curr, *curr.parents]:
        for candidate_name in (".venv", "venv", "env", ".env"):
            candidate_dir = path / candidate_name
            if not candidate_dir.is_dir():
                continue

            for subdir in ("Scripts", "bin"):
                for executable in ("python.exe", "python", "python3.exe", "python3"):
                    executable_path = candidate_dir / subdir / executable
                    if executable_path.exists():
                        return executable_path

    return None


try:
    from rich.console import Console
    import typer
except ImportError:
    venv_python = _find_venv()
    if venv_python and sys.executable != str(venv_python.resolve()):
        import subprocess
        sys.exit(subprocess.call([str(venv_python)] + sys.argv))
    else:
        sys.exit(
            "Error: 'typer' and 'rich' are required.\n"
            "Install with: pip install typer rich\n"
            "Or run Shellby inside its virtual environment."
        )


# ---------------------------------------------------------------------------
# CLI application setup
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="shellby",
    help="Shellby - Windows Command Prompt Session Analyser & Guide Generator",
    add_completion=False,
)

console = Console()

READ_CHUNK = 4096
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(text: str, max_length: int = 60) -> str:
    """Turn an arbitrary title into a short, filesystem-safe slug."""
    slug = _SLUG_RE.sub("-", text.strip()).strip("-")
    return slug[:max_length].strip("-")


def _unique_session_dir(day_dir: Path, base_name: str) -> tuple[Path, str]:
    """Pick a session folder name under `day_dir`, avoiding collisions."""
    candidate = base_name
    n = 2
    while (day_dir / candidate).exists():
        candidate = f"{base_name}-{n}"
        n += 1
    return day_dir / candidate, candidate


def _get_default_sessions_dir() -> Path:
    """Return default session storage directory, supporting SHELLBY_SESSIONS_DIR."""
    env_dir = os.environ.get("SHELLBY_SESSIONS_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path("sessions")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """A single input, output, resize, or metadata event from the session."""
    time: str
    type: str
    text: str


@dataclass
class Session:
    """Raw Shellby terminal session."""
    session_id: str
    started_at: str
    shell: str
    columns: int = 80
    title: str = ""
    initial_cwd: str = ""
    ended_at: str = ""
    duration: float = 0.0
    events: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "shell": self.shell,
            "columns": self.columns,
            "title": self.title,
            "initial_cwd": self.initial_cwd,
            "events": [asdict(event) for event in self.events],
            "ended_at": self.ended_at,
            "duration": self.duration,
        }


# ---------------------------------------------------------------------------
# Windows Console & Terminal Dimensions
# ---------------------------------------------------------------------------

def _get_terminal_size() -> tuple[int, int]:
    """Return terminal size as (rows, columns) on Windows or Unix."""
    try:
        size = os.get_terminal_size()
        if size.lines > 0 and size.columns > 0:
            return size.lines, size.columns
    except OSError:
        pass
    return 25, 80


def _enable_windows_vt_mode() -> tuple[bool, int, int]:
    """Enable Virtual Terminal Processing on Windows console handles."""
    if sys.platform != "win32" or not ctypes:
        return False, 0, 0

    kernel32 = ctypes.windll.kernel32
    STD_OUTPUT_HANDLE = -11
    STD_INPUT_HANDLE = -10
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    hIn = kernel32.GetStdHandle(STD_INPUT_HANDLE)

    out_mode = wintypes.DWORD()
    in_mode = wintypes.DWORD()

    if not kernel32.GetConsoleMode(hOut, ctypes.byref(out_mode)):
        return False, 0, 0
    kernel32.GetConsoleMode(hIn, ctypes.byref(in_mode))

    kernel32.SetConsoleMode(hOut, out_mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    kernel32.SetConsoleMode(hIn, in_mode.value | ENABLE_VIRTUAL_TERMINAL_INPUT)

    return True, out_mode.value, in_mode.value


def _restore_windows_console_mode(orig_out: int, orig_in: int) -> None:
    """Restore original console modes on exit."""
    if sys.platform != "win32" or not ctypes:
        return
    kernel32 = ctypes.windll.kernel32
    STD_OUTPUT_HANDLE = -11
    STD_INPUT_HANDLE = -10
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    if orig_out:
        kernel32.SetConsoleMode(hOut, orig_out)
    if orig_in:
        kernel32.SetConsoleMode(kernel32.GetStdHandle(STD_INPUT_HANDLE), orig_in)

    # Guarantee Win32 Input Mode and Focus Reporting are disabled on the host terminal
    reset_seq = b"\x1b[?9001l\x1b[?1004l"
    written = wintypes.DWORD()
    kernel32.WriteFile(hOut, reset_seq, len(reset_seq), ctypes.byref(written), None)


# ---------------------------------------------------------------------------
# ConPTY Input / Output Filters
# ---------------------------------------------------------------------------

CONPTY_STRIP_RE = re.compile(rb"\x1b\[\?(?:9001|1004)[hl]")
WIN32_INPUT_RE = re.compile(rb"\x1b\[(\d+);(\d+);(\d+);(\d+);(\d+);(\d+)[_~]")
FOCUS_EVENT_RE = re.compile(rb"\x1b\[[IO]")

VK_MAP: dict[int, bytes] = {
    8: b"\x08",      # BACKSPACE
    9: b"\t",        # TAB
    13: b"\r",       # ENTER
    27: b"\x1b",     # ESC
    33: b"\x1b[5~",  # PAGE UP
    34: b"\x1b[6~",  # PAGE DOWN
    35: b"\x1b[F",   # END
    36: b"\x1b[H",   # HOME
    37: b"\x1b[D",   # LEFT
    38: b"\x1b[A",   # UP
    39: b"\x1b[C",   # RIGHT
    40: b"\x1b[B",   # DOWN
    45: b"\x1b[2~",  # INSERT
    46: b"\x1b[3~",  # DELETE
}


def _translate_win32_input(raw: bytes) -> bytes:
    """Translate Win32 Input Mode escape sequences back to standard bytes if present."""
    if b"\x1b[" not in raw:
        return raw

    # Strip focus reporting escape sequences (e.g. \x1b[I or \x1b[O)
    raw = FOCUS_EVENT_RE.sub(b"", raw)

    def _replace(m: re.Match) -> bytes:
        vk = int(m.group(1))
        uc = int(m.group(3))
        kd = int(m.group(4))
        if kd == 0:
            # Key-up event: ignore
            return b""
        if uc > 0:
            if uc == 13:
                return b"\r"
            if uc == 8:
                return b"\x08"
            return chr(uc).encode("utf-8", errors="replace")
        return VK_MAP.get(vk, b"")

    return WIN32_INPUT_RE.sub(_replace, raw)


# ---------------------------------------------------------------------------
# Phase 1 - Windows Console Recorder (ConPTY / cmd.exe)
# ---------------------------------------------------------------------------

def _record_conpty_session(
    session: Session,
    events_file,
    rows: int,
    cols: int,
    shell_cmd: str,
) -> None:
    """Record an interactive Windows console session using the Windows 10/11 ConPTY API."""
    kernel32 = ctypes.windll.kernel32

    HPCON = wintypes.HANDLE
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

    # Ensure host terminal is reset out of Win32 Input Mode & Focus reporting
    hStdOut = kernel32.GetStdHandle(-11)
    reset_seq = b"\x1b[?9001l\x1b[?1004l"
    written = wintypes.DWORD()
    kernel32.WriteFile(hStdOut, reset_seq, len(reset_seq), ctypes.byref(written), None)

    # Ensure 64-bit safe CreateProcessW prototype
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL

    # Create communication pipes for ConPTY
    hPipePTYIn = wintypes.HANDLE()
    hPipeInputWrite = wintypes.HANDLE()
    hPipeOutputRead = wintypes.HANDLE()
    hPipePTYOut = wintypes.HANDLE()

    kernel32.CreatePipe(ctypes.byref(hPipePTYIn), ctypes.byref(hPipeInputWrite), None, 0)
    kernel32.CreatePipe(ctypes.byref(hPipeOutputRead), ctypes.byref(hPipePTYOut), None, 0)

    hPC = HPCON()
    coord = COORD(cols, rows)
    hr = kernel32.CreatePseudoConsole(coord, hPipePTYIn, hPipePTYOut, 0, ctypes.byref(hPC))
    if hr != 0:
        kernel32.CloseHandle(hPipePTYIn)
        kernel32.CloseHandle(hPipeInputWrite)
        kernel32.CloseHandle(hPipeOutputRead)
        kernel32.CloseHandle(hPipePTYOut)
        raise RuntimeError(f"CreatePseudoConsole failed with HRESULT 0x{hr:08x}")

    # Child handles can be closed once pseudo console is created
    kernel32.CloseHandle(hPipePTYIn)
    kernel32.CloseHandle(hPipePTYOut)

    # Initialize STARTUPINFOEX
    size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attr_list = ctypes.create_string_buffer(size.value)
    kernel32.InitializeProcThreadAttributeList(attr_list, 1, 0, ctypes.byref(size))

    kernel32.UpdateProcThreadAttribute(
        attr_list,
        0,
        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        hPC,
        ctypes.sizeof(hPC),
        None,
        None,
    )

    siex = STARTUPINFOEXW()
    siex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    siex.lpAttributeList = ctypes.cast(attr_list, ctypes.c_void_p)

    pi = PROCESS_INFORMATION()

    # Launch cmd.exe
    cmdline = ctypes.create_unicode_buffer(shell_cmd)
    success = kernel32.CreateProcessW(
        None,
        cmdline,
        None,
        None,
        False,
        EXTENDED_STARTUPINFO_PRESENT,
        None,
        None,
        ctypes.byref(siex.StartupInfo),
        ctypes.byref(pi),
    )

    if not success:
        kernel32.ClosePseudoConsole(hPC)
        kernel32.CloseHandle(hPipeInputWrite)
        kernel32.CloseHandle(hPipeOutputRead)
        kernel32.DeleteProcThreadAttributeList(attr_list)
        raise RuntimeError(f"Failed to spawn Command Prompt process (error {kernel32.GetLastError()}).")

    input_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    output_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _log(ev: Event):
        session.events.append(ev)
        try:
            events_file.write(json.dumps(asdict(ev), ensure_ascii=False) + "\n")
            events_file.flush()
        except Exception:
            pass

    stop_event = threading.Event()

    # Thread 1: Output reader from ConPTY
    def read_output():
        buf = ctypes.create_string_buffer(READ_CHUNK)
        read_bytes = wintypes.DWORD()

        while not stop_event.is_set():
            ok = kernel32.ReadFile(hPipeOutputRead, buf, READ_CHUNK, ctypes.byref(read_bytes), None)
            if not ok or read_bytes.value == 0:
                break
            raw = buf.raw[:read_bytes.value]

            # Strip ConPTY management handshake sequences (Win32 Input Mode & Focus reporting)
            out_raw = CONPTY_STRIP_RE.sub(b"", raw)
            if out_raw:
                written = wintypes.DWORD()
                kernel32.WriteFile(hStdOut, out_raw, len(out_raw), ctypes.byref(written), None)

                text = output_decoder.decode(out_raw)
                if text:
                    ev = Event(
                        time=datetime.now(timezone.utc).isoformat(),
                        type="output",
                        text=text,
                    )
                    _log(ev)

    # Put console input handle into raw VT mode for ConPTY pass-through
    hStdIn = kernel32.GetStdHandle(-10)
    current_in_mode = wintypes.DWORD()
    in_mode_ok = kernel32.GetConsoleMode(hStdIn, ctypes.byref(current_in_mode))
    if in_mode_ok:
        raw_mode = (
            current_in_mode.value & ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
        ) | ENABLE_VIRTUAL_TERMINAL_INPUT
        kernel32.SetConsoleMode(hStdIn, raw_mode)

    # Thread 2: Input writer from console stdin
    def read_input():
        in_buf = ctypes.create_string_buffer(READ_CHUNK)
        in_read = wintypes.DWORD()

        while not stop_event.is_set():
            ok = kernel32.ReadFile(hStdIn, in_buf, READ_CHUNK, ctypes.byref(in_read), None)
            if not ok or in_read.value == 0:
                break
            raw = in_buf.raw[:in_read.value]

            # Translate Win32 Input Mode sequences if any arrived from host terminal
            translated = _translate_win32_input(raw)
            if not translated:
                continue

            written = wintypes.DWORD()
            kernel32.WriteFile(hPipeInputWrite, translated, len(translated), ctypes.byref(written), None)

            text = input_decoder.decode(translated)
            if text:
                ev = Event(
                    time=datetime.now(timezone.utc).isoformat(),
                    type="input",
                    text=text,
                )
                _log(ev)

    out_thread = threading.Thread(target=read_output, daemon=True)
    out_thread.start()

    in_thread = threading.Thread(target=read_input, daemon=True)
    in_thread.start()

    try:
        # Wait for cmd.exe process to exit
        kernel32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)

        stop_event.set()
        kernel32.ClosePseudoConsole(hPC)
        kernel32.CloseHandle(hPipeInputWrite)
        kernel32.CloseHandle(hPipeOutputRead)
        kernel32.CloseHandle(pi.hProcess)
        kernel32.CloseHandle(pi.hThread)
        kernel32.DeleteProcThreadAttributeList(attr_list)

        # Gracefully unblock input reader thread if still waiting
        if hasattr(in_thread, "native_id"):
            hInThread = kernel32.OpenThread(0x0020 | 0x0001, False, in_thread.native_id)
            if hInThread:
                kernel32.CancelSynchronousIo(hInThread)
                kernel32.CloseHandle(hInThread)

        in_thread.join(timeout=0.5)
        out_thread.join(timeout=1.0)

        # Flush any remaining decoder bytes
        tail_out = output_decoder.decode(b"", final=True)
        if tail_out:
            _log(Event(time=datetime.now(timezone.utc).isoformat(), type="output", text=tail_out))

        tail_in = input_decoder.decode(b"", final=True)
        if tail_in:
            _log(Event(time=datetime.now(timezone.utc).isoformat(), type="input", text=tail_in))
    finally:
        # Guarantee Win32 Input Mode & Focus reporting are disabled on the host terminal
        reset_seq = b"\x1b[?9001l\x1b[?1004l"
        written = wintypes.DWORD()
        kernel32.WriteFile(hStdOut, reset_seq, len(reset_seq), ctypes.byref(written), None)

        if in_mode_ok:
            kernel32.SetConsoleMode(hStdIn, current_in_mode.value)


def _record_interactive_fallback(
    session: Session,
    events_file,
    shell_cmd: str,
) -> None:
    """Fallback interactive Command Prompt runner when ConPTY is unavailable."""
    import subprocess

    def _log(ev: Event):
        session.events.append(ev)
        try:
            events_file.write(json.dumps(asdict(ev), ensure_ascii=False) + "\n")
            events_file.flush()
        except Exception:
            pass

    is_powershell = "powershell" in session.shell.lower() or "pwsh" in session.shell.lower()
    shell_display = "PowerShell" if is_powershell else "Command Prompt"
    console.print(
        f"[cyan]Starting interactive {shell_display} session. "
        "Type `exit` to finish recording.[/cyan]"
    )

    initial_cwd = os.getcwd()
    _log(Event(time=datetime.now(timezone.utc).isoformat(), type="cwd", text=initial_cwd))

    while True:
        try:
            curr_cwd = os.getcwd()
            prompt_str = f"PS {curr_cwd}> " if is_powershell else f"{curr_cwd}> "
            cmd = input(prompt_str)
        except (EOFError, KeyboardInterrupt):
            print()
            break

        _log(Event(time=datetime.now(timezone.utc).isoformat(), type="input", text=cmd + "\n"))

        stripped = cmd.strip()
        if not stripped:
            continue

        if stripped.lower() in ("exit", "quit"):
            break

        # Handle cd command directly
        if stripped.lower().startswith("cd ") or stripped.lower() == "cd":
            parts = stripped.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else None
            if target:
                try:
                    os.chdir(target)
                    new_cwd = os.getcwd()
                    _log(Event(time=datetime.now(timezone.utc).isoformat(), type="cwd", text=new_cwd))
                except Exception as exc:
                    print(f"The system cannot find the path specified: {exc}")
            else:
                print(os.getcwd())
            continue

        # Execute command in shell
        start_ts = datetime.now(timezone.utc).isoformat()
        try:
            if is_powershell:
                proc = subprocess.run(
                    [session.shell, "-NoProfile", "-NonInteractive", "-Command", cmd],
                    text=True,
                    capture_output=True,
                )
            else:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    text=True,
                    capture_output=True,
                )
            out = proc.stdout
            err = proc.stderr

            if out:
                sys.stdout.write(out)
                sys.stdout.flush()
                _log(Event(time=datetime.now(timezone.utc).isoformat(), type="output", text=out))

            if err:
                sys.stderr.write(err)
                sys.stderr.flush()
                _log(Event(time=datetime.now(timezone.utc).isoformat(), type="output", text=err))

        except Exception as exc:
            err_msg = f"Command execution error: {exc}\n"
            sys.stderr.write(err_msg)
            _log(Event(time=datetime.now(timezone.utc).isoformat(), type="output", text=err_msg))


def record_session(
    sessions_dir: Path | None = None,
    title: str | None = None,
    shell_cmd: str | None = None,
) -> Path:
    """Record an interactive Windows Command Prompt or PowerShell session."""
    resolved_sessions_dir = sessions_dir or _get_default_sessions_dir()

    session_id = str(uuid.uuid4())
    started_dt = datetime.now(timezone.utc)
    date_str = started_dt.strftime("%Y-%m-%d")

    title = title or ""
    slug = _slugify(title)
    base_name = slug or f"session-{session_id[:8]}"

    session_dir, session_name = _unique_session_dir(
        resolved_sessions_dir / date_str, base_name
    )
    session_dir.mkdir(parents=True, exist_ok=True)

    session_path = session_dir / f"{session_name}.json"
    events_log_path = session_dir / f"{session_name}.events.jsonl"

    shell = shell_cmd or os.environ.get("SHELLBY_SHELL") or os.environ.get("COMSPEC", "cmd.exe")
    rows, cols = _get_terminal_size()
    initial_cwd = os.getcwd()

    session = Session(
        session_id=session_id,
        started_at=started_dt.isoformat(),
        shell=shell,
        columns=cols,
        title=title,
        initial_cwd=initial_cwd,
    )

    events_file = events_log_path.open("a", encoding="utf-8")

    is_powershell = "powershell" in shell.lower() or "pwsh" in shell.lower()
    shell_display = "PowerShell" if is_powershell else "Windows Command Prompt"
    console.print(
        f"[bold cyan]Shellby is recording this {shell_display} session.[/bold cyan]\n"
        "[dim]Exit the shell (type `exit`) to stop recording.[/dim]\n"
    )

    vt_enabled, orig_out, orig_in = _enable_windows_vt_mode()

    try:
        no_conpty_env = os.environ.get("SHELLBY_NO_CONPTY", "").lower() in ("1", "true", "yes")
        can_conpty = (
            sys.platform == "win32"
            and not no_conpty_env
            and ctypes is not None
            and hasattr(ctypes.windll.kernel32, "CreatePseudoConsole")
            and sys.stdin.isatty()
            and sys.stdout.isatty()
            and vt_enabled
        )

        if can_conpty:
            try:
                _record_conpty_session(
                    session=session,
                    events_file=events_file,
                    rows=rows,
                    cols=cols,
                    shell_cmd=shell,
                )
            except Exception as exc:
                console.print(f"[yellow]ConPTY unavailable ({exc}), using interactive fallback.[/yellow]")
                _record_interactive_fallback(
                    session=session,
                    events_file=events_file,
                    shell_cmd=shell,
                )
        else:
            _record_interactive_fallback(
                session=session,
                events_file=events_file,
                shell_cmd=shell,
            )
    finally:
        _restore_windows_console_mode(orig_out, orig_in)
        try:
            events_file.close()
        except Exception:
            pass

    ended_dt = datetime.now(timezone.utc)
    session.ended_at = ended_dt.isoformat()
    session.duration = (ended_dt - started_dt).total_seconds()

    session_path.write_text(
        json.dumps(session.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return session_path


# ---------------------------------------------------------------------------
# Pipeline glue (Phases 2-6)
# ---------------------------------------------------------------------------

def _run_guide_pipeline(
    session_path: Path,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    focus: str | None = None,
    pdf_transcript: bool = False,
) -> None:
    """Run Phases 2-6 on a recorded Windows session."""
    try:
        from .shellby_heuristics import session_blocks_payload
        from .shellby_markdown import render_markdown
        from .shellby_curator import curate_file
        from .shellby_pdf import render_markdown_to_pdf
    except ImportError:
        from shellby_heuristics import session_blocks_payload
        from shellby_markdown import render_markdown
        from shellby_curator import curate_file
        from shellby_pdf import render_markdown_to_pdf

    stem = session_path.stem

    blocks_path = session_path.with_name(f"{stem}.blocks.json")
    transcript_path = session_path.with_name(f"{stem}.transcript.md")
    transcript_pdf_path = session_path.with_name(f"{stem}.transcript.pdf")
    curated_path = session_path.with_name(f"{stem}.guide.md")
    pdf_path = session_path.with_name(f"{stem}.guide.pdf")

    console.print("[dim]Parsing Windows session and extracting command blocks...[/dim]")

    payload = session_blocks_payload(session_path)
    blocks_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    console.print(f"[green]Blocks:[/green]     {blocks_path}")

    transcript = render_markdown(blocks_path)
    transcript_path.write_text(transcript, encoding="utf-8")
    console.print(f"[green]Transcript:[/green] {transcript_path}")

    if pdf_transcript:
        render_markdown_to_pdf(transcript, transcript_pdf_path)
        console.print(f"[green]Transcript PDF:[/green] {transcript_pdf_path}")

    console.print(
        "[dim]Sending Windows session data to Gemini to draft an efficient guide...[/dim]"
    )

    try:
        curated = curate_file(
            input_path=blocks_path,
            out_path=curated_path,
            api_key=api_key,
            model=model,
            focus=focus,
        )
    except Exception as exc:
        console.print(f"[red]AI curation failed:[/red] {exc}")
        console.print("[dim]Generating transcript PDF as fallback...[/dim]")
        if not pdf_transcript:
            try:
                render_markdown_to_pdf(transcript, transcript_pdf_path)
                console.print(f"[green]Fallback PDF:[/green] {transcript_pdf_path}")
            except Exception as pdf_exc:
                console.print(f"[dim]Could not generate fallback PDF: {pdf_exc}[/dim]")
        return

    console.print(f"[green]Curated guide:[/green] {curated_path}")

    render_markdown_to_pdf(curated, pdf_path)
    console.print(f"[green]PDF guide:[/green]    {pdf_path}")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@app.command()
def record(
    sessions_dir: Path | None = typer.Option(
        None,
        "--dir",
        "-d",
        help="Directory to store the recorded session (defaults to SHELLBY_SESSIONS_DIR or .\\sessions).",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        "-t",
        help="Optional title for the session, folded into the filename and stored in session.json.",
    ),
    guide: bool = typer.Option(
        True,
        "--guide/--no-guide",
        help="Generate parse/blocks/transcript/AI guide/PDF right after recording.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        "-k",
        help="Gemini API key (or set GEMINI_API_KEY).",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL,
        "--model",
        "-m",
        help="Gemini model to use for curation.",
    ),
    focus: str | None = typer.Option(
        None,
        "--focus",
        "-f",
        help="Optional hint about the goal of the session, to help curation.",
    ),
    pdf_transcript: bool = typer.Option(
        False,
        "--pdf-transcript",
        help="Also compile the raw transcript directly into a PDF.",
    ),
    shell: str | None = typer.Option(
        None,
        "--shell",
        "-s",
        help="Shell to launch for recording (e.g. cmd.exe, powershell.exe, pwsh.exe). Defaults to %COMSPEC% or cmd.exe.",
    ),
) -> None:
    """Record a Windows Command Prompt or PowerShell session, then turn it into a guide."""
    try:
        session_path = record_session(sessions_dir=sessions_dir, title=title, shell_cmd=shell)
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]Session recorded:[/green] {session_path}")

    if guide:
        _run_guide_pipeline(
            session_path,
            api_key=api_key,
            model=model,
            focus=focus,
            pdf_transcript=pdf_transcript,
        )
    else:
        console.print(
            f"[dim]Run `python shellby.py guide {session_path}` "
            "whenever you're ready to generate the guide.[/dim]"
        )


@app.command()
def guide(
    session_path: Path = typer.Argument(
        ...,
        help="Path to a recorded session.json.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        "-k",
        help="Gemini API key (or set GEMINI_API_KEY).",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL,
        "--model",
        "-m",
        help="Gemini model to use for curation.",
    ),
    focus: str | None = typer.Option(
        None,
        "--focus",
        "-f",
        help="Optional hint about the goal of the session, to help curation.",
    ),
    pdf_transcript: bool = typer.Option(
        False,
        "--pdf-transcript",
        help="Also compile the raw transcript directly into a PDF.",
    ),
) -> None:
    """Run the full Phase 2-6 pipeline on an existing recorded session."""
    if not session_path.exists():
        console.print(f"[red]Session file not found:[/red] {session_path}")
        raise typer.Exit(code=1)

    _run_guide_pipeline(
        session_path,
        api_key=api_key,
        model=model,
        focus=focus,
        pdf_transcript=pdf_transcript,
    )


@app.command()
def curate(
    blocks_path: Path = typer.Argument(
        ...,
        help="Path to a blocks.json produced by the heuristics phase.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Where to write the curated Markdown guide.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        "-k",
        help="Gemini API key (or set GEMINI_API_KEY).",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL,
        "--model",
        "-m",
        help="Gemini model to use for curation.",
    ),
    focus: str | None = typer.Option(
        None,
        "--focus",
        "-f",
        help="Optional hint about the goal of the session, to help curation.",
    ),
) -> None:
    """Send an existing blocks.json to Gemini and print/save the curated guide."""
    try:
        from .shellby_curator import curate_file
    except ImportError:
        from shellby_curator import curate_file

    try:
        curated = curate_file(
            input_path=blocks_path,
            out_path=out,
            api_key=api_key,
            model=model,
            focus=focus,
        )
    except Exception as exc:
        console.print(f"[red]AI curation failed:[/red] {exc}")
        raise typer.Exit(code=1)

    if out:
        console.print(f"[green]Curated guide written to:[/green] {out}")
    else:
        console.print(curated)


@app.command()
def parse(
    session_path: Path = typer.Argument(
        ...,
        help="Path to session.json to parse.",
    ),
) -> None:
    """Parse session.json and output plain reconstructed text."""
    try:
        from .shellby_parser import parse_session
    except ImportError:
        from shellby_parser import parse_session

    parsed = parse_session(session_path)
    console.print(parsed.as_text())


@app.command()
def pdf(
    markdown_path: Path = typer.Argument(
        ...,
        help="Path to markdown guide or transcript.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Path to output PDF file.",
    ),
) -> None:
    """Convert a Markdown guide or transcript to PDF."""
    try:
        from .shellby_pdf import render_markdown_to_pdf
    except ImportError:
        from shellby_pdf import render_markdown_to_pdf

    out_path = out or markdown_path.with_suffix(".pdf")
    render_markdown_to_pdf(markdown_path.read_text(encoding="utf-8"), out_path)
    console.print(f"[green]PDF exported to:[/green] {out_path}")


if __name__ == "__main__":
    app()
