"""Shellby Phase 2 - Parser.

Takes a raw session log (per-keystroke input/output events, full of ANSI
escape codes) and replays the `output` stream through a real terminal
emulator to reconstruct what the terminal actually displayed: clean,
plain-text lines.

Reconstruction features:
    - Faithful terminal emulation with VT100 / ANSI escape sequence handling.
    - Seamless scrollback capture: lines that scroll off the top are preserved
      without hardcoded buffer limits or IndexError crashes.
    - Screen clear preservation: `clear` or Ctrl+L (`\\x1b[2J`) commits preceding
      transcript lines before resetting visible viewport, preventing data destruction.
    - Accurate timestamp assignment: line completions and in-place carriage return
      updates receive their true completion timestamps.
    - Dynamic window resize handling: terminal dimension adjustments (`SIGWINCH`)
      update screen geometry mid-stream.

Requires: pip install pyte
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    import pyte
except ImportError:  # pragma: no cover
    sys.exit(
        "The 'pyte' package is required for parsing.\n"
        "Install it with: pip install pyte"
    )

DEFAULT_COLUMNS = 80
DEFAULT_ROWS = 24


@dataclass
class ParsedLine:
    index: int          # row number in the reconstructed transcript, 0-based
    text: str            # plain text, ANSI/control codes resolved away
    timestamp: str        # best-effort: when this row was first completed

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedSession:
    session_id: str
    shell: str
    started_at: str
    ended_at: str
    duration: float
    columns: int
    lines: list[ParsedLine]

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "shell": self.shell,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": self.duration,
            "columns": self.columns,
            "lines": [line.to_dict() for line in self.lines],
        }

    def as_text(self) -> str:
        """Plain scrollback text, one reconstructed line per row."""
        return "\n".join(line.text for line in self.lines)


class RecordingScreen(pyte.Screen):
    """Terminal screen that faithfully accumulates lines, preserves scrollback,
    and assigns accurate timestamps across upward scrolling, screen clears,
    and in-place carriage returns."""

    def __init__(self, columns: int = DEFAULT_COLUMNS, lines: int = DEFAULT_ROWS):
        super().__init__(columns, lines)
        self.committed_lines: list[tuple[str, str]] = []  # (text, timestamp)
        self.current_event_time: str = ""
        self.row_timestamps: dict[int, str] = {}
        self.max_y: int = 0

    def draw(self, data: str) -> None:
        y = self.cursor.y
        self.max_y = max(self.max_y, y)
        self.row_timestamps[y] = self.current_event_time
        super().draw(data)

    def carriage_return(self) -> None:
        y = self.cursor.y
        self.row_timestamps[y] = self.current_event_time
        super().carriage_return()

    def linefeed(self) -> None:
        y = self.cursor.y
        if y >= self.lines - 1:
            # Row 0 scrolls off the top of the viewport into committed history
            top_text = self.display[0].rstrip()
            top_ts = self.row_timestamps.get(0, self.current_event_time)
            self.committed_lines.append((top_text, top_ts))
            # Shift remaining visible row timestamps up by 1
            new_ts = {r - 1: t for r, t in self.row_timestamps.items() if r > 0}
            self.row_timestamps = new_ts
        super().linefeed()
        self.max_y = max(self.max_y, self.cursor.y)

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        if how in (2, 3):
            # Screen is being cleared (\x1b[2J / Ctrl-L): commit active rows
            for r in range(self.max_y + 1):
                if r < len(self.display):
                    txt = self.display[r].rstrip()
                    if txt or r < self.cursor.y:
                        ts = self.row_timestamps.get(r, self.current_event_time)
                        self.committed_lines.append((txt, ts))
            self.row_timestamps.clear()
            self.max_y = 0
        super().erase_in_display(how, *args, **kwargs)

    def finalize(self, ended_at: str) -> list[ParsedLine]:
        """Commit all remaining rows on screen to build the final ParsedLine list."""
        all_raw = list(self.committed_lines)
        for r in range(self.max_y + 1):
            if r < len(self.display):
                txt = self.display[r].rstrip()
                if txt or r < self.cursor.y:
                    ts = self.row_timestamps.get(r, self.current_event_time or ended_at)
                    all_raw.append((txt, ts))

        lines: list[ParsedLine] = []
        for idx, (txt, ts) in enumerate(all_raw):
            lines.append(ParsedLine(index=idx, text=txt, timestamp=ts or ended_at))
        return lines


def parse_session(session_path: Path, columns: int | None = None) -> ParsedSession:
    data = json.loads(session_path.read_text(encoding="utf-8"))

    cols = columns or data.get("columns", DEFAULT_COLUMNS)
    ended_at = data.get("ended_at", "")

    screen = RecordingScreen(columns=cols, lines=DEFAULT_ROWS)
    stream = pyte.Stream(screen)

    for event in data.get("events", []):
        ev_type = event.get("type")
        ev_time = event.get("time", "")

        if ev_type == "resize":
            # Handle dynamic terminal resizes (Issue 1.2)
            try:
                resize_text = event.get("text", "")
                if "x" in resize_text:
                    new_cols, new_lines = map(int, resize_text.split("x"))
                    screen.resize(lines=new_lines, columns=new_cols)
            except Exception:
                pass
            continue

        if ev_type != "output":
            continue

        screen.current_event_time = ev_time
        stream.feed(event.get("text", ""))

    lines = screen.finalize(ended_at)

    return ParsedSession(
        session_id=data.get("session_id", ""),
        shell=data.get("shell", ""),
        started_at=data.get("started_at", ""),
        ended_at=ended_at,
        duration=data.get("duration", 0.0),
        columns=cols,
        lines=lines,
    )


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"Usage: {sys.argv[0]} <session.json> [--out parsed.json]")

    session_path = Path(sys.argv[1])
    out_path = None
    if "--out" in sys.argv:
        out_path = Path(sys.argv[sys.argv.index("--out") + 1])

    parsed = parse_session(session_path)

    if out_path:
        out_path.write_text(json.dumps(parsed.to_dict(), indent=2), encoding="utf-8")
        print(f"Parsed session written to {out_path}")
    else:
        print(parsed.as_text())


if __name__ == "__main__":
    main()