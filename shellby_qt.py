#!/usr/bin/env python3
"""Shellby - Desktop GUI for browsing, recording, and curating terminal sessions.

Rectifications (Section 6):
    - Added UI controls to record sessions, run heuristics, AI curate, and export PDFs (Issue 6.1).
    - Per-session pending saves with flush on session switch, focus loss, and window close (Issue 6.2).
    - Debounced search & visibility toggling eliminating widget destruction lag (Issue 6.3).
    - Capped directory watching & timestamp write guards preventing inotify loops (Issue 6.4).
    - Enclosing session folder and parent date directory cleanup on session delete (Issue 6.5).
    - Accurate date formatting preserving zero-padded minutes without corruption (Issue 6.6).
    - Normalized POSIX path handling eliminating path separator bugs (Issue 6.7).
"""

from __future__ import annotations

import html as htmlmod
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import (
    Qt, QTimer, QSettings, QUrl, QFileSystemWatcher, QSize, pyqtSignal, QRect, QPoint, QThread,
    QPointF, QRectF
)
from PyQt5.QtGui import (
    QDesktopServices, QFont, QKeySequence, QPixmap, QPainter, QPainterPath,
    QColor, QPen, QBrush, QPolygon, QPolygonF, QIcon
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QTextEdit, QTextBrowser,
    QPlainTextEdit, QTabWidget, QFileDialog, QMessageBox, QScrollArea,
    QFrame, QSplitter, QLayout, QDialog, QDialogButtonBox, QCheckBox,
    QProgressDialog, QStatusBar, QShortcut
)

try:
    import markdown
    HAS_MARKDOWN = True
except ImportError:
    HAS_MARKDOWN = False

APP_ORG = "Shellby"
APP_NAME = "ShellbyQt"
LIBRARY_FILE = ".shellby-library.json"

SUFFIXES = [
    ("blocks", ".blocks.json"),
    ("transcript", ".transcript.md"),
    ("transcriptPdf", ".transcript.pdf"),
    ("guideMd", ".guide.md"),
    ("guidePdf", ".guide.pdf"),
]


# ----------------------------------------------------------------------
# Path & Filesystem helpers (Issue 6.7)
# ----------------------------------------------------------------------

def norm_rel_path(path_str: str) -> str:
    """Normalize relative path to use consistent forward slashes (Issue 6.7)."""
    return Path(path_str).as_posix()


def base_stem(stem: str) -> str:
    """Extract final component of a session stem safely (Issue 6.7)."""
    return Path(stem).name


def dir_of(rel_path: str) -> str:
    """Extract directory portion of a relative path safely (Issue 6.7)."""
    p = Path(rel_path).parent.as_posix()
    return "" if p == "." else p


def is_bare_guide_file(filename: str) -> bool:
    """Check if filename matches bare guide naming safely (Issue 6.7)."""
    base = Path(filename).name
    return base in ("guide.md", "guide.pdf")


def stem_for(filename: str) -> str | None:
    norm_fn = norm_rel_path(filename)
    for _key, suffix in SUFFIXES:
        if norm_fn.endswith(suffix):
            return norm_fn[:-len(suffix)]
    if norm_fn.endswith(".json"):
        return norm_fn[:-len(".json")]
    return None


