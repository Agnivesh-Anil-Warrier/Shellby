"""Shellby - Terminal Session Recorder and Guide Generator.

Shellby pipeline:

    Phase 1: Recorder
        ↓
    session.json
        ↓
    Phase 2: Parser
        ↓
    reconstructed terminal data
        ↓
    Phase 3: Heuristics
        ↓
    blocks.json
        ├──────────────→ Phase 4: Markdown Export
        │                         ↓
        │                    transcript.md
        │                         ↓ (Optional or fallback)
        │                    transcript.pdf
        │
        └──────────────→ Phase 5: AI Curator
                                  ↓
                             curated.md
                                  ↓
                           Phase 6: PDF Export
                                  ↓
                               guide.pdf

The recorded session and blocks.json are treated as source artifacts.
AI curation produces a separate guide and does not modify the source data.
"""

from __future__ import annotations

import codecs
import errno
import json
import os
import re
import select
import signal
import struct
import sys
import time
import uuid

try:
    import fcntl
    import pty
    import termios
except ImportError:
    fcntl = None  # type: ignore
    pty = None  # type: ignore
    termios = None  # type: ignore

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Dependencies & Environment Discovery
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def _find_venv() -> Path | None:
    """Find a project virtual environment across standard conventions."""

    for env_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if env_var in os.environ:
            candidate = Path(os.environ[env_var])
            for subdir in ("bin", "Scripts"):
                for exe in ("python", "python.exe", "python3"):
                    p = candidate / subdir / exe
                    if p.exists():
                        return p

    curr = Path(__file__).resolve().parent

    for path in [curr, *curr.parents]:
        for candidate_name in (".venv", "venv", "env", ".env"):
            candidate_dir = path / candidate_name

            if not candidate_dir.is_dir():
                continue

            for subdir in ("bin", "Scripts"):
                for executable in ("python", "python.exe", "python3"):
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
        os.execv(
            str(venv_python),
            [str(venv_python)] + sys.argv,
        )
    else:
        sys.exit(
            "Error: 'typer' and 'rich' are required. "
            "Run Shellby inside its virtual environment."
        )


# ---------------------------------------------------------------------------
# CLI application setup
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="shellby",
    help="Shellby - Terminal Session Analyser",
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


def _write_all(fd: int, data: bytes) -> None:
    """Write all bytes to a file descriptor, handling partial writes safely."""
    while data:
        try:
            written = os.write(fd, data)
            data = data[written:]
        except (BlockingIOError, InterruptedError):
            continue


def _get_child_cwd(child_pid: int) -> str | None:
    """Inspect current working directory of child process via /proc on Linux."""
    try:
        return os.readlink(f"/proc/{child_pid}/cwd")
    except (OSError, ValueError):
        return None


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
            "events": [
                asdict(event)
                for event in self.events
            ],
            "ended_at": self.ended_at,
            "duration": self.duration,
        }


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

def _get_size() -> tuple[int, int]:
    """Return terminal size as (rows, columns)."""

    try:
        size = os.get_terminal_size()
        if size.lines <= 0 or size.columns <= 0:
            return 24, 80
        return size.lines, size.columns
    except OSError:
        return 24, 80


def _set_pty_size(
    fd: int,
    rows: int,
    cols: int,
) -> None:
    """Set the size of the pseudo-terminal."""

    winsize = struct.pack(
        "HHHH",
        rows,
        cols,
        0,
        0,
    )

    fcntl.ioctl(
        fd,
        termios.TIOCSWINSZ,
        winsize,
    )


# ---------------------------------------------------------------------------
# Phase 1 - Recorder
# ---------------------------------------------------------------------------