def read_library(folder: str) -> dict:
    lib_path = os.path.join(folder, LIBRARY_FILE)
    try:
        with open(lib_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        if isinstance(parsed, dict) and "sessions" in parsed:
            return parsed
    except Exception:
        pass
    return {"sessions": {}}


def write_library(folder: str, library: dict) -> None:
    lib_path = os.path.join(folder, LIBRARY_FILE)
    with open(lib_path, "w", encoding="utf-8") as f:
        json.dump(library, f, indent=2)


def safe_read_json(file_path: str) -> dict | list | None:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def walk_files(base_dir: str) -> list[str]:
    results = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            full_path = os.path.join(root, name)
            rel = os.path.relpath(full_path, base_dir)
            results.append(norm_rel_path(rel))
    return results


def scan_folder(folder: str) -> list[dict]:
    """Scan folder and group sessions, normalizing paths consistently (Issue 6.7)."""
    files = walk_files(folder)

    groups: dict[str, dict] = {}
    bare_guide_files = []

    for filename in files:
        if filename == LIBRARY_FILE or filename.endswith("/" + LIBRARY_FILE):
            continue

        if is_bare_guide_file(filename):
            bare_guide_files.append(filename)
            continue

        stem = stem_for(filename)
        if not stem:
            continue

        group = groups.setdefault(stem, {"stem": stem, "files": {}})

        matched_key = None
        for key, suffix in SUFFIXES:
            if filename.endswith(suffix):
                matched_key = key
                break

        if matched_key:
            group["files"][matched_key] = filename
        elif filename.endswith(".json"):
            group["files"].setdefault("session", filename)

    for filename in bare_guide_files:
        key = "guidePdf" if filename.endswith(".pdf") else "guideMd"
        d = dir_of(filename)
        matches = [g for g in groups.values() if dir_of(g["stem"]) == d]

        if matches:
            for g in matches:
                g["files"].setdefault(key, filename)
        else:
            standalone_stem = f"{d}/guide" if d else "guide"
            group = groups.setdefault(standalone_stem, {"stem": standalone_stem, "files": {}})
            group["files"].setdefault(key, filename)

    library = read_library(folder)
    lib_sessions = library.get("sessions", {})

    sessions = []
    for stem, group in groups.items():
        gfiles = group["files"]
        session_file = gfiles.get("session")
        blocks_file = gfiles.get("blocks")

        session_meta = safe_read_json(os.path.join(folder, session_file)) if session_file else None
        blocks_meta = safe_read_json(os.path.join(folder, blocks_file)) if blocks_file else None

        commands = []
        structured_blocks = []
        if blocks_meta and isinstance(blocks_meta.get("blocks"), list):
            for b in blocks_meta["blocks"]:
                if isinstance(b, dict) and isinstance(b.get("command"), str) and b["command"].strip():
                    cmd_str = b["command"].strip()
                    commands.append(cmd_str)
                    structured_blocks.append({
                        "command": cmd_str,
                        "exit_code": b.get("exit_code"),
                        "cwd": b.get("cwd"),
                    })

        def pick(*srcs_keys):
            for src, key in srcs_keys:
                if src and src.get(key) is not None:
                    return src.get(key)
            return None

        started_at = pick((session_meta, "started_at"), (blocks_meta, "started_at"))
        ended_at = pick((session_meta, "ended_at"), (blocks_meta, "ended_at"))
        duration = pick((session_meta, "duration"), (blocks_meta, "duration"))
        shell = pick((session_meta, "shell"), (blocks_meta, "shell"))
        session_id = pick((session_meta, "session_id"), (blocks_meta, "session_id")) or stem

        lib_entry = lib_sessions.get(stem, {})

        any_file = (
            session_file
            or blocks_file
            or gfiles.get("transcript")
            or gfiles.get("guideMd")
            or gfiles.get("guidePdf")
        )
        mtime_ms = None
        if any_file:
            try:
                mtime_ms = os.path.getmtime(os.path.join(folder, any_file)) * 1000
            except OSError:
                mtime_ms = None

        sessions.append({
            "stem": stem,
            "sessionId": session_id,
            "startedAt": started_at,
            "endedAt": ended_at,
            "duration": duration,
            "shell": shell,
            "commandCount": len(commands),
            "commands": commands,
            "structuredBlocks": structured_blocks,
            "files": gfiles,
            "title": lib_entry.get("title"),
            "tags": list(lib_entry.get("tags", [])),
            "notes": lib_entry.get("notes", ""),
            "favorite": bool(lib_entry.get("favorite", False)),
            "mtimeMs": mtime_ms,
        })

    def time_of(s):
        if s["startedAt"]:
            t = parse_iso(s["startedAt"])
            if t is not None:
                return t
        return s["mtimeMs"] or 0

    sessions.sort(key=time_of, reverse=True)
    return sessions


def update_session_meta(folder: str, stem: str, patch: dict) -> dict:
    library = read_library(folder)
    existing = library["sessions"].get(stem, {})
    existing.update(patch)
    library["sessions"][stem] = existing
    write_library(folder, library)
    return existing


def delete_session_files(folder: str, stem: str, files: dict | None) -> None:
    """Delete session files and clean up empty enclosing session and date folders (Issue 6.5)."""
    del_paths = []
    for filename in (files or {}).values():
        if not filename:
            continue
        full_p = os.path.join(folder, filename)
        del_paths.append(full_p)
        try:
            if os.path.exists(full_p):
                os.remove(full_p)
        except OSError:
            pass

    # Clean companion files matching the stem (e.g. .events.jsonl, .raw.log, .transcript.pdf)
    stem_full = os.path.join(folder, stem)
    companion_suffixes = [
        ".events.jsonl", ".raw.log", ".transcript.pdf", ".guide.pdf",
        ".guide.md", ".transcript.md", ".blocks.json", ".json"
    ]
    for cs in companion_suffixes:
        cp = stem_full + cs
        if cp not in del_paths and os.path.exists(cp):
            del_paths.append(cp)
            try:
                os.remove(cp)
            except OSError:
                pass

    # Clean up empty parent directories (session folder and date folder)
    dirs_to_check = set()
    for p in del_paths:
        s_dir = os.path.dirname(p)
        if s_dir and s_dir != folder:
            dirs_to_check.add(s_dir)

    for session_dir in sorted(dirs_to_check, key=len, reverse=True):
        if os.path.isdir(session_dir) and session_dir != folder:
            try:
                remaining = [f for f in os.listdir(session_dir) if not f.startswith(".")]
                if not remaining:
                    shutil.rmtree(session_dir, ignore_errors=True)
            except OSError:
                pass

            parent_dir = os.path.dirname(session_dir)
            if parent_dir and os.path.isdir(parent_dir) and parent_dir != folder:
                try:
                    remaining_date = [f for f in os.listdir(parent_dir) if not f.startswith(".")]
                    if not remaining_date:
                        shutil.rmtree(parent_dir, ignore_errors=True)
                except OSError:
                    pass

    library = read_library(folder)
    library["sessions"].pop(stem, None)
    write_library(folder, library)


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------

def parse_iso(iso_str: str | None) -> float | None:
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp() * 1000
    except Exception:
        return None


def fmt_duration(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None
    s = round(seconds)
    m, rem = divmod(s, 60)
    return f"{m}m {rem}s" if m > 0 else f"{rem}s"


def fmt_date(iso_str: str | None) -> str | None:
    """Accurately format dates without corrupting minute components (Issue 6.6)."""
    ts = parse_iso(iso_str)
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts / 1000)
    hour_12 = dt.hour % 12 or 12
    return f"{dt.strftime('%b')} {dt.day}, {hour_12}:{dt.minute:02d} {dt.strftime('%p')}"


def display_title(session: dict) -> str:
    return session.get("title") or base_stem(session["stem"])


# ----------------------------------------------------------------------
# Vector Icon Generator (Professional, 100% compatible, no emoji glyphs)
# ----------------------------------------------------------------------

def create_vector_icon(name: str, color_hex: str = "#475569", size: int = 16) -> QIcon:
    """Create a crisp, modern vector QIcon without relying on system fonts or emojis."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    color = QColor(color_hex)
    pen = QPen(color, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)

    if name == "record":
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QRectF(3.5, 3.5, 9, 9))

    elif name == "parse":
        painter.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(QPointF(5, 4.5), QPointF(2.5, 8))
        painter.drawLine(QPointF(2.5, 8), QPointF(5, 11.5))
        painter.drawLine(QPointF(6.5, 12.5), QPointF(9.5, 3.5))
        painter.drawLine(QPointF(11, 4.5), QPointF(13.5, 8))
        painter.drawLine(QPointF(13.5, 8), QPointF(11, 11.5))

    elif name == "ai":
        path = QPainterPath()
        path.moveTo(7.5, 1.5)
        path.quadTo(7.5, 7.5, 1.5, 7.5)
        path.quadTo(7.5, 7.5, 7.5, 13.5)
        path.quadTo(7.5, 7.5, 13.5, 7.5)
        path.quadTo(7.5, 7.5, 7.5, 1.5)
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)
        path2 = QPainterPath()
        path2.moveTo(13.5, 10.5)
        path2.quadTo(13.5, 12.5, 11.5, 12.5)
        path2.quadTo(13.5, 12.5, 13.5, 14.5)
        path2.quadTo(13.5, 12.5, 15.5, 12.5)
        path2.quadTo(13.5, 12.5, 13.5, 10.5)
        painter.drawPath(path2)

    elif name == "pdf":
        path = QPainterPath()
        path.moveTo(3, 1.5)
        path.lineTo(9.5, 1.5)
        path.lineTo(13, 5)
        path.lineTo(13, 14.5)
        path.lineTo(3, 14.5)
        path.closeSubpath()
        painter.setPen(QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(path)
        painter.drawLine(QPointF(9.5, 1.5), QPointF(9.5, 5))
        painter.drawLine(QPointF(9.5, 5), QPointF(13, 5))
        painter.drawLine(QPointF(5.5, 8.5), QPointF(10.5, 8.5))
        painter.drawLine(QPointF(5.5, 11.5), QPointF(10.5, 11.5))

    elif name == "pipeline":
        painter.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(QPointF(3, 3.5), QPointF(7.5, 8))
        painter.drawLine(QPointF(7.5, 8), QPointF(3, 12.5))
        painter.drawLine(QPointF(8.5, 3.5), QPointF(13, 8))
        painter.drawLine(QPointF(13, 8), QPointF(8.5, 12.5))

    elif name == "open_pdf":
        painter.setPen(QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(QPolygonF([QPointF(7, 3), QPointF(2.5, 3), QPointF(2.5, 13.5), QPointF(13, 13.5), QPointF(13, 9)]))
        painter.drawLine(QPointF(7.5, 8.5), QPointF(13.5, 2.5))
        painter.drawLine(QPointF(9.5, 2.5), QPointF(13.5, 2.5))
        painter.drawLine(QPointF(13.5, 2.5), QPointF(13.5, 6.5))

    elif name == "reveal":
        path = QPainterPath()
        path.moveTo(2, 4)
        path.lineTo(6, 4)
        path.lineTo(7.5, 6)
        path.lineTo(14, 6)
        path.lineTo(14, 13)
        path.lineTo(2, 13)
        path.closeSubpath()
        painter.setPen(QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(path)

    elif name == "delete":
        painter.setPen(QPen(QColor("#ef4444"), 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(QPointF(4, 5.5), QPointF(4.8, 14))
        painter.drawLine(QPointF(4.8, 14), QPointF(11.2, 14))
        painter.drawLine(QPointF(11.2, 14), QPointF(12, 5.5))
        painter.drawLine(QPointF(2.5, 5.5), QPointF(13.5, 5.5))
        painter.drawLine(QPointF(6, 3.5), QPointF(10, 3.5))
        painter.drawLine(QPointF(6, 3.5), QPointF(6, 5.5))
        painter.drawLine(QPointF(10, 3.5), QPointF(10, 5.5))
        painter.drawLine(QPointF(6.5, 8), QPointF(6.5, 12))
        painter.drawLine(QPointF(9.5, 8), QPointF(9.5, 12))

    elif name in ("star", "star_outline"):
        pts = []
        for i in range(10):
            r = 7.0 if i % 2 == 0 else 3.2
            angle = -math.pi / 2 + i * math.pi / 5
            pts.append(QPointF(8 + r * math.cos(angle), 8 + r * math.sin(angle)))
        if name == "star":
            painter.setBrush(QBrush(QColor("#f59e0b")))
            painter.setPen(Qt.NoPen)
        else:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#94a3b8"), 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolygon(QPolygonF(pts))

    elif name == "refresh":
        painter.setPen(QPen(color, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawArc(QRectF(2.5, 2.5, 11, 11), 45 * 16, 270 * 16)
        painter.drawLine(QPointF(12, 4), QPointF(14, 8))
        painter.drawLine(QPointF(12, 4), QPointF(8, 6.5))

    elif name == "key":
        painter.setPen(QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawEllipse(QRectF(2.0, 4.0, 6.5, 6.5))
        painter.drawEllipse(QRectF(4.0, 6.0, 2.5, 2.5))
        painter.drawLine(QPointF(8.5, 7.25), QPointF(14.5, 7.25))
        painter.drawLine(QPointF(12.0, 7.25), QPointF(12.0, 10.5))
        painter.drawLine(QPointF(14.2, 7.25), QPointF(14.2, 11.0))

    painter.end()
    return QIcon(pixmap)


def within_when(session: dict, when: str) -> bool:
    if when == "all":
        return True
    ts = parse_iso(session["startedAt"]) if session["startedAt"] else session["mtimeMs"]
    if not ts:
        return when == "older"

    now = datetime.now().timestamp() * 1000
    one_day = 86400000
    age = now - ts

    if when == "today":
        return age < one_day and datetime.fromtimestamp(ts / 1000).date() == datetime.now().date()
    if when == "week":
        return age < 7 * one_day
    if when == "older":
        return age >= 7 * one_day
    return True


def matches_search(session: dict, q: str) -> bool:
    if not q:
        return True
    haystack = " ".join(filter(None, [
        session.get("title"), session["stem"], session.get("sessionId"),
        session.get("shell"), *(session.get("tags") or []), *(session.get("commands") or []),
    ])).lower()
    return q in haystack


def sort_sessions(sessions: list[dict], mode: str) -> list[dict]:
    def t_of(s):
        if s["startedAt"]:
            t = parse_iso(s["startedAt"])
            if t is not None:
                return t
        return s["mtimeMs"] or 0

    sessions = list(sessions)
    if mode == "date-asc":
        sessions.sort(key=t_of)
    elif mode == "duration-desc":
        sessions.sort(key=lambda s: s["duration"] or 0, reverse=True)
    elif mode == "duration-asc":
        sessions.sort(key=lambda s: s["duration"] or 0)
    elif mode == "commands-desc":
        sessions.sort(key=lambda s: s["commandCount"] or 0, reverse=True)
    elif mode == "title-asc":
        sessions.sort(key=lambda s: (display_title(s) or "").lower())
    else:  # date-desc
        sessions.sort(key=t_of, reverse=True)
    return sessions


# ----------------------------------------------------------------------
# Markdown -> HTML renderer (CommonMark standard with rich styling)
# ----------------------------------------------------------------------

def escape_html(s: str) -> str:
    return htmlmod.escape(str(s), quote=True)


def inline_md(text: str) -> str:
    t = escape_html(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return t


def format_code_block_table(code_str: str, lang: str = "", is_transcript: bool = False) -> str:
    """Render a code block as a sleek, contiguous terminal card for Qt rich text."""
    lines = code_str.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop(-1)

    if not lines:
        return ""

    lang_clean = (lang or "").strip().lower()
    if is_transcript or lang_clean in ("console", "terminal", "shell", "sh", "bash"):
        header_title = "Terminal"
        if lang_clean and lang_clean not in ("console", "terminal"):
            header_title = f"Terminal ({lang_clean})"
    elif lang_clean:
        header_title = lang_clean
    else:
        header_title = "Terminal"

    formatted_lines = []
    for line in lines:
        escaped = htmlmod.escape(line)
        if is_transcript or lang_clean in ("console", "terminal", "shell", "bash"):
            # Format prompt: e.g. (/path) $ cmd or $ cmd or # cmd
            prompt_match = re.match(r"^(\(.*?\)\s*[$#]|\$|#)\s*(.*)$", line)
            if prompt_match:
                p_text = htmlmod.escape(prompt_match.group(1))
                c_text = htmlmod.escape(prompt_match.group(2))
                formatted_lines.append(
                    f'<span style="color: #818cf8; font-weight: bold;">{p_text}</span> '
                    f'<span style="color: #ffffff; font-weight: bold;">{c_text}</span>'
                )
            elif line.startswith("[sudo]") or "[interactive session" in line:
                formatted_lines.append(f'<span style="color: #fbbf24; font-style: italic;">{escaped}</span>')
            elif "exited with code" in line or line.startswith("Error:") or line.startswith("fatal:"):
                formatted_lines.append(f'<span style="color: #f87171; font-weight: bold;">{escaped}</span>')
            else:
                formatted_lines.append(f'<span style="color: #cbd5e1;">{escaped}</span>')
        else:
            if line.strip().startswith("#"):
                formatted_lines.append(f'<span style="color: #64748b; font-style: italic;">{escaped}</span>')
            else:
                formatted_lines.append(f'<span style="color: #f8fafc;">{escaped}</span>')

    code_body = "<br/>".join(formatted_lines)

    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" style="margin: 12px 0; border: 1px solid #334155; background-color: #0f172a; border-radius: 6px;">'
        f'<tr><td style="background-color: #1e293b; color: #94a3b8; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif; font-size: 11px; font-weight: 600; padding: 5px 12px; border-bottom: 1px solid #334155;">'
        f'<span style="color: #ef4444; font-size: 11px;">●</span>&nbsp;<span style="color: #f59e0b; font-size: 11px;">●</span>&nbsp;<span style="color: #10b981; font-size: 11px;">●</span>&nbsp;&nbsp;&nbsp;{escape_html(header_title)}'
        f'</td></tr>'
        f'<tr><td style="padding: 10px 14px; background-color: #0f172a; font-family: \'JetBrains Mono\', \'Cascadia Code\', Menlo, Consolas, monospace; font-size: 12px; line-height: 1.5; color: #f8fafc;">'
        f'{code_body}'
        f'</td></tr>'
        f'</table>'
    )


def render_simple_markdown(md: str, is_transcript: bool = False) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out = []
    in_code = False
    code_buf = []
    code_lang = ""
    list_open = False

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    for line in lines:
        fence = re.match(r"^\s*`{3,}([a-zA-Z0-9_-]*)", line)
        if fence:
            if not in_code:
                in_code = True
                code_lang = fence.group(1).strip()
                code_buf = []
            else:
                in_code = False
                out.append(format_code_block_table("\n".join(code_buf), lang=code_lang, is_transcript=is_transcript))
            continue

        if in_code:
            code_buf.append(line)
            continue

        trimmed = line.strip()
        if not trimmed:
            close_list()
            continue

        if re.match(r"^(---|\*\*\*|___)$", trimmed):
            close_list()
            out.append("<hr/>")
            continue

        m = re.match(r"^###\s+(.*)$", trimmed)
        if m:
            close_list()
            out.append(f"<h3>{inline_md(m.group(1))}</h3>")
            continue
        m = re.match(r"^##\s+(.*)$", trimmed)
        if m:
            close_list()
            out.append(f"<h2>{inline_md(m.group(1))}</h2>")
            continue
        m = re.match(r"^#\s+(.*)$", trimmed)
        if m:
            close_list()
            out.append(f"<h1>{inline_md(m.group(1))}</h1>")
            continue
        m = re.match(r"^[-*]\s+(.*)$", trimmed)
        if m:
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline_md(m.group(1))}</li>")
            continue
        m = re.match(r"^>\s?(.*)$", trimmed)
        if m:
            close_list()
            out.append(f"<blockquote>{inline_md(m.group(1))}</blockquote>")
            continue

        close_list()
        out.append(f"<p>{inline_md(trimmed)}</p>")

    close_list()
    if in_code and code_buf:
        out.append(format_code_block_table("\n".join(code_buf), lang=code_lang, is_transcript=is_transcript))

    return "\n".join(out)


def render_markdown_html(md_text: str, is_transcript: bool = False) -> str:
    """Render Markdown to styled HTML tailored for Qt QTextBrowser with elegant code cards."""
    # 1. Extract fenced code blocks
    code_blocks = []
    def code_replacer(m):
        lang = m.group(1).strip()
        code = m.group(2)
        idx = len(code_blocks)
        table_html = format_code_block_table(code, lang=lang, is_transcript=is_transcript)
        code_blocks.append(table_html)
        return f"\n\n<!--__SHELLBY_CODE_BLOCK_{idx}__-->\n\n"

    processed_md = re.sub(r"```([a-zA-Z0-9_-]*)[ \t]*\r?\n([\s\S]*?)```", code_replacer, md_text)

    # 2. Format transcript metadata header if applicable
    if is_transcript:
        meta_match = re.search(
            r"#\s*Shellby Session Transcript\s*"
            r"-\s*\*\*Session ID:\*\*\s*(.*?)\r?\n"
            r"-\s*\*\*Shell:\*\*\s*(.*?)\r?\n"
            r"-\s*\*\*Started:\*\*\s*(.*?)\r?\n"
            r"-\s*\*\*Ended:\*\*\s*(.*?)\r?\n"
            r"-\s*\*\*Duration:\*\*\s*([0-9.]+s?)\s*"
            r"(---\s*)?",
            processed_md,
        )
        if meta_match:
            sess_id = meta_match.group(1).strip()
            shell_val = meta_match.group(2).strip()
            started_val = meta_match.group(3).strip()
            ended_val = meta_match.group(4).strip()
            dur_val = meta_match.group(5).strip()

            d_started = fmt_date(started_val) or started_val
            dur_num = None
            try:
                dur_num = float(dur_val.rstrip("s"))
            except Exception:
                pass
            d_dur = fmt_duration(dur_num) or dur_val

            meta_card = (
                f'<div style="margin: 4px 0 14px 0;">'
                f'<h1 style="margin: 0 0 10px 0; font-size: 19px; color: #0f172a; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px;">'
                f'Shellby Session Transcript</h1>'
                f'<table width="100%" cellpadding="8" cellspacing="0" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; margin-bottom: 10px;">'
                f'<tr>'
                f'<td style="color: #64748b; font-size: 11px; font-weight: 600; width: 85px;">Session ID:</td>'
                f'<td style="color: #0f172a; font-family: monospace; font-size: 11px;">{escape_html(sess_id)}</td>'
                f'<td style="color: #64748b; font-size: 11px; font-weight: 600; width: 60px;">Shell:</td>'
                f'<td style="color: #0f172a; font-size: 11px;">{escape_html(shell_val)}</td>'
                f'</tr>'
                f'<tr>'
                f'<td style="color: #64748b; font-size: 11px; font-weight: 600;">Started:</td>'
                f'<td style="color: #0f172a; font-size: 11px;">{escape_html(d_started)}</td>'
                f'<td style="color: #64748b; font-size: 11px; font-weight: 600;">Duration:</td>'
                f'<td style="color: #0f172a; font-size: 11px; font-weight: 600;">{escape_html(d_dur)}</td>'
                f'</tr>'
                f'</table>'
                f'</div>\n\n'
            )
            processed_md = processed_md[:meta_match.start()] + meta_card + processed_md[meta_match.end():]
    else:
        # 3. Format category badges in Guide step headings
        badge_styles = {
            "MAINTENANCE": "background-color: #e0e7ff; color: #3730a3; border: 1px solid #c7d2fe;",
            "CONFIGURATION": "background-color: #fef3c7; color: #92400e; border: 1px solid #fde68a;",
            "SETUP": "background-color: #dcfce7; color: #166534; border: 1px solid #bbf7d0;",
            "BUILD": "background-color: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd;",
            "VERIFICATION": "background-color: #f3e8ff; color: #6b21a8; border: 1px solid #e9d5ff;",
            "CLEANUP": "background-color: #f1f5f9; color: #475569; border: 1px solid #cbd5e1;",
        }

        def badge_replacer(m):
            tag = m.group(1).upper()
            b_style = badge_styles.get(tag, "background-color: #e0e7ff; color: #3730a3; border: 1px solid #c7d2fe;")
            return (
                f'<span style="{b_style} font-size: 10px; font-weight: 700; padding: 2px 8px; '
                f'border-radius: 10px; margin-left: 6px; vertical-align: middle;">{tag}</span>'
            )

        processed_md = re.sub(r"\[([A-Za-z0-9_-]+)\]", badge_replacer, processed_md)
        processed_md = re.sub(r"##\s*Result\b", "## <span style='color: #10b981;'>✓</span> Result", processed_md)

    # 4. Render markdown body
    if HAS_MARKDOWN:
        try:
            body = markdown.markdown(processed_md, extensions=["extra", "sane_lists"])
        except Exception:
            body = render_simple_markdown(processed_md, is_transcript=is_transcript)
    else:
        body = render_simple_markdown(processed_md, is_transcript=is_transcript)

    # 5. Style inline code elements
    body = re.sub(
        r"<code>(.*?)</code>",
        r'<code style="background-color: #eef2ff; color: #4338ca; font-family: \'JetBrains Mono\', Consolas, monospace; font-size: 11px; padding: 2px 5px; border: 1px solid #c7d2fe; border-radius: 4px;">\1</code>',
        body,
    )

    # 6. Re-insert the code block tables
    for idx, table_html in enumerate(code_blocks):
        placeholder = f"<!--__SHELLBY_CODE_BLOCK_{idx}__-->"
        body = body.replace(placeholder, table_html)

    # 7. Wrap in styled HTML document
    return f"""<!DOCTYPE html>
    <html>
    <head>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            font-size: 13px;
            line-height: 1.6;
            color: #1e293b;
            background-color: #ffffff;
            margin: 14px 18px;
        }}
        h1 {{
            color: #0f172a;
            font-size: 19px;
            font-weight: 700;
            border-bottom: 1px solid #e2e8f0;
            padding-bottom: 8px;
            margin-top: 6px;
            margin-bottom: 12px;
        }}
        h2 {{
            color: #0f172a;
            font-size: 15px;
            font-weight: 700;
            margin-top: 18px;
            margin-bottom: 8px;
        }}
        h3 {{
            color: #1e293b;
            font-size: 13px;
            font-weight: 600;
            margin-top: 12px;
            margin-bottom: 6px;
        }}
        p {{
            margin: 6px 0;
            color: #334155;
        }}
        ul, ol {{
            padding-left: 20px;
            margin: 6px 0 10px 0;
        }}
        li {{
            margin-bottom: 5px;
            color: #334155;
        }}
        blockquote {{
            border-left: 4px solid #6366f1;
            margin: 10px 0;
            padding: 6px 12px;
            background-color: #f8fafc;
            color: #475569;
            border-radius: 0 4px 4px 0;
        }}
        table {{
            border-collapse: collapse;
            margin: 10px 0;
            font-size: 12px;
        }}
        th, td {{
            border: 1px solid #cbd5e1;
            padding: 6px 10px;
            text-align: left;
        }}
        th {{
            background-color: #f1f5f9;
            font-weight: 600;
            color: #0f172a;
        }}
        hr {{
            border: none;
            border-top: 1px solid #e2e8f0;
            margin: 16px 0;
        }}
        a {{
            color: #4f46e5;
            text-decoration: none;
        }}
    </style>
    </head>
    <body>
    {body}
    </body>
    </html>
    """


# ----------------------------------------------------------------------
# Background Workers for Asynchronous Pipelines (Issue 6.1)
# ----------------------------------------------------------------------

class HeuristicsWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, session_path: Path):
        super().__init__()
        self.session_path = session_path

    def run(self):
        try:
            from shellby_heuristics import session_blocks_payload
            from shellby_markdown import render_markdown

            stem = self.session_path.stem
            blocks_path = self.session_path.with_name(f"{stem}.blocks.json")
            transcript_path = self.session_path.with_name(f"{stem}.transcript.md")

            payload = session_blocks_payload(self.session_path)
            blocks_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

            transcript = render_markdown(blocks_path)
            transcript_path.write_text(transcript, encoding="utf-8")

            self.finished.emit(True, "Heuristics and Markdown transcript successfully generated.")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class CurationWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, blocks_path: Path, out_path: Path, api_key: str | None, model: str, focus: str | None):
        super().__init__()
        self.blocks_path = blocks_path
        self.out_path = out_path
        self.api_key = api_key
        self.model = model
        self.focus = focus

    def run(self):
        try:
            from shellby_curator import curate_file
            from shellby_pdf import render_markdown_to_pdf

            curated = curate_file(
                input_path=self.blocks_path,
                out_path=self.out_path,
                api_key=self.api_key,
                model=self.model,
                focus=self.focus,
            )

            pdf_path = self.out_path.with_suffix(".pdf")
            try:
                render_markdown_to_pdf(curated, pdf_path)
                pdf_note = " Guide Markdown and PDF generated successfully!"
            except Exception as pe:
                pdf_note = f" Guide Markdown saved, but PDF rendering failed: {pe}"

            self.finished.emit(True, f"AI Curation finished.{pdf_note}")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class PdfExportWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, md_path: Path, pdf_path: Path):
        super().__init__()
        self.md_path = md_path
        self.pdf_path = pdf_path

    def run(self):
        try:
            from shellby_pdf import render_pdf_file
            render_pdf_file(self.md_path, self.pdf_path)
            self.finished.emit(True, f"PDF rendered successfully:\n{self.pdf_path.name}")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class FullPipelineWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, session_path: Path, api_key: str | None, model: str, focus: str | None):
        super().__init__()
        self.session_path = session_path
        self.api_key = api_key
        self.model = model
        self.focus = focus

    def run(self):
        try:
            from shellby_heuristics import session_blocks_payload
            from shellby_markdown import render_markdown
            from shellby_curator import curate_file
            from shellby_pdf import render_markdown_to_pdf

            self.progress.emit("Phase 1/3: Parsing terminal output and grouping blocks...")
            stem = self.session_path.stem
            blocks_path = self.session_path.with_name(f"{stem}.blocks.json")
            transcript_path = self.session_path.with_name(f"{stem}.transcript.md")
            curated_path = self.session_path.with_name(f"{stem}.guide.md")
            pdf_path = self.session_path.with_name(f"{stem}.guide.pdf")

            payload = session_blocks_payload(self.session_path)
            blocks_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

            transcript = render_markdown(blocks_path)
            transcript_path.write_text(transcript, encoding="utf-8")

            self.progress.emit("Phase 2/3: Curating technical guide with Gemini...")
            curated = curate_file(
                input_path=blocks_path,
                out_path=curated_path,
                api_key=self.api_key,
                model=self.model,
                focus=self.focus,
            )

            self.progress.emit("Phase 3/3: Rendering PDF document...")
            render_markdown_to_pdf(curated, pdf_path)

            self.finished.emit(True, "Full pipeline completed: Blocks, Transcript, Guide, and PDF generated!")
        except Exception as exc:
            self.finished.emit(False, str(exc))


# ----------------------------------------------------------------------
# FlowLayout - wrapping layout for chips / tag pills
# ----------------------------------------------------------------------

class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect, test_only):
        x, y = rect.x(), rect.y()
        line_height = 0
        for item in self._items:
            space_x, space_y = self._spacing, self._spacing
            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
        return y + line_height - rect.y()


def make_chip(text: str, checkable: bool = True) -> QPushButton:
    btn = QPushButton(text)
    btn.setCheckable(checkable)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet("""
        QPushButton {
            padding: 5px 14px;
            border-radius: 12px;
            border: 1px solid #d1d5db;
            background: #f8fafc;
            color: #374151;
            font-size: 12px;
        }
        QPushButton:checked {
            background: #4f46e5;
            color: white;
            border-color: #4f46e5;
        }
        QPushButton:hover {
            border-color: #6366f1;
            background: #eef2ff;
        }
    """)
    return btn


# ----------------------------------------------------------------------
# Session card widget with Dynamic In-Place Updates (Issue 6.3)
# ----------------------------------------------------------------------

class SessionCard(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, session: dict):
        super().__init__()
        self.stem = session["stem"]
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("SessionCard")
        self._selected = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        title_row = QHBoxLayout()
        self.title_lbl = QLabel(display_title(session))
        self.title_lbl.setStyleSheet("font-weight: 600; color: #0f172a; font-size: 13px;")
        title_row.addWidget(self.title_lbl, 1)

        self.fav_lbl = QLabel()
        if session.get("favorite"):
            self.fav_lbl.setPixmap(create_vector_icon("star", "#f59e0b", 13).pixmap(13, 13))
        title_row.addWidget(self.fav_lbl)
        layout.addLayout(title_row)

        self.meta_lbl = QLabel("")
        self.meta_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        layout.addWidget(self.meta_lbl)

        self.badge_container = QWidget()
        self.badges_layout = FlowLayout(spacing=4)
        self.badge_container.setLayout(self.badges_layout)
        layout.addWidget(self.badge_container)

        self._apply_style()
        self.update_data(session)

    def update_data(self, session: dict):
        """Update card UI in place without destroying or recreating widgets (Issue 6.3)."""
        self.title_lbl.setText(display_title(session))
        if session.get("favorite"):
            self.fav_lbl.setPixmap(create_vector_icon("star", "#f59e0b", 13).pixmap(13, 13))
        else:
            self.fav_lbl.clear()

        meta_bits = []
        d = fmt_date(session.get("startedAt"))
        dur = fmt_duration(session.get("duration"))
        if d:
            meta_bits.append(d)
        if dur:
            meta_bits.append(dur)
        if session.get("shell"):
            meta_bits.append(session["shell"])
        self.meta_lbl.setText("  •  ".join(meta_bits) if meta_bits else "")

        # Clear existing badges
        while self.badges_layout.count():
            item = self.badges_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        has_guide = bool(session["files"].get("guideMd") or session["files"].get("guidePdf"))
        self.badges_layout.addWidget(self._create_badge("Guide" if has_guide else "No guide", has_guide))

        cmd_count = session.get("commandCount", 0)
        if cmd_count > 0:
            self.badges_layout.addWidget(self._create_badge(f"{cmd_count} cmds", None))

        for t in (session.get("tags") or [])[:3]:
            self.badges_layout.addWidget(self._create_badge(t, "tag"))

    def _create_badge(self, text: str, kind):
        lbl = QLabel(text)
        if kind is True:
            style = "background:#dcfce7; color:#166534;"
        elif kind is False:
            style = "background:#fee2e2; color:#991b1b;"
        elif kind == "tag":
            style = "background:#e0e7ff; color:#3730a3;"
        else:
            style = "background:#f1f5f9; color:#475569;"

        lbl.setStyleSheet(f"{style} border-radius: 5px; padding: 1px 6px; font-size: 10px; font-weight: 500;")
        return lbl

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style()

    def _apply_style(self):
        border = "#4f46e5" if self._selected else "#e2e8f0"
        bg = "#eef2ff" if self._selected else "#ffffff"
        self.setStyleSheet(f"""
            #SessionCard {{
                background: {bg};
                border-radius: 8px;
                border: 1.5px solid {border};
            }}
            #SessionCard:hover {{
                border-color: #6366f1;
            }}
        """)

    def mousePressEvent(self, event):
        self.clicked.emit(self.stem)
        super().mousePressEvent(event)


# ----------------------------------------------------------------------
# Main window with Issue 6.1-6.7 enhancements
# ----------------------------------------------------------------------

class ShellbyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shellby - Session Manager & Guide Curator")
        self.resize(1320, 840)

        self.settings = QSettings(APP_ORG, APP_NAME)
        self.folder = None
        self.sessions = []
        self.selected_stem = None
        self.search_q = ""
        self.when = "all"
        self.tag = None
        self.favorites_only = False
        self.sort_mode = "date-desc"
        self.card_widgets = {}

        # Per-session pending saves map preventing race conditions (Issue 6.2)
        self._pending_saves = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_pending_saves)

        # Debounced search timer preventing re-render lag (Issue 6.3)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self._apply_filters_and_visibility)

        # Timestamp tracking preventing inotify loop thrashing (Issue 6.4)
        self._last_internal_write_time = 0.0

        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_dir_changed)
        self._rescan_debounce = QTimer(self)
        self._rescan_debounce.setSingleShot(True)
        self._rescan_debounce.timeout.connect(lambda: self.open_folder(self.folder, keep_selection=True))

        # Synchronize remembered API key with environment if available
        saved_api_key = self.settings.value("shellby/api_key", "")
        if saved_api_key and str(saved_api_key).strip():
            os.environ["GEMINI_API_KEY"] = str(saved_api_key).strip()

        self._build_ui()
        self._apply_theme()
        self._setup_shortcuts()

        last_folder = self.settings.value("shellby/folder", "")
        if last_folder and os.path.isdir(last_folder):
            self.open_folder(last_folder)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+O"), self, self.choose_folder)
        QShortcut(QKeySequence("Ctrl+K"), self, self.configure_api_key)
        QShortcut(QKeySequence("Ctrl+R"), self, self._on_record_session_clicked)
        QShortcut(QKeySequence("Ctrl+F"), self, self.search_input.setFocus)
        QShortcut(QKeySequence("Ctrl+S"), self, self._flush_pending_saves)
        QShortcut(QKeySequence("F5"), self, lambda: self.folder and self.open_folder(self.folder, keep_selection=True))

    # ---------------- UI construction ----------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_workspace())
        splitter.setSizes([380, 940])

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.setStyleSheet("background: #f8fafc; border-top: 1px solid #e2e8f0; color: #64748b; font-size: 11px;")
        self.status_msg = QLabel("Ready")
        self.status_saved = QLabel("All changes saved ✓")
        self.status_saved.setStyleSheet("color: #10b981; font-weight: 500;")
        self.status_bar.addWidget(self.status_msg, 1)
        self.status_bar.addPermanentWidget(self.status_saved)

    def _build_sidebar(self):
        panel = QWidget()
        panel.setObjectName("Sidebar")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Record button (Issue 6.1)
        record_btn = QPushButton(" Record New Session")
        record_btn.setIcon(create_vector_icon("record", "#ffffff", 14))
        record_btn.setIconSize(QSize(14, 14))
        record_btn.setToolTip("Start a new terminal session recording (Ctrl+R)")
        record_btn.setCursor(Qt.PointingHandCursor)
        record_btn.setStyleSheet("""
            QPushButton {
                background: #4f46e5;
                color: white;
                font-weight: 700;
                padding: 10px 18px;
                border-radius: 6px;
                border: none;
                font-size: 13px;
                min-height: 20px;
            }
            QPushButton:hover {
                background: #4338ca;
            }
            QPushButton:pressed {
                background: #3730a3;
            }
        """)
        record_btn.clicked.connect(self._on_record_session_clicked)
        layout.addWidget(record_btn)

        # Folder row
        folder_row = QHBoxLayout()
        self.folder_lbl = QLabel("No folder selected")
        self.folder_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        self.folder_lbl.setWordWrap(True)
        choose_btn = QPushButton("Choose Folder…")
        choose_btn.setToolTip("Select Shellby sessions folder (Ctrl+O)")
        choose_btn.clicked.connect(self.choose_folder)
        rescan_btn = QPushButton()
        rescan_btn.setIcon(create_vector_icon("refresh", "#475569", 14))
        rescan_btn.setIconSize(QSize(14, 14))
        rescan_btn.setFixedSize(34, 32)
        rescan_btn.setToolTip("Rescan folder (F5)")
        rescan_btn.clicked.connect(lambda: self.folder and self.open_folder(self.folder, keep_selection=True))
        layout.addWidget(self.folder_lbl)
        folder_row.addWidget(choose_btn, 1)
        folder_row.addWidget(rescan_btn)
        layout.addLayout(folder_row)

        # Gemini API Key configuration row
        api_box = QFrame()
        api_box.setStyleSheet("background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;")
        api_layout = QHBoxLayout(api_box)
        api_layout.setContentsMargins(10, 6, 8, 6)
        api_layout.setSpacing(8)

        self.api_key_lbl = QLabel()
        self._update_api_key_status_label()

        self.api_key_btn = QPushButton(" API Key…")
        self.api_key_btn.setIcon(create_vector_icon("key", "#475569", 14))
        self.api_key_btn.setIconSize(QSize(14, 14))
        self.api_key_btn.setToolTip("Configure and remember Gemini API Key (Ctrl+K)")
        self.api_key_btn.clicked.connect(self.configure_api_key)

        api_layout.addWidget(self.api_key_lbl, 1)
        api_layout.addWidget(self.api_key_btn)
        layout.addWidget(api_box)

        # Stats
        stats_row = QHBoxLayout()
        self.stat_sessions = self._stat_box("Sessions")
        self.stat_guides = self._stat_box("Guides")
        self.stat_commands = self._stat_box("Commands")
        stats_row.addWidget(self.stat_sessions)
        stats_row.addWidget(self.stat_guides)
        stats_row.addWidget(self.stat_commands)
        layout.addLayout(stats_row)

        # Search with debouncing (Issue 6.3)
        search_box = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search sessions, commands, tags… (Ctrl+F)")
        self.search_input.setEnabled(False)
        self.search_input.textChanged.connect(lambda: self._search_debounce.start(180))
        self.search_clear_btn = QPushButton("✕")
        self.search_clear_btn.setFixedWidth(26)
        self.search_clear_btn.setStyleSheet("border:none; color:#94a3b8; font-weight:bold;")
        self.search_clear_btn.clicked.connect(self.search_input.clear)
        search_box.addWidget(self.search_input, 1)
        search_box.addWidget(self.search_clear_btn)
        layout.addLayout(search_box)

        # Sort
        self.sort_combo = QComboBox()
        self.sort_combo.setEnabled(False)
        self.sort_combo.addItems([
            "Newest first", "Oldest first", "Longest duration", "Shortest duration",
            "Most commands", "Title A–Z",
        ])
        self._sort_values = [
            "date-desc", "date-asc", "duration-desc", "duration-asc",
            "commands-desc", "title-asc"
        ]
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        layout.addWidget(self.sort_combo)

        # Date filter chips
        date_row = QHBoxLayout()
        self.date_chips = {}
        for key, label in [("all", "All"), ("today", "Today"), ("week", "This week"), ("older", "Older")]:
            chip = make_chip(label)
            chip.setChecked(key == "all")
            chip.clicked.connect(lambda _=False, k=key: self._on_when_changed(k))
            self.date_chips[key] = chip
            date_row.addWidget(chip)
        layout.addLayout(date_row)

        # Favorites toggle
        self.fav_toggle = make_chip(" Favorites only")
        self.fav_toggle.setIcon(create_vector_icon("star", "#f59e0b", 13))
        self.fav_toggle.setIconSize(QSize(13, 13))
        self.fav_toggle.clicked.connect(self._on_favorites_toggled)
        layout.addWidget(self.fav_toggle)

        # Tag filters
        tag_lbl = QLabel("TAGS")
        tag_lbl.setStyleSheet("color: #64748b; font-size: 10px; font-weight: 700; margin-top: 4px;")
        layout.addWidget(tag_lbl)
        self.tag_filters_widget = QWidget()
        self.tag_filters_layout = FlowLayout(spacing=6)
        self.tag_filters_widget.setLayout(self.tag_filters_layout)
        layout.addWidget(self.tag_filters_widget)

        # Session list
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch(1)
        self.cards_scroll.setWidget(self.cards_container)
        layout.addWidget(self.cards_scroll, 1)

        self.empty_hint_lbl = QLabel("Choose a folder to get started.")
        self.empty_hint_lbl.setStyleSheet("color: #64748b; font-size: 12px;")
        self.empty_hint_lbl.setAlignment(Qt.AlignCenter)
        self.empty_hint_lbl.setWordWrap(True)
        layout.addWidget(self.empty_hint_lbl)

        return panel

    def _stat_box(self, label: str):
        box = QFrame()
        box.setStyleSheet("""
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
        """)
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 6, 8, 6)

        num = QLabel("0")
        num.setStyleSheet("color: #0f172a; font-size: 16px; font-weight: 700;")
        num.setAlignment(Qt.AlignCenter)

        lbl = QLabel(label)
        lbl.setStyleSheet("color: #64748b; font-size: 10px; font-weight: 500;")
        lbl.setAlignment(Qt.AlignCenter)

        v.addWidget(num)
        v.addWidget(lbl)
        box.num_label = num
        return box

    def _build_workspace(self):
        self.workspace_stack = QWidget()
        outer = QVBoxLayout(self.workspace_stack)
        outer.setContentsMargins(0, 0, 0, 0)

        self.ws_empty = QLabel("Select a session to view details.")
        self.ws_empty.setAlignment(Qt.AlignCenter)
        self.ws_empty.setStyleSheet("color: #64748b; font-size: 14px;")

        self.ws_content = QWidget()
        content_layout = QVBoxLayout(self.ws_content)
        content_layout.setContentsMargins(22, 18, 22, 18)
        content_layout.setSpacing(10)

        # Title & Favorite header row
        header_row = QHBoxLayout()
        self.ws_title_input = QLineEdit()
        self.ws_title_input.setStyleSheet("""
            font-size: 18px;
            font-weight: 700;
            border: none;
            background: transparent;
            color: #0f172a;
        """)
        self.ws_title_input.textChanged.connect(self._on_title_changed)
        self.ws_favorite_btn = QPushButton()
        self.ws_favorite_btn.setIcon(create_vector_icon("star_outline", "#f59e0b", 16))
        self.ws_favorite_btn.setIconSize(QSize(16, 16))
        self.ws_favorite_btn.setToolTip("Toggle favorite")
        self.ws_favorite_btn.setFixedSize(36, 32)
        self.ws_favorite_btn.setStyleSheet("border: 1px solid #e2e8f0; border-radius: 6px; background: #ffffff;")
        self.ws_favorite_btn.clicked.connect(self._toggle_favorite)
        header_row.addWidget(self.ws_title_input, 1)
        header_row.addWidget(self.ws_favorite_btn)
        content_layout.addLayout(header_row)

        self.ws_meta_lbl = QLabel("")
        self.ws_meta_lbl.setStyleSheet("color: #64748b; font-size: 12px;")
        content_layout.addWidget(self.ws_meta_lbl)

        # Tags
        tag_row = QVBoxLayout()
        self.ws_tags_widget = QWidget()
        self.ws_tags_layout = FlowLayout(spacing=6)
        self.ws_tags_widget.setLayout(self.ws_tags_layout)
        tag_row.addWidget(self.ws_tags_widget)
        self.ws_tag_input = QLineEdit()
        self.ws_tag_input.setPlaceholderText("Add a tag and press Enter…")
        self.ws_tag_input.returnPressed.connect(self._add_tag)
        tag_row.addWidget(self.ws_tag_input)
        content_layout.addLayout(tag_row)

        # Notes
        notes_lbl = QLabel("NOTES")
        notes_lbl.setStyleSheet("color: #64748b; font-size: 10px; font-weight: 700;")
        content_layout.addWidget(notes_lbl)
        self.ws_notes = QTextEdit()
        self.ws_notes.setFixedHeight(65)
        self.ws_notes.setPlaceholderText("Notes about this session (autosaved)…")
        self.ws_notes.textChanged.connect(
            lambda: self._queue_save(self.selected_stem, {"notes": self.ws_notes.toPlainText()})
        )
        content_layout.addWidget(self.ws_notes)

        # Pipeline action toolbar (Issue 6.1)
        pipeline_box = QFrame()
        pipeline_box.setObjectName("PipelineBox")
        pipeline_box.setStyleSheet("""
            #PipelineBox {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
            }
            #PipelineBox QPushButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 7px 18px;
                min-height: 24px;
                font-size: 12px;
                font-weight: 600;
                color: #334155;
            }
            #PipelineBox QPushButton:hover {
                background: #f1f5f9;
                border-color: #6366f1;
            }
            #PipelineBox QPushButton:pressed {
                background: #e2e8f0;
            }
            #PipelineBox QPushButton:disabled {
                background: #f8fafc;
                color: #94a3b8;
                border-color: #e2e8f0;
            }
        """)
        pipeline_layout = QHBoxLayout(pipeline_box)
        pipeline_layout.setContentsMargins(10, 8, 10, 8)
        pipeline_layout.setSpacing(10)

        self.btn_run_heuristics = QPushButton(" Parse && Blocks")
        self.btn_run_heuristics.setIcon(create_vector_icon("parse", "#334155", 15))
        self.btn_run_heuristics.setIconSize(QSize(15, 15))
        self.btn_run_heuristics.setToolTip("Run Heuristics: extract commands into blocks.json and transcript.md")
        self.btn_run_heuristics.clicked.connect(self._run_heuristics_pipeline)

        self.btn_ai_curate = QPushButton(" AI Curate Guide")
        self.btn_ai_curate.setIcon(create_vector_icon("ai", "#4338ca", 15))
        self.btn_ai_curate.setIconSize(QSize(15, 15))
        self.btn_ai_curate.setToolTip("Transform commands into clean step-by-step guide with Gemini")
        self.btn_ai_curate.setStyleSheet("""
            QPushButton {
                background: #eef2ff;
                color: #4338ca;
                font-weight: 700;
                border: 1px solid #c7d2fe;
            }
            QPushButton:hover {
                background: #e0e7ff;
                border-color: #818cf8;
            }
        """)
        self.btn_ai_curate.clicked.connect(self._run_ai_curate_pipeline)

        self.btn_export_pdf = QPushButton(" Render PDF")
        self.btn_export_pdf.setIcon(create_vector_icon("pdf", "#334155", 15))
        self.btn_export_pdf.setIconSize(QSize(15, 15))
        self.btn_export_pdf.setToolTip("Export Guide or Transcript directly to PDF")
        self.btn_export_pdf.clicked.connect(self._export_pdf_direct)

        self.btn_full_pipeline = QPushButton(" Full Pipeline")
        self.btn_full_pipeline.setIcon(create_vector_icon("pipeline", "#3730a3", 15))
        self.btn_full_pipeline.setIconSize(QSize(15, 15))
        self.btn_full_pipeline.setToolTip("Run Parse -> AI Curate -> PDF Export in one click")
        self.btn_full_pipeline.setStyleSheet("""
            QPushButton {
                background: #eef2ff;
                color: #3730a3;
                font-weight: 700;
                border: 1px solid #c7d2fe;
            }
            QPushButton:hover {
                background: #e0e7ff;
                border-color: #818cf8;
            }
        """)
        self.btn_full_pipeline.clicked.connect(self._run_full_pipeline)

        self.open_pdf_btn = QPushButton(" Open PDF")
        self.open_pdf_btn.setIcon(create_vector_icon("open_pdf", "#334155", 14))
        self.open_pdf_btn.setIconSize(QSize(14, 14))
        self.open_pdf_btn.setToolTip("Open generated PDF in system viewer")
        self.open_pdf_btn.clicked.connect(self._open_pdf)

        self.reveal_btn = QPushButton(" Reveal")
        self.reveal_btn.setIcon(create_vector_icon("reveal", "#334155", 14))
        self.reveal_btn.setIconSize(QSize(14, 14))
        self.reveal_btn.setToolTip("Open session directory in file manager")
        self.reveal_btn.clicked.connect(self._reveal_in_folder)

        self.delete_btn = QPushButton(" Delete")
        self.delete_btn.setIcon(create_vector_icon("delete", "#ef4444", 14))
        self.delete_btn.setIconSize(QSize(14, 14))
        self.delete_btn.setToolTip("Delete all session artifacts and clean empty folders")
        self.delete_btn.setStyleSheet("""
            QPushButton {
                color: #ef4444;
                font-weight: 600;
                border: 1px solid #fecaca;
                background: #fff5f5;
            }
            QPushButton:hover {
                background: #fee2e2;
                border-color: #f87171;
            }
        """)
        self.delete_btn.clicked.connect(self._delete_session)

        pipeline_layout.addWidget(self.btn_run_heuristics)
        pipeline_layout.addWidget(self.btn_ai_curate)
        pipeline_layout.addWidget(self.btn_export_pdf)
        pipeline_layout.addWidget(self.btn_full_pipeline)
        pipeline_layout.addWidget(self.open_pdf_btn)
        pipeline_layout.addWidget(self.reveal_btn)
        pipeline_layout.addStretch(1)
        pipeline_layout.addWidget(self.delete_btn)
        content_layout.addWidget(pipeline_box)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.tabBar().setFocusPolicy(Qt.NoFocus)
        self.tabs.tabBar().setMinimumHeight(40)
        self.overview_body = QTextBrowser()
        self.overview_body.setOpenExternalLinks(True)
        self.overview_body.setFrameShape(QFrame.NoFrame)
        self.panel_transcript = QTextBrowser()
        self.panel_transcript.setOpenExternalLinks(True)
        self.panel_transcript.setFrameShape(QFrame.NoFrame)
        self.panel_guide = QTextBrowser()
        self.panel_guide.setOpenExternalLinks(True)
        self.panel_guide.setFrameShape(QFrame.NoFrame)
        self.panel_session = QPlainTextEdit()
        self.panel_session.setReadOnly(True)
        self.panel_session.setFrameShape(QFrame.NoFrame)
        self.panel_blocks = QPlainTextEdit()
        self.panel_blocks.setReadOnly(True)
        self.panel_blocks.setFrameShape(QFrame.NoFrame)

        mono = QFont("JetBrains Mono, Menlo, Consolas, monospace", 10)
        self.panel_session.setFont(mono)
        self.panel_blocks.setFont(mono)

        self.tabs.addTab(self.overview_body, "Overview")
        self.tabs.addTab(self.panel_transcript, "Transcript")
        self.tabs.addTab(self.panel_guide, "Guide")
        self.tabs.addTab(self.panel_session, "session.json")
        self.tabs.addTab(self.panel_blocks, "blocks.json")
        content_layout.addWidget(self.tabs, 1)

        outer.addWidget(self.ws_empty)
        outer.addWidget(self.ws_content)
        self.ws_content.hide()
        return self.workspace_stack

    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #f8fafc;
                color: #0f172a;
            }
            #Sidebar {
                background: #f8fafc;
                border-right: 1px solid #e2e8f0;
            }
            QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QTextBrowser {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 6px;
                color: #0f172a;
                selection-background-color: #c7d2fe;
                selection-color: #0f172a;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus, QTextBrowser:focus {
                border: 1px solid #4f46e5;
            }
            QLineEdit:disabled, QComboBox:disabled {
                background: #f1f5f9;
                color: #94a3b8;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 7px 16px;
                min-height: 22px;
                color: #334155;
            }
            QPushButton:hover {
                background: #f1f5f9;
                border-color: #6366f1;
            }
            QPushButton:pressed {
                background: #e2e8f0;
            }
            QPushButton:disabled {
                background: #f1f5f9;
                color: #94a3b8;
                border-color: #e2e8f0;
            }
            QTabWidget::pane {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
            }
            QTabBar {
                qproperty-drawBase: 0;
            }
            QTabBar::tab {
                background: #f8fafc;
                color: #64748b;
                padding: 8px 24px;
                min-height: 24px;
                min-width: 110px;
                margin-right: 6px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                border: 1px solid #e2e8f0;
                border-bottom: none;
                font-size: 13px;
                font-weight: 500;
            }
            QTabBar::tab:hover {
                background: #eef2ff;
                color: #4f46e5;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #4f46e5;
                font-weight: 700;
                border: 1px solid #cbd5e1;
                border-bottom: 2px solid #4f46e5;
            }
            QTextBrowser {
                background: #ffffff;
                border: none;
                padding: 10px 14px;
                selection-background-color: #c7d2fe;
                selection-color: #0f172a;
            }
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: #f1f5f9;
                width: 8px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #cbd5e1;
                min-height: 26px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #94a3b8;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

    # ---------------- Folder handling ----------------

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose your Shellby sessions folder")
        if folder:
            self.open_folder(folder)

    def open_folder(self, folder: str, keep_selection: bool = False):
        self._flush_pending_saves()
        self.folder = folder
        self.settings.setValue("shellby/folder", folder)
        self.folder_lbl.setText(folder)
        self.folder_lbl.setToolTip(folder)
        self.search_input.setEnabled(True)
        self.sort_combo.setEnabled(True)
        self.status_msg.setText(f"Folder: {folder}")

        try:
            self.sessions = scan_folder(folder)
        except Exception as exc:
            self.empty_hint_lbl.setText(f"Error scanning folder: {exc}")
            self.empty_hint_lbl.show()
            return

        self._watch_folder(folder)
        self._render_sidebar_stats()
        self._render_sidebar_tags()
        self._render_cards()

        if not keep_selection:
            self.selected_stem = None
            self._show_workspace_empty()
        elif self.selected_stem:
            still = next((s for s in self.sessions if s["stem"] == self.selected_stem), None)
            if still:
                self._select_session(self.selected_stem)
            else:
                self._show_workspace_empty()

    def _watch_folder(self, folder: str):
        """Watch top-level and immediate subdirectories, capped to prevent descriptor exhaustion (Issue 6.4)."""
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        dirs_to_watch = [folder]
        try:
            entries = os.listdir(folder)
            for entry in entries[:40]:
                p = os.path.join(folder, entry)
                if os.path.isdir(p) and not entry.startswith("."):
                    dirs_to_watch.append(p)
        except OSError:
            pass

        if dirs_to_watch:
            self.watcher.addPaths(dirs_to_watch)

    def _on_dir_changed(self, _path: str):
        """Ignore self-inflicted metadata writes to break inotify feedback loops (Issue 6.4)."""
        if time.time() - self._last_internal_write_time < 1.2:
            return
        self._rescan_debounce.start(400)

    # ---------------- API Key handling ----------------

    def get_api_key(self) -> str | None:
        """Retrieve remembered Gemini API key from QSettings or GEMINI_API_KEY env."""
        saved = self.settings.value("shellby/api_key", "")
        if saved and str(saved).strip():
            return str(saved).strip()
        env_val = os.environ.get("GEMINI_API_KEY", "").strip()
        return env_val or None

    def set_api_key(self, key: str | None) -> None:
        """Persist Gemini API key in QSettings and update current process environment."""
        clean_key = (key or "").strip()
        if clean_key:
            self.settings.setValue("shellby/api_key", clean_key)
            os.environ["GEMINI_API_KEY"] = clean_key
        else:
            self.settings.remove("shellby/api_key")
            if "GEMINI_API_KEY" in os.environ:
                del os.environ["GEMINI_API_KEY"]
        self._update_api_key_status_label()

    def _update_api_key_status_label(self):
        if not hasattr(self, "api_key_lbl"):
            return
        key = self.get_api_key()
        if key:
            masked = "••••••••" + key[-4:] if len(key) > 4 else "••••"
            self.api_key_lbl.setText(f"API Key: {masked}")
            self.api_key_lbl.setStyleSheet("color: #059669; font-size: 11px; font-weight: 500;")
            self.api_key_lbl.setToolTip(f"Gemini API key is configured and remembered ({masked})")
        else:
            self.api_key_lbl.setText("API Key: Not configured")
            self.api_key_lbl.setStyleSheet("color: #dc2626; font-size: 11px; font-weight: 500;")
            self.api_key_lbl.setToolTip("Click to configure Gemini API Key for AI guide curation")

    def configure_api_key(self):
        """Display dialog allowing user to view, edit, save, or clear their Gemini API key."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Gemini API Key Settings")
        dialog.resize(460, 220)
        v = QVBoxLayout(dialog)
        v.setSpacing(12)
        v.setContentsMargins(18, 18, 18, 18)

        desc = QLabel(
            "Configure your Gemini API Key. Just like the folder location, it is saved "
            "in local settings and remembered across sessions for AI guide curation and full pipeline runs."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #475569; font-size: 12px; line-height: 1.4;")
        v.addWidget(desc)

        key_layout = QVBoxLayout()
        key_lbl = QLabel("Gemini API Key:")
        key_lbl.setStyleSheet("font-weight: 600; font-size: 12px; color: #0f172a;")
        key_layout.addWidget(key_lbl)

        key_input = QLineEdit()
        key_input.setEchoMode(QLineEdit.Password)
        key_input.setPlaceholderText("Paste your Gemini API Key (e.g. AIzaSy…)")
        current_key = self.get_api_key() or ""
        key_input.setText(current_key)
        key_layout.addWidget(key_input)

        show_cb = QCheckBox("Show key text")
        show_cb.setStyleSheet("font-size: 11px; color: #64748b;")
        show_cb.toggled.connect(lambda checked: key_input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password))
        key_layout.addWidget(show_cb)
        v.addLayout(key_layout)

        btns_row = QHBoxLayout()
        clear_btn = QPushButton("Clear Key")
        clear_btn.setStyleSheet("color: #ef4444; border-color: #fca5a5;")
        clear_btn.setToolTip("Remove saved API key")

        def on_clear():
            self.set_api_key(None)
            self.status_msg.setText("Gemini API key cleared.")
            dialog.accept()
            QMessageBox.information(self, "API Key Cleared", "Gemini API key has been cleared from local settings.")

        clear_btn.clicked.connect(on_clear)
        btns_row.addWidget(clear_btn)
        btns_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        btns_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save Key")
        save_btn.setStyleSheet("background: #4f46e5; color: white; font-weight: 700; border: none; padding: 7px 18px;")

        def on_save():
            new_key = key_input.text().strip()
            self.set_api_key(new_key if new_key else None)
            if new_key:
                self.status_msg.setText("Gemini API key saved.")
                QMessageBox.information(self, "API Key Saved", "Gemini API key has been successfully saved!")
            else:
                self.status_msg.setText("Gemini API key cleared.")
            dialog.accept()

        save_btn.clicked.connect(on_save)
        btns_row.addWidget(save_btn)
        v.addLayout(btns_row)

        dialog.exec_()

    # ---------------- Sidebar rendering ----------------

    def _render_sidebar_stats(self):
        self.stat_sessions.num_label.setText(str(len(self.sessions)))
        guides = sum(1 for s in self.sessions if s["files"].get("guideMd") or s["files"].get("guidePdf"))
        self.stat_guides.num_label.setText(str(guides))
        commands = sum(s["commandCount"] for s in self.sessions)
        self.stat_commands.num_label.setText(str(commands))

    def _render_sidebar_tags(self):
        while self.tag_filters_layout.count():
            item = self.tag_filters_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        tag_counts: dict[str, int] = {}
        for s in self.sessions:
            for t in s.get("tags") or []:
                tag_counts[t] = tag_counts.get(t, 0) + 1

        if not tag_counts:
            lbl = QLabel("No tags yet")
            lbl.setStyleSheet("color:#94a3b8; font-size:11px;")
            self.tag_filters_layout.addWidget(lbl)
            return

        for tag, count in sorted(tag_counts.items(), key=lambda kv: -kv[1]):
            chip = make_chip(f"{tag} ({count})")
            chip.setChecked(self.tag == tag)
            chip.clicked.connect(lambda _=False, t=tag: self._on_tag_changed(t))
            self.tag_filters_layout.addWidget(chip)

    def _get_filtered_sessions(self) -> list[dict]:
        filtered = [
            s for s in self.sessions
            if within_when(s, self.when)
            and matches_search(s, self.search_q)
            and (not self.favorites_only or s["favorite"])
            and (not self.tag or self.tag in (s.get("tags") or []))
        ]
        return sort_sessions(filtered, self.sort_mode)

    def _render_cards(self):
        """Instantiate card widgets only once when sessions change (Issue 6.3)."""
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.card_widgets = {}

        if not self.sessions:
            self.empty_hint_lbl.setText("Choose a folder to get started.")
            self.empty_hint_lbl.show()
            return

        for s in self.sessions:
            card = SessionCard(s)
            card.set_selected(s["stem"] == self.selected_stem)
            card.clicked.connect(self._select_session)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self.card_widgets[s["stem"]] = card

        self._apply_filters_and_visibility()

    def _apply_filters_and_visibility(self):
        """Toggle card visibility rather than destroying and recreating widget trees (Issue 6.3)."""
        self.search_q = self.search_input.text().strip().lower()
        flist = self._get_filtered_sessions()
        visible_stems = {s["stem"] for s in flist}

        if not self.sessions:
            self.empty_hint_lbl.setText("Choose a folder to get started.")
            self.empty_hint_lbl.show()
        elif not flist:
            self.empty_hint_lbl.setText("No sessions match the current filters.")
            self.empty_hint_lbl.show()
        else:
            self.empty_hint_lbl.hide()

        for stem, card in self.card_widgets.items():
            card.setVisible(stem in visible_stems)

    # ---------------- Filter/search/sort handlers ----------------

    def _on_sort_changed(self, idx: int):
        self.sort_mode = self._sort_values[idx]
        self._render_cards()

    def _on_when_changed(self, key: str):
        self.when = key
        for k, chip in self.date_chips.items():
            chip.setChecked(k == key)
        self._apply_filters_and_visibility()

    def _on_favorites_toggled(self):
        self.favorites_only = self.fav_toggle.isChecked()
        self._apply_filters_and_visibility()

    def _on_tag_changed(self, tag: str):
        self.tag = None if self.tag == tag else tag
        self._render_sidebar_tags()
        self._apply_filters_and_visibility()

    # ---------------- Workspace ----------------

    def _show_workspace_empty(self):
        self.ws_empty.show()
        self.ws_content.hide()

    def _current_session(self) -> dict | None:
        return next((s for s in self.sessions if s["stem"] == self.selected_stem), None)

    def _select_session(self, stem: str):
        # Flush any pending metadata saves before switching session (Issue 6.2)
        self._flush_pending_saves()

        self.selected_stem = stem
        for s, card in self.card_widgets.items():
            card.set_selected(s == stem)

        session = self._current_session()
        if not session:
            self._show_workspace_empty()
            return

        self.ws_empty.hide()
        self.ws_content.show()

        self.ws_title_input.blockSignals(True)
        self.ws_title_input.setText(session.get("title") or "")
        self.ws_title_input.setPlaceholderText(base_stem(session["stem"]))
        self.ws_favorite_btn.setIcon(
            create_vector_icon("star" if session.get("favorite") else "star_outline", "#f59e0b", 16)
        )

        self.ws_notes.blockSignals(True)
        self.ws_notes.setPlainText(session.get("notes") or "")
        self.ws_notes.blockSignals(False)

        meta_parts = []
        d = fmt_date(session["startedAt"])
        dur = fmt_duration(session["duration"])
        if d:
            meta_parts.append(d)
        if dur:
            meta_parts.append(dur)
        if session.get("shell"):
            meta_parts.append(session["shell"])
        meta_parts.append(f'{session["commandCount"]} commands')
        self.ws_meta_lbl.setText("  •  ".join(meta_parts))

        self._render_tag_pills(session)
        self._update_action_button_states(session)
        self._render_overview_tab(session)

        self._load_markdown_panel(self.panel_transcript, session["files"].get("transcript"), "No transcript.md generated for this session.", is_transcript=True)
        self._load_markdown_panel(self.panel_guide, session["files"].get("guideMd"), "No guide.md generated for this session.", is_transcript=False)
        self._load_json_panel(self.panel_session, session["files"].get("session"), "No session.json found for this session.")
        self._load_json_panel(self.panel_blocks, session["files"].get("blocks"), "No blocks.json found for this session.")

    def _update_action_button_states(self, session: dict):
        has_session = bool(session["files"].get("session"))
        has_blocks = bool(session["files"].get("blocks"))
        has_guide_md = bool(session["files"].get("guideMd"))
        has_transcript_md = bool(session["files"].get("transcript"))
        has_pdf = bool(session["files"].get("guidePdf") or session["files"].get("transcriptPdf"))

        self.btn_run_heuristics.setEnabled(has_session)
        self.btn_ai_curate.setEnabled(has_blocks)
        self.btn_export_pdf.setEnabled(has_guide_md or has_transcript_md)
        self.btn_full_pipeline.setEnabled(has_session)
        self.open_pdf_btn.setEnabled(has_pdf)

    def _render_overview_tab(self, session: dict):
        structured = session.get("structuredBlocks") or []
        success_count = sum(1 for b in structured if b.get("exit_code") == 0)
        failure_count = sum(1 for b in structured if b.get("exit_code") is not None and b.get("exit_code") != 0)

        overview_html = f"""
        <div style="font-size:12px; margin-bottom: 12px;">
            <table style="width:100%; border:none; margin:0;">
                <tr style="background:none;">
                    <td style="border:none; padding:4px;"><b>Commands:</b> {session["commandCount"]}</td>
                    <td style="border:none; padding:4px;"><b>Duration:</b> {fmt_duration(session["duration"]) or "—"}</td>
                    <td style="border:none; padding:4px;"><b>Shell:</b> {escape_html(session.get("shell") or "—")}</td>
                    <td style="border:none; padding:4px;"><b>Status:</b>
                        <span style="color:#16a34a; font-weight:600;">{success_count} passed</span>
                        {f', <span style="color:#dc2626; font-weight:600;">{failure_count} failed</span>' if failure_count else ''}
                    </td>
                </tr>
            </table>
        </div><hr/>
        """

        if structured:
            overview_html += "<div style='font-family:monospace; font-size:12px;'>"
            for b in structured:
                ec = b.get("exit_code")
                if ec == 0:
                    status_badge = "<span style='color:#16a34a; font-weight:bold;'>✓ 0</span>"
                elif ec is not None:
                    status_badge = f"<span style='color:#dc2626; font-weight:bold;'>✕ {ec}</span>"
                else:
                    status_badge = "<span style='color:#94a3b8;'>—</span>"

                cwd_str = f" <span style='color:#6366f1; font-size:10px;'>[{escape_html(b['cwd'])}]</span>" if b.get("cwd") else ""
                cmd_escaped = escape_html(b["command"])
                overview_html += f"<div style='margin: 4px 0; padding: 4px 8px; background: #f8fafc; border-radius: 4px;'>{status_badge} <b>$</b> {cmd_escaped}{cwd_str}</div>"
            overview_html += "</div>"
        elif session["commands"]:
            overview_html += "<div style='font-family:monospace; font-size:12px;'>"
            for c in session["commands"]:
                overview_html += f"<div style='margin: 3px 0;'>$ {escape_html(c)}</div>"
            overview_html += "</div>"
        else:
            overview_html += "<i style='color:#94a3b8;'>No blocks generated yet. Click 'Parse & Blocks' to extract structured command blocks.</i>"

        self.overview_body.setHtml(overview_html)

    def _load_markdown_panel(self, widget: QTextBrowser, filename: str | None, empty_msg: str, is_transcript: bool = False):
        if not filename or not self.folder:
            widget.setHtml(f"<i style='color:#94a3b8; font-family:sans-serif;'>{escape_html(empty_msg)}</i>")
            return
        try:
            full_path = os.path.join(self.folder, filename)
            with open(full_path, "r", encoding="utf-8") as f:
                raw = f.read()
            widget.setHtml(render_markdown_html(raw, is_transcript=is_transcript))
        except Exception as exc:
            widget.setHtml(f"<i style='color:#f87171;'>Could not read {escape_html(filename)}: {escape_html(str(exc))}</i>")

    def _load_json_panel(self, widget: QPlainTextEdit, filename: str | None, empty_msg: str):
        if not filename or not self.folder:
            widget.setPlainText(empty_msg)
            return
        try:
            full_path = os.path.join(self.folder, filename)
            with open(full_path, "r", encoding="utf-8") as f:
                raw = f.read()
            try:
                pretty = json.dumps(json.loads(raw), indent=2)
            except Exception:
                pretty = raw
            widget.setPlainText(pretty)
        except Exception as exc:
            widget.setPlainText(f"Could not read {filename}: {exc}")

    def _render_tag_pills(self, session: dict):
        while self.ws_tags_layout.count():
            item = self.ws_tags_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for tag in session.get("tags") or []:
            pill = QFrame()
            pill.setStyleSheet("background: #e0e7ff; border-radius: 10px;")
            h = QHBoxLayout(pill)
            h.setContentsMargins(8, 2, 4, 2)
            h.setSpacing(4)
            lbl = QLabel(tag)
            lbl.setStyleSheet("color:#3730a3; font-size:11px; background: transparent; font-weight: 500;")
            remove_btn = QPushButton("×")
            remove_btn.setFixedSize(16, 16)
            remove_btn.setStyleSheet("border:none; background:transparent; color:#4338ca; font-weight:700;")
            remove_btn.clicked.connect(lambda _=False, t=tag: self._remove_tag(t))
            h.addWidget(lbl)
            h.addWidget(remove_btn)
            self.ws_tags_layout.addWidget(pill)

    def _on_title_changed(self, text: str):
        clean_text = text.strip() or None
        self._queue_save(self.selected_stem, {"title": clean_text})
        session = self._current_session()
        if session and self.selected_stem in self.card_widgets:
            self.card_widgets[self.selected_stem].update_data(session)

    def _add_tag(self):
        value = self.ws_tag_input.text().strip()
        if not value:
            return
        session = self._current_session()
        if not session:
            return
        if value not in (session.get("tags") or []):
            session["tags"] = (session.get("tags") or []) + [value]
            self._queue_save(session["stem"], {"tags": session["tags"]})
            self._render_tag_pills(session)
            self._render_sidebar_tags()
            if self.selected_stem in self.card_widgets:
                self.card_widgets[self.selected_stem].update_data(session)
        self.ws_tag_input.clear()

    def _remove_tag(self, tag: str):
        session = self._current_session()
        if not session:
            return
        session["tags"] = [t for t in (session.get("tags") or []) if t != tag]
        self._queue_save(session["stem"], {"tags": session["tags"]})
        self._render_tag_pills(session)
        self._render_sidebar_tags()
        if self.selected_stem in self.card_widgets:
            self.card_widgets[self.selected_stem].update_data(session)

    def _toggle_favorite(self):
        session = self._current_session()
        if not session:
            return
        session["favorite"] = not session["favorite"]
        self.ws_favorite_btn.setIcon(
            create_vector_icon("star" if session.get("favorite") else "star_outline", "#f59e0b", 16)
        )
        self._queue_save(session["stem"], {"favorite": session["favorite"]})
        if self.selected_stem in self.card_widgets:
            self.card_widgets[self.selected_stem].update_data(session)
            self._apply_filters_and_visibility()

    def _queue_save(self, stem: str | None, patch: dict):
        """Queue edits for a specific session stem, eliminating race conditions (Issue 6.2)."""
        if not stem:
            return
        session = next((s for s in self.sessions if s["stem"] == stem), None)
        if session:
            session.update(patch)

        if stem not in self._pending_saves:
            self._pending_saves[stem] = {}
        self._pending_saves[stem].update(patch)

        self.status_saved.setText("Saving changes...")
        self.status_saved.setStyleSheet("color: #f59e0b; font-weight: 500;")
        self._save_timer.start(400)

    def _flush_pending_saves(self):
        """Immediately commit all pending saves to disk before switching/exiting (Issue 6.2)."""
        if not self._pending_saves or not self.folder:
            self.status_saved.setText("All changes saved ✓")
            self.status_saved.setStyleSheet("color: #10b981; font-weight: 500;")
            return

        self._save_timer.stop()
        to_save = dict(self._pending_saves)
        self._pending_saves.clear()

        try:
            self._last_internal_write_time = time.time()
            for stem, patch in to_save.items():
                update_session_meta(self.folder, stem, patch)
        finally:
            self._last_internal_write_time = time.time()

        self.status_saved.setText("All changes saved ✓")
        self.status_saved.setStyleSheet("color: #10b981; font-weight: 500;")

    def closeEvent(self, event):
        """Save any pending edits before window close (Issue 6.2)."""
        self._flush_pending_saves()
        super().closeEvent(event)

    def _open_pdf(self):
        session = self._current_session()
        if not session:
            return
        pdf_rel = session["files"].get("guidePdf") or session["files"].get("transcriptPdf")
        if pdf_rel:
            path = os.path.join(self.folder, pdf_rel)
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _reveal_in_folder(self):
        session = self._current_session()
        if not session:
            return
        any_file = next((f for f in session["files"].values() if f), None)
        if any_file:
            dir_path = os.path.dirname(os.path.join(self.folder, any_file)) or self.folder
            QDesktopServices.openUrl(QUrl.fromLocalFile(dir_path))

    def _delete_session(self):
        session = self._current_session()
        if not session:
            return
        reply = QMessageBox.question(
            self, "Delete Session",
            f'Delete all Shellby files for "{display_title(session)}"?\n\n'
            "This removes the raw session, blocks, transcript, guide, and PDF from disk, "
            "and cleans up empty parent directories.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        stem = session["stem"]
        self._pending_saves.pop(stem, None)
        delete_session_files(self.folder, stem, session["files"])

        self.sessions = [s for s in self.sessions if s["stem"] != stem]
        self.selected_stem = None
        self._render_sidebar_stats()
        self._render_sidebar_tags()
        self._render_cards()
        self._show_workspace_empty()
        self.status_msg.setText("Session deleted.")

    # ---------------- GUI Controls: Record & Pipelines (Issue 6.1) ----------------

    def _on_record_session_clicked(self):
        """Launch recording dialog and run terminal recorder (Issue 6.1)."""
        if not self.folder:
            self.choose_folder()
            if not self.folder:
                return

        dialog = QDialog(self)
        dialog.setWindowTitle("Record Terminal Session")
        dialog.resize(460, 240)
        v = QVBoxLayout(dialog)

        title_input = QLineEdit()
        title_input.setPlaceholderText("Optional Session Title (e.g. Deploy Web Service)")
        v.addWidget(QLabel("Session Title (optional):"))
        v.addWidget(title_input)

        guide_check = QCheckBox("Automatically generate Guide & PDF upon exit")
        guide_check.setChecked(True)
        v.addWidget(guide_check)

        v.addWidget(QLabel(f"Target Directory:\n{self.folder}"))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        v.addWidget(buttons)

        if dialog.exec_() != QDialog.Accepted:
            return

        title = title_input.text().strip() or None
        target_dir = self.folder

        project_root = Path(__file__).resolve().parent
        shellby_script = str(project_root / "shellby.py")

        cmd = [sys.executable, shellby_script, "record", "--dir", target_dir]
        if title:
            cmd.extend(["--title", title])
        if not guide_check.isChecked():
            cmd.append("--no-guide")

        # 1. Check WSL environment
        is_wsl = os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop") or "WSL_DISTRO_NAME" in os.environ
        launched = False

        if is_wsl:
            distro = os.environ.get("WSL_DISTRO_NAME", "")
            cmd_joined = " ".join(f'"{c}"' if " " in c else c for c in cmd)
            shell_cmd = f"cd '{project_root.as_posix()}' && {cmd_joined}"

            if shutil.which("wt.exe"):
                try:
                    wt_args = ["wt.exe"]
                    if distro:
                        wt_args.extend(["wsl.exe", "-d", distro])
                    else:
                        wt_args.append("wsl.exe")
                    wt_args.extend(["bash", "-c", shell_cmd])
                    subprocess.Popen(wt_args)
                    launched = True
                except Exception:
                    pass

            if not launched and shutil.which("cmd.exe"):
                try:
                    wsl_exec = f"wsl.exe -d {distro} bash -c \"{shell_cmd}\"" if distro else f"wsl.exe bash -c \"{shell_cmd}\""
                    subprocess.Popen(["cmd.exe", "/c", "start", "cmd.exe", "/c", wsl_exec])
                    launched = True
                except Exception:
                    pass

        # 2. Linux desktop terminal emulators
        if not launched:
            terminals = [
                ["x-terminal-emulator", "-e"],
                ["gnome-terminal", "--"],
                ["konsole", "-e"],
                ["xfce4-terminal", "-e"],
                ["xterm", "-e"],
                ["alacritty", "-e"],
                ["kitty", "-e"],
                ["terminator", "-e"],
                ["tilix", "-e"],
            ]
            for term in terminals:
                if shutil.which(term[0]):
                    try:
                        subprocess.Popen(term + cmd, cwd=str(project_root))
                        launched = True
                        break
                    except Exception:
                        pass

        if not launched:
            cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Record in Terminal")
            msg_box.setText("Terminal recording requires a controlling PTY.\n\nRun this command in your terminal:")
            msg_box.setDetailedText(cmd_str)
            copy_btn = msg_box.addButton("Copy Command", QMessageBox.ActionRole)
            msg_box.addButton(QMessageBox.Ok)
            msg_box.exec_()
            if msg_box.clickedButton() == copy_btn:
                QApplication.clipboard().setText(cmd_str)
        else:
            self.status_msg.setText("Terminal recording launched.")

    def _run_heuristics_pipeline(self):
        """Run Phase 2 & 3 pipeline directly from GUI asynchronously (Issue 6.1)."""
        session = self._current_session()
        if not session or not session["files"].get("session"):
            return

        session_path = Path(self.folder) / session["files"]["session"]
        progress = QProgressDialog("Extracting commands and parsing transcript...", "Cancel", 0, 0, self)
        progress.setWindowTitle("Parsing Session")
        progress.setWindowModality(Qt.WindowModal)
        progress.show()

        self._heuristics_worker = HeuristicsWorker(session_path)

        def on_done(success: bool, msg: str):
            progress.close()
            if success:
                self.open_folder(self.folder, keep_selection=True)
                self.status_msg.setText("Heuristics parsing completed.")
                QMessageBox.information(self, "Parsing Complete", msg)
            else:
                QMessageBox.critical(self, "Parsing Error", f"Failed: {msg}")

        self._heuristics_worker.finished.connect(on_done)
        self._heuristics_worker.start()

    def _run_ai_curate_pipeline(self):
        """Trigger AI curation pipeline with model selector and progress tracking (Issue 6.1)."""
        session = self._current_session()
        if not session:
            return

        blocks_rel = session["files"].get("blocks")
        if not blocks_rel:
            QMessageBox.warning(self, "Missing Blocks", "Please run 'Parse & Blocks' before AI curation.")
            return

        blocks_path = Path(self.folder) / blocks_rel
        stem_clean = blocks_path.stem.replace(".blocks", "")
        curated_path = blocks_path.with_name(f"{stem_clean}.guide.md")

        dialog = QDialog(self)
        dialog.setWindowTitle("Curate Technical Guide with Gemini")
        dialog.resize(480, 260)
        v = QVBoxLayout(dialog)

        model_combo = QComboBox()
        model_combo.addItems([
            "gemini-3.5-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ])
        current_env_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        idx = model_combo.findText(current_env_model)
        if idx >= 0:
            model_combo.setCurrentIndex(idx)
        v.addWidget(QLabel("Gemini Model:"))
        v.addWidget(model_combo)

        focus_input = QLineEdit()
        focus_input.setPlaceholderText("Optional objective (e.g. Focus on deployment steps, ignore debug errors)")
        v.addWidget(QLabel("Curation Goal / Focus (optional):"))
        v.addWidget(focus_input)

        api_key_input = QLineEdit()
        api_key_input.setEchoMode(QLineEdit.Password)
        api_key_input.setPlaceholderText("Gemini API Key (saved automatically in settings)")
        remembered_key = self.get_api_key()
        if remembered_key:
            api_key_input.setText(remembered_key)
        v.addWidget(QLabel("Gemini API Key:"))
        v.addWidget(api_key_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        v.addWidget(buttons)

        if dialog.exec_() != QDialog.Accepted:
            return

        raw_key = api_key_input.text().strip()
        if raw_key:
            self.set_api_key(raw_key)
            api_key = raw_key
        else:
            api_key = self.get_api_key()

        if not api_key:
            QMessageBox.warning(
                self,
                "API Key Required",
                "A Gemini API Key is required to run AI guide curation. Please enter your API key."
            )
            return

        focus = focus_input.text().strip() or None
        model = model_combo.currentText().strip()

        progress = QProgressDialog(f"Sending session blocks to {model}...", "Cancel", 0, 0, self)
        progress.setWindowTitle("AI Curation in Progress")
        progress.setWindowModality(Qt.WindowModal)
        progress.show()

        self._curation_worker = CurationWorker(
            blocks_path=blocks_path,
            out_path=curated_path,
            api_key=api_key,
            model=model,
            focus=focus,
        )

        def on_curation_done(success: bool, message: str):
            progress.close()
            if success:
                self.open_folder(self.folder, keep_selection=True)
                self.status_msg.setText("AI curation completed.")
                QMessageBox.information(self, "AI Curation Complete", message)
            else:
                QMessageBox.critical(self, "AI Curation Failed", f"Curation error: {message}")

        self._curation_worker.finished.connect(on_curation_done)
        self._curation_worker.start()

    def _export_pdf_direct(self):
        """Export guide or transcript directly to PDF from GUI (Issue 6.1)."""
        session = self._current_session()
        if not session:
            return

        guide_rel = session["files"].get("guideMd")
        transcript_rel = session["files"].get("transcript")

        if not guide_rel and not transcript_rel:
            QMessageBox.warning(self, "No Markdown", "Neither guide.md nor transcript.md exists for this session.")
            return

        # Determine default source
        source_rel = guide_rel or transcript_rel
        source_path = Path(self.folder) / source_rel
        pdf_path = source_path.with_suffix(".pdf")

        progress = QProgressDialog(f"Compiling {source_path.name} to PDF...", "Cancel", 0, 0, self)
        progress.setWindowTitle("Rendering PDF")
        progress.setWindowModality(Qt.WindowModal)
        progress.show()

        self._pdf_worker = PdfExportWorker(source_path, pdf_path)

        def on_pdf_done(success: bool, msg: str):
            progress.close()
            if success:
                self.open_folder(self.folder, keep_selection=True)
                self.status_msg.setText("PDF rendered.")
                QMessageBox.information(self, "PDF Export", msg)
            else:
                QMessageBox.critical(self, "PDF Export Failed", f"Could not render PDF: {msg}")

        self._pdf_worker.finished.connect(on_pdf_done)
        self._pdf_worker.start()

    def _run_full_pipeline(self):
        """Run Parse -> Curate -> PDF Export in one automated background sequence (Issue 6.1)."""
        session = self._current_session()
        if not session or not session["files"].get("session"):
            return

        session_path = Path(self.folder) / session["files"]["session"]
        api_key = self.get_api_key()
        if not api_key:
            reply = QMessageBox.question(
                self,
                "Gemini API Key Required",
                "A Gemini API Key is required to run AI curation in the Full Pipeline.\n\n"
                "Would you like to configure and save your API Key now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self.configure_api_key()
                api_key = self.get_api_key()
            if not api_key:
                self.status_msg.setText("Full pipeline cancelled: Gemini API key missing.")
                return

        model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

        progress = QProgressDialog("Starting automated pipeline...", "Cancel", 0, 0, self)
        progress.setWindowTitle("Running Full Pipeline")
        progress.setWindowModality(Qt.WindowModal)
        progress.show()

        self._full_pipeline_worker = FullPipelineWorker(
            session_path=session_path,
            api_key=api_key,
            model=model,
            focus=None,
        )
        self._full_pipeline_worker.progress.connect(progress.setLabelText)

        def on_full_done(success: bool, msg: str):
            progress.close()
            if success:
                self.open_folder(self.folder, keep_selection=True)
                self.status_msg.setText("Full pipeline completed.")
                QMessageBox.information(self, "Pipeline Complete", msg)
            else:
                QMessageBox.critical(self, "Pipeline Error", f"Full pipeline failed: {msg}")

        self._full_pipeline_worker.finished.connect(on_full_done)
        self._full_pipeline_worker.start()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Shellby")
    win = ShellbyWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