def record_session(
    sessions_dir: Path | None = None,
    title: str | None = None,
) -> Path:
    """Record an interactive terminal session with streaming crash resilience."""

    if (
        pty is None
        or termios is None
        or fcntl is None
    ):
        raise RuntimeError(
            "Terminal session recording requires "
            "a Unix operating system (Linux or macOS)."
        )

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

    shell = os.environ.get("SHELL", "/bin/bash")
    is_tty = sys.stdin.isatty()

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    old_settings = (
        termios.tcgetattr(stdin_fd)
        if is_tty
        else None
    )

    rows, cols = _get_size()
    initial_cwd = os.getcwd()

    session = Session(
        session_id=session_id,
        started_at=started_dt.isoformat(),
        shell=shell,
        columns=cols,
        title=title,
        initial_cwd=initial_cwd,
    )

    # Open append-only events file for crash resilience (Issue 1.1)
    events_file = events_log_path.open("a", encoding="utf-8")

    def _log_event(ev: Event) -> None:
        try:
            events_file.write(json.dumps(asdict(ev), ensure_ascii=False) + "\n")
            events_file.flush()
        except Exception:
            pass

    # Create real PTY and fork the shell.
    pid, master_fd = pty.fork()

    if pid == 0:
        os.execvp(shell, [shell])
        os._exit(1)

    _set_pty_size(master_fd, rows, cols)

    if is_tty:
        import tty
        tty.setraw(stdin_fd)

    def _handle_winch(signum, frame) -> None:
        """Propagate real terminal resizes to PTY and record event (Issue 1.2)."""
        try:
            new_rows, new_cols = _get_size()
            _set_pty_size(master_fd, new_rows, new_cols)
            resize_ev = Event(
                time=datetime.now(timezone.utc).isoformat(),
                type="resize",
                text=f"{new_cols}x{new_rows}",
            )
            session.events.append(resize_ev)
            _log_event(resize_ev)
        except OSError:
            pass

    have_winch = hasattr(signal, "SIGWINCH")
    old_winch_handler = (
        signal.signal(signal.SIGWINCH, _handle_winch)
        if have_winch
        else None
    )

    # Graceful exit signal handler (Issue 1.1)
    def _handle_term(signum, frame) -> None:
        raise KeyboardInterrupt()

    old_sigterm = signal.signal(signal.SIGTERM, _handle_term)

    input_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    output_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    console.print(
        "[dim]Shellby is recording this session. "
        "Exit the shell (e.g. `exit` or Ctrl-D) to stop.[/dim]"
    )

    last_cwd = initial_cwd

    try:
        while True:
            try:
                readable, _, _ = select.select(
                    [stdin_fd, master_fd],
                    [],
                    [],
                    0.25,
                )
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise

            # Periodic/Enter CWD tracking (Issue 5.2)
            current_cwd = _get_child_cwd(pid)
            if current_cwd and current_cwd != last_cwd:
                last_cwd = current_cwd
                cwd_ev = Event(
                    time=datetime.now(timezone.utc).isoformat(),
                    type="cwd",
                    text=current_cwd,
                )
                session.events.append(cwd_ev)
                _log_event(cwd_ev)

            if master_fd in readable:
                try:
                    data = os.read(master_fd, READ_CHUNK)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        data = b""
                    else:
                        raise

                if not data:
                    break

                # Full write handling (Issue 1.4)
                _write_all(stdout_fd, data)

                text = output_decoder.decode(data)
                if text:
                    ev = Event(
                        time=datetime.now(timezone.utc).isoformat(),
                        type="output",
                        text=text,
                    )
                    session.events.append(ev)
                    _log_event(ev)

            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, READ_CHUNK)
                except OSError:
                    data = b""

                if data:
                    # Full write handling (Issue 1.4)
                    _write_all(master_fd, data)

                    text = input_decoder.decode(data)
                    if text:
                        ev = Event(
                            time=datetime.now(timezone.utc).isoformat(),
                            type="input",
                            text=text,
                        )
                        session.events.append(ev)
                        _log_event(ev)

            try:
                waited_pid, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break

            if waited_pid == pid:
                break

    finally:
        # Flush trailing multibyte sequences (Issue 1.5)
        tail_out = output_decoder.decode(b"", final=True)
        if tail_out:
            ev = Event(
                time=datetime.now(timezone.utc).isoformat(),
                type="output",
                text=tail_out,
            )
            session.events.append(ev)
            _log_event(ev)

        tail_in = input_decoder.decode(b"", final=True)
        if tail_in:
            ev = Event(
                time=datetime.now(timezone.utc).isoformat(),
                type="input",
                text=tail_in,
            )
            session.events.append(ev)
            _log_event(ev)

        try:
            events_file.close()
        except Exception:
            pass

        if have_winch and old_winch_handler is not None:
            signal.signal(signal.SIGWINCH, old_winch_handler)

        signal.signal(signal.SIGTERM, old_sigterm)

        if old_settings is not None:
            termios.tcsetattr(
                stdin_fd,
                termios.TCSADRAIN,
                old_settings,
            )

        try:
            os.close(master_fd)
        except OSError:
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
    """Run Phases 2-6 on a recorded session. Supports direct transcript PDF export."""

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

    console.print("[dim]Parsing session and grouping command blocks...[/dim]")

    payload = session_blocks_payload(session_path)
    blocks_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    console.print(f"[green]Blocks:[/green]     {blocks_path}")

    transcript = render_markdown(blocks_path)
    transcript_path.write_text(transcript, encoding="utf-8")
    console.print(f"[green]Transcript:[/green] {transcript_path}")

    # Direct transcript PDF export if requested (Issue 5.3)
    if pdf_transcript:
        render_markdown_to_pdf(transcript, transcript_pdf_path)
        console.print(f"[green]Transcript PDF:[/green] {transcript_pdf_path}")

    console.print(
        "[dim]Sending session data to Gemini to identify the goal and "
        "draft an efficient guide...[/dim]"
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
        console.print(
            "[dim]The transcript above is unaffected. Generating transcript PDF as fallback...[/dim]"
        )
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
        help="Directory to store the recorded session (defaults to SHELLBY_SESSIONS_DIR or ./sessions).",
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
) -> None:
    """Record a terminal session, then (by default) turn it into a guide."""

    try:
        session_path = record_session(sessions_dir=sessions_dir, title=title)
    except RuntimeError as exc:
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
            f"[dim]Run `shellby guide {session_path}` "
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


if __name__ == "__main__":
    app()