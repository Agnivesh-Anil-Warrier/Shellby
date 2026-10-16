#!/usr/bin/env python3
"""Shellby - Desktop GUI for Windows Command Prompt (winShell Edition).

Desktop GUI for browsing, recording, and curating Windows terminal sessions.

Windows Command Prompt Features:
    - Native Command Prompt (`cmd.exe`) and Windows Terminal (`wt.exe`) launcher
      for instant recording directly from the GUI.
    - Windows Explorer file and directory integration (`os.startfile`).
    - Robust Windows backslash (`\\`) and forward-slash (`/`) path normalization.
    - Windows Command Prompt transcript styling and step categorizations.
    - Asynchronous worker threads for parsing, heuristics, AI curation, and PDF export.
    - Responsive dark and light themes with crisp vector icons.
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
    QPointF, QRectF, QByteArray
)
from PyQt5.QtGui import (
    QDesktopServices, QFont, QKeySequence, QPixmap, QPainter, QPainterPath,
    QColor, QPen, QBrush, QPolygon, QPolygonF, QIcon, QLinearGradient
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QTextEdit, QTextBrowser,
    QPlainTextEdit, QTabWidget, QFileDialog, QMessageBox, QScrollArea,
    QFrame, QSplitter, QLayout, QDialog, QDialogButtonBox, QCheckBox,
    QProgressDialog, QStatusBar, QShortcut, QSplashScreen
)

try:
    from PyQt5.QtSvg import QSvgRenderer
    HAS_QT_SVG = True
except ImportError:
    HAS_QT_SVG = False

try:
    import markdown
    HAS_MARKDOWN = True
except ImportError:
    HAS_MARKDOWN = False

APP_ORG = "Shellby"
APP_NAME = "ShellbyWin"
LIBRARY_FILE = ".shellby-library.json"

SUFFIXES = [
    ("blocks", ".blocks.json"),
    ("transcript", ".transcript.md"),
    ("transcriptPdf", ".transcript.pdf"),
    ("guideMd", ".guide.md"),
    ("guidePdf", ".guide.pdf"),
]

if getattr(sys, "frozen", False):
    _pkg_root = Path(sys.executable).resolve().parent
else:
    _pkg_root = Path(__file__).resolve().parent

# ----------------------------------------------------------------------
# Path & Filesystem helpers
# ----------------------------------------------------------------------

def norm_rel_path(path_str: str) -> str:
    """Normalize relative path to use consistent forward slashes."""
    return str(path_str).replace("\\", "/")


def base_stem(stem: str) -> str:
    """Extract final component of a session stem safely."""
    return Path(stem).name


def dir_of(rel_path: str) -> str:
    """Extract directory portion of a relative path safely."""
    p = Path(rel_path).parent.as_posix()
    return "" if p == "." else p


def is_bare_guide_file(filename: str) -> bool:
    """Check if filename matches bare guide naming safely."""
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
    try:
        with open(lib_path, "w", encoding="utf-8") as f:
            json.dump(library, f, indent=2)
    except Exception:
        pass


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
    """Scan folder and group sessions, normalizing paths consistently."""
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
        if not matched_key and filename.endswith(".json"):
            matched_key = "session"

        if matched_key:
            group["files"][matched_key] = filename

    for filename in bare_guide_files:
        d = dir_of(filename)
        key = "guidePdf" if filename.endswith(".pdf") else "guideMd"
        matches = [g for s, g in groups.items() if dir_of(g["files"].get("session", s)) == d]

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
        columns = pick((session_meta, "columns"), (blocks_meta, "columns"))
        shell = pick((session_meta, "shell"), (blocks_meta, "shell")) or "cmd.exe"

        primary_file = session_file or blocks_file or next(iter(gfiles.values()), None)
        mtime_ms = 0
        if primary_file:
            try:
                mtime_ms = int(os.path.getmtime(os.path.join(folder, primary_file)) * 1000)
            except OSError:
                pass

        meta = lib_sessions.get(stem, {})
        title = meta.get("title") or pick((session_meta, "title"), (blocks_meta, "title"))
        tags = meta.get("tags") or []
        notes = meta.get("notes") or ""
        starred = bool(meta.get("starred", False))

        sessions.append({
            "stem": stem,
            "title": title,
            "startedAt": started_at,
            "endedAt": ended_at,
            "duration": duration,
            "columns": columns,
            "shell": shell,
            "commands": commands,
            "structuredBlocks": structured_blocks,
            "files": gfiles,
            "tags": tags,
            "notes": notes,
            "starred": starred,
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
    """Delete session files and clean up empty enclosing session and date folders."""
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

    dirs_to_check = set()
    for dp in del_paths:
        parent_dir = os.path.dirname(dp)
        if parent_dir and parent_dir != folder and os.path.abspath(parent_dir) != os.path.abspath(folder):
            dirs_to_check.add(parent_dir)

    for d in sorted(dirs_to_check, key=lambda x: len(x), reverse=True):
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                grandparent = os.path.dirname(d)
                if grandparent and grandparent != folder and os.path.isdir(grandparent) and not os.listdir(grandparent):
                    os.rmdir(grandparent)
        except OSError:
            pass

    library = read_library(folder)
    if stem in library.get("sessions", {}):
        del library["sessions"][stem]
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
    """Accurately format dates without corrupting minute components."""
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        hour_12 = dt.hour % 12 or 12
        return f"{dt.strftime('%b')} {dt.day}, {hour_12}:{dt.minute:02d} {dt.strftime('%p')}"
    except Exception:
        return None


def display_title(session: dict) -> str:
    return session.get("title") or base_stem(session["stem"])


# ----------------------------------------------------------------------
# Vector Icon Generator
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
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 1.5, Qt.SolidLine))
        painter.drawEllipse(QRectF(2.5, 2.5, 11, 11))
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QRectF(5.5, 5.5, 5, 5))

    elif name == "sparkle":
        path = QPainterPath()
        path.moveTo(8.0, 2.0)
        path.quadTo(8.0, 8.0, 14.0, 8.0)
        path.quadTo(8.0, 8.0, 8.0, 14.0)
        path.quadTo(8.0, 8.0, 2.0, 8.0)
        path.quadTo(8.0, 8.0, 8.0, 2.0)
        painter.fillPath(path, QBrush(color))

    elif name == "search":
        painter.drawEllipse(QRectF(3.0, 3.0, 7.5, 7.5))
        painter.drawLine(QPointF(8.5, 8.5), QPointF(13.0, 13.0))

    elif name == "folder":
        path = QPainterPath()
        path.moveTo(2.0, 4.0)
        path.lineTo(6.0, 4.0)
        path.lineTo(7.5, 5.5)
        path.lineTo(14.0, 5.5)
        path.lineTo(14.0, 12.5)
        path.lineTo(2.0, 12.5)
        path.closeSubpath()
        painter.drawPath(path)

    elif name == "terminal":
        path = QPainterPath()
        path.moveTo(4.0, 5.0)
        path.lineTo(7.5, 8.0)
        path.lineTo(4.0, 11.0)
        painter.drawPath(path)
        painter.drawLine(QPointF(9.0, 11.0), QPointF(12.5, 11.0))

    elif name == "star":
        painter.setBrush(QBrush(color))
        path = QPainterPath()
        pts = [
            QPointF(8.0, 2.0), QPointF(9.8, 5.7), QPointF(14.0, 6.3),
            QPointF(11.0, 9.2), QPointF(11.7, 13.5), QPointF(8.0, 11.5),
            QPointF(4.3, 13.5), QPointF(5.0, 9.2), QPointF(2.0, 6.3),
            QPointF(6.2, 5.7)
        ]
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        path.closeSubpath()
        painter.drawPath(path)

    elif name == "refresh":
        rect = QRectF(3.0, 3.0, 10.0, 10.0)
        painter.drawArc(rect, 45 * 16, 270 * 16)
        arrow = QPainterPath()
        arrow.moveTo(10.0, 1.5)
        arrow.lineTo(13.5, 3.5)
        arrow.lineTo(10.5, 6.0)
        painter.drawPath(arrow)

    else:
        painter.drawRect(QRectF(3.0, 3.0, 10.0, 10.0))

    painter.end()
    return QIcon(pixmap)


# ----------------------------------------------------------------------
# Search & Filtering
# ----------------------------------------------------------------------

def matches_search(session: dict, q: str) -> bool:
    if not q:
        return True
    s = q.lower().strip()
    if s in (session.get("title") or "").lower():
        return True
    if s in session["stem"].lower():
        return True
    if s in (session.get("notes") or "").lower():
        return True
    for tag in session.get("tags") or []:
        if s in tag.lower():
            return True
    for cmd in session.get("commands") or []:
        if s in cmd.lower():
            return True
    return False


def within_when(session: dict, when: str) -> bool:
    if when == "all":
        return True
    now = time.time() * 1000
    t = parse_iso(session.get("startedAt")) or session.get("mtimeMs") or 0
    if when == "today":
        return (now - t) <= 24 * 3600 * 1000
    elif when == "week":
        return (now - t) <= 7 * 24 * 3600 * 1000
    elif when == "month":
        return (now - t) <= 30 * 24 * 3600 * 1000
    return True


def sort_sessions(sessions: list[dict], sort_key: str) -> list[dict]:
    out = list(sessions)
    if sort_key == "date-desc":
        out.sort(key=lambda s: parse_iso(s["startedAt"]) or s["mtimeMs"] or 0, reverse=True)
    elif sort_key == "date-asc":
        out.sort(key=lambda s: parse_iso(s["startedAt"]) or s["mtimeMs"] or 0)
    elif sort_key == "duration-desc":
        out.sort(key=lambda s: s.get("duration") or 0, reverse=True)
    elif sort_key == "duration-asc":
        out.sort(key=lambda s: s.get("duration") or 0)
    elif sort_key == "commands-desc":
        out.sort(key=lambda s: len(s.get("commands") or []), reverse=True)
    elif sort_key == "title-asc":
        out.sort(key=lambda s: display_title(s).lower())
    return out


# ----------------------------------------------------------------------
# Background Worker Threads
# ----------------------------------------------------------------------

class HeuristicsWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, session_path: Path):
        super().__init__()
        self.session_path = session_path

    def run(self):
        try:
            try:
                from .shellby_heuristics import session_blocks_payload
                from .shellby_markdown import render_markdown
            except ImportError:
                from shellby_heuristics import session_blocks_payload
                from shellby_markdown import render_markdown

            stem = self.session_path.stem
            blocks_path = self.session_path.with_name(f"{stem}.blocks.json")
            transcript_path = self.session_path.with_name(f"{stem}.transcript.md")

            payload = session_blocks_payload(self.session_path)
            blocks_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

            transcript = render_markdown(blocks_path)
            transcript_path.write_text(transcript, encoding="utf-8")

            self.finished.emit(True, f"Extracted {len(payload.get('blocks', []))} command blocks.")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class CuratorWorker(QThread):
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
            try:
                from .shellby_curator import curate_file
            except ImportError:
                from shellby_curator import curate_file

            curate_file(
                input_path=self.blocks_path,
                out_path=self.out_path,
                api_key=self.api_key,
                model=self.model,
                focus=self.focus,
            )
            self.finished.emit(True, "Curated guide successfully generated.")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class PdfWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, md_path: Path, pdf_path: Path):
        super().__init__()
        self.md_path = md_path
        self.pdf_path = pdf_path

    def run(self):
        try:
            try:
                from .shellby_pdf import render_markdown_to_pdf
            except ImportError:
                from shellby_pdf import render_markdown_to_pdf

            md_content = self.md_path.read_text(encoding="utf-8")
            render_markdown_to_pdf(md_content, self.pdf_path)
            self.finished.emit(True, f"PDF exported: {self.pdf_path.name}")
        except Exception as exc:
            self.finished.emit(False, str(exc))


# ----------------------------------------------------------------------
# Main Application Window
# ----------------------------------------------------------------------

class ShellbyWinMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shellby - Windows Command Prompt Session Analyser")
        self.resize(1180, 780)

        self.folder: str | None = None
        self.sessions: list[dict] = []
        self.active_stem: str | None = None
        self.dark_mode = True
        self.settings = QSettings("Shellby", "ShellbyWin")

        # Sync saved API key and model with process environment
        saved_key = self.settings.value("shellby/api_key", "")
        if saved_key and str(saved_key).strip():
            os.environ["GEMINI_API_KEY"] = str(saved_key).strip()
        saved_model = self.settings.value("shellby/model", "")
        if saved_model and str(saved_model).strip():
            os.environ["GEMINI_MODEL"] = str(saved_model).strip()

        self._init_ui()
        self._apply_theme()

    def get_api_key(self) -> str | None:
        saved = self.settings.value("shellby/api_key", "")
        if saved and str(saved).strip():
            return str(saved).strip()
        return os.environ.get("GEMINI_API_KEY", "").strip() or None

    def set_api_key(self, key: str | None) -> None:
        clean = (key or "").strip()
        if clean:
            self.settings.setValue("shellby/api_key", clean)
            os.environ["GEMINI_API_KEY"] = clean
        else:
            self.settings.remove("shellby/api_key")
            if "GEMINI_API_KEY" in os.environ:
                del os.environ["GEMINI_API_KEY"]

    def get_model(self) -> str:
        saved = self.settings.value("shellby/model", "")
        if saved and str(saved).strip():
            return str(saved).strip()
        return os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"

    def set_model(self, model: str | None) -> None:
        clean = (model or "").strip()
        if clean:
            self.settings.setValue("shellby/model", clean)
            os.environ["GEMINI_MODEL"] = clean
        else:
            self.settings.remove("shellby/model")
            if "GEMINI_MODEL" in os.environ:
                del os.environ["GEMINI_MODEL"]

        # Auto-open sessions directory if exists
        curr = Path(__file__).resolve().parent
        for candidate in (curr / "sessions", curr.parent / "sessions"):
            if candidate.is_dir():
                self.open_folder(str(candidate))
                break

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header toolbar
        header = QFrame()
        header.setFixedHeight(54)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(16, 8, 16, 8)

        logo_lbl = QLabel("🐚 Shellby (Windows)")
        logo_lbl.setFont(QFont("Segoe UI", 13, QFont.Bold))
        h_layout.addWidget(logo_lbl)

        self.folder_btn = QPushButton(" Open Folder")
        self.folder_btn.setIcon(create_vector_icon("folder"))
        self.folder_btn.clicked.connect(self._choose_folder)
        h_layout.addWidget(self.folder_btn)

        self.record_btn = QPushButton(" Record Session")
        self.record_btn.setIcon(create_vector_icon("record", "#ef4444"))
        self.record_btn.clicked.connect(self._launch_record_dialog)
        h_layout.addWidget(self.record_btn)

        h_layout.addStretch()

        self.theme_btn = QPushButton("Toggle Theme")
        self.theme_btn.clicked.connect(self._toggle_theme)
        h_layout.addWidget(self.theme_btn)

        main_layout.addWidget(header)

        # Splitter: Sidebar + Workspace
        splitter = QSplitter(Qt.Horizontal)

        # Sidebar
        sidebar = QWidget()
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(12, 12, 12, 12)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search sessions, commands...")
        self.search_box.textChanged.connect(self._filter_sessions)
        sb_layout.addWidget(self.search_box)

        self.session_list = QScrollArea()
        self.session_list.setWidgetResizable(True)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setAlignment(Qt.AlignTop)
        self.session_list.setWidget(self.list_container)
        sb_layout.addWidget(self.session_list)

        sidebar.setMinimumWidth(280)
        splitter.addWidget(sidebar)

        # Workspace
        self.workspace = QWidget()
        ws_layout = QVBoxLayout(self.workspace)
        ws_layout.setContentsMargins(16, 16, 16, 16)

        # Actions row
        act_row = QHBoxLayout()
        self.ws_title = QLabel("No Session Selected")
        self.ws_title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        act_row.addWidget(self.ws_title)
        act_row.addStretch()

        self.parse_btn = QPushButton("Extract Blocks")
        self.parse_btn.setIcon(create_vector_icon("terminal"))
        self.parse_btn.clicked.connect(self._run_heuristics_pipeline)
        act_row.addWidget(self.parse_btn)

        self.curate_btn = QPushButton("AI Curate")
        self.curate_btn.setIcon(create_vector_icon("sparkle", "#6366f1"))
        self.curate_btn.clicked.connect(self._run_ai_curate_pipeline)
        act_row.addWidget(self.curate_btn)

        self.pdf_btn = QPushButton("Export PDF")
        self.pdf_btn.clicked.connect(self._export_pdf_dialog)
        act_row.addWidget(self.pdf_btn)

        ws_layout.addLayout(act_row)

        # Tabs: Transcript, Guide, Blocks, Session JSON
        self.tabs = QTabWidget()
        self.transcript_view = QTextBrowser()
        self.transcript_view.setOpenExternalLinks(True)
        self.tabs.addTab(self.transcript_view, "Transcript")

        self.guide_view = QTextBrowser()
        self.guide_view.setOpenExternalLinks(True)
        self.tabs.addTab(self.guide_view, "Curated Guide")

        self.blocks_view = QPlainTextEdit()
        self.blocks_view.setReadOnly(True)
        self.tabs.addTab(self.blocks_view, "Blocks JSON")

        self.raw_view = QPlainTextEdit()
        self.raw_view.setReadOnly(True)
        self.tabs.addTab(self.raw_view, "Raw Session JSON")

        ws_layout.addWidget(self.tabs)
        splitter.addWidget(self.workspace)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        main_layout.addWidget(splitter)

        # Status Bar
        self.statusBar().showMessage("Ready")

    def _apply_theme(self):
        if self.dark_mode:
            self.setStyleSheet("""
                QMainWindow, QWidget { background-color: #0f172a; color: #f8fafc; font-family: 'Segoe UI', sans-serif; }
                QFrame { background-color: #1e293b; border-bottom: 1px solid #334155; }
                QPushButton { background-color: #334155; color: #f8fafc; border: 1px solid #475569; border-radius: 6px; padding: 6px 12px; }
                QPushButton:hover { background-color: #475569; }
                QLineEdit { background-color: #1e293b; color: #f8fafc; border: 1px solid #334155; border-radius: 6px; padding: 6px; }
                QTabWidget::pane { border: 1px solid #334155; background-color: #1e293b; border-radius: 6px; }
                QTabBar::tab { background-color: #0f172a; color: #94a3b8; padding: 8px 16px; border: 1px solid #334155; border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
                QTabBar::tab:selected { background-color: #1e293b; color: #f8fafc; font-weight: bold; }
                QTextBrowser, QPlainTextEdit { background-color: #0b1120; color: #f8fafc; border: none; font-family: Consolas, monospace; font-size: 13px; }
                QScrollArea { border: none; background-color: transparent; }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow, QWidget { background-color: #f8fafc; color: #0f172a; font-family: 'Segoe UI', sans-serif; }
                QFrame { background-color: #ffffff; border-bottom: 1px solid #e2e8f0; }
                QPushButton { background-color: #f1f5f9; color: #0f172a; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 12px; }
                QPushButton:hover { background-color: #e2e8f0; }
                QLineEdit { background-color: #ffffff; color: #0f172a; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px; }
                QTabWidget::pane { border: 1px solid #e2e8f0; background-color: #ffffff; border-radius: 6px; }
                QTabBar::tab { background-color: #f8fafc; color: #64748b; padding: 8px 16px; border: 1px solid #e2e8f0; border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
                QTabBar::tab:selected { background-color: #ffffff; color: #0f172a; font-weight: bold; }
                QTextBrowser, QPlainTextEdit { background-color: #ffffff; color: #0f172a; border: none; font-family: Consolas, monospace; font-size: 13px; }
                QScrollArea { border: none; background-color: transparent; }
            """)

    def _toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self._apply_theme()
        if self.active_stem:
            self._display_session(self.active_stem)

    def _choose_folder(self):
        f = QFileDialog.getExistingDirectory(self, "Select Sessions Directory", self.folder or os.getcwd())
        if f:
            self.open_folder(f)

    def open_folder(self, folder: str):
        self.folder = folder
        self.sessions = scan_folder(folder)
        self.statusBar().showMessage(f"Loaded {len(self.sessions)} sessions from {folder}")
        self._render_session_cards()
        if self.sessions:
            self._select_session(self.sessions[0]["stem"])

    def _render_session_cards(self):
        # Clear existing cards
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        q = self.search_box.text()
        matched = [s for s in self.sessions if matches_search(s, q)]

        for s in matched:
            card = QPushButton()
            card.setFixedHeight(64)
            card.setCursor(Qt.PointingHandCursor)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 6, 8, 6)
            card_layout.setSpacing(2)

            title_lbl = QLabel(display_title(s))
            title_lbl.setFont(QFont("Segoe UI", 10, QFont.Bold))
            card_layout.addWidget(title_lbl)

            meta_text = fmt_date(s.get("startedAt")) or "Recorded session"
            if s.get("duration"):
                meta_text += f" • {fmt_duration(s['duration'])}"
            meta_lbl = QLabel(meta_text)
            meta_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
            card_layout.addWidget(meta_lbl)

            stem = s["stem"]
            card.clicked.connect(lambda checked, st=stem: self._select_session(st))
            self.list_layout.addWidget(card)

    def _filter_sessions(self):
        self._render_session_cards()

    def _select_session(self, stem: str):
        self.active_stem = stem
        self._display_session(stem)

    def _display_session(self, stem: str):
        session = next((s for s in self.sessions if s["stem"] == stem), None)
        if not session or not self.folder:
            return

        self.ws_title.setText(display_title(session))
        files = session.get("files", {})

        # 1. Transcript
        tr_file = files.get("transcript")
        if tr_file and os.path.exists(os.path.join(self.folder, tr_file)):
            md = Path(os.path.join(self.folder, tr_file)).read_text(encoding="utf-8")
            self.transcript_view.setPlainText(md)
        else:
            self.transcript_view.setPlainText("No transcript.md generated yet. Click 'Extract Blocks'.")

        # 2. Curated Guide
        guide_file = files.get("guideMd")
        if guide_file and os.path.exists(os.path.join(self.folder, guide_file)):
            md = Path(os.path.join(self.folder, guide_file)).read_text(encoding="utf-8")
            self.guide_view.setPlainText(md)
        else:
            self.guide_view.setPlainText("No curated guide.md generated yet. Click 'AI Curate'.")

        # 3. Blocks JSON
        blocks_file = files.get("blocks")
        if blocks_file and os.path.exists(os.path.join(self.folder, blocks_file)):
            b_text = Path(os.path.join(self.folder, blocks_file)).read_text(encoding="utf-8")
            self.blocks_view.setPlainText(b_text)
        else:
            self.blocks_view.setPlainText("{}")

        # 4. Raw Session JSON
        sess_file = files.get("session")
        if sess_file and os.path.exists(os.path.join(self.folder, sess_file)):
            s_text = Path(os.path.join(self.folder, sess_file)).read_text(encoding="utf-8")
            self.raw_view.setPlainText(s_text)
        else:
            self.raw_view.setPlainText("{}")

    def _launch_record_dialog(self):
        """Launch Windows Command Prompt session recording."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Record Windows Session")
        dialog.setFixedWidth(400)
        d_layout = QVBoxLayout(dialog)

        d_layout.addWidget(QLabel("Session Title (optional):"))
        title_in = QLineEdit()
        d_layout.addWidget(title_in)

        guide_chk = QCheckBox("Generate AI Guide and PDF automatically")
        guide_chk.setChecked(True)
        d_layout.addWidget(guide_chk)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        d_layout.addWidget(btns)

        if dialog.exec_() == QDialog.Accepted:
            title = title_in.text().strip()
            target_dir = self.folder or str(Path("sessions").resolve())

            cli_script = str(Path(__file__).resolve().parent / "shellby.py")
            cmd = [sys.executable, cli_script, "record", "--dir", target_dir]
            if title:
                cmd.extend(["--title", title])
            if not guide_chk.isChecked():
                cmd.append("--no-guide")

            # Native Windows terminal launching:
            launched = False
            if sys.platform == "win32":
                # Check Windows Terminal (wt.exe)
                if shutil.which("wt.exe"):
                    try:
                        subprocess.Popen(["wt.exe", "cmd.exe", "/k"] + cmd)
                        launched = True
                    except Exception:
                        pass
                # Native cmd.exe fallback
                if not launched:
                    try:
                        subprocess.Popen(["cmd.exe", "/c", "start", "cmd.exe", "/k"] + cmd)
                        launched = True
                    except Exception:
                        pass
            else:
                # Linux desktop terminal fallbacks if running under X11/Wayland
                for term in [["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["xterm", "-e"]]:
                    if shutil.which(term[0]):
                        try:
                            subprocess.Popen(term + cmd)
                            launched = True
                            break
                        except Exception:
                            pass

            if launched:
                self.statusBar().showMessage("Command Prompt recording launched.")
            else:
                cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
                QMessageBox.information(
                    self,
                    "Run in Command Prompt",
                    f"Run this command in Windows Command Prompt:\n\n{cmd_str}"
                )

    def _run_heuristics_pipeline(self):
        session = next((s for s in self.sessions if s["stem"] == self.active_stem), None)
        if not session or not session.get("files", {}).get("session"):
            return

        session_path = Path(self.folder) / session["files"]["session"]
        progress = QProgressDialog("Extracting Windows command blocks...", "Cancel", 0, 0, self)
        progress.show()

        self.worker = HeuristicsWorker(session_path)

        def on_done(success: bool, msg: str):
            progress.close()
            if success:
                self.open_folder(self.folder)
                QMessageBox.information(self, "Extraction Complete", msg)
            else:
                QMessageBox.critical(self, "Extraction Failed", msg)

        self.worker.finished.connect(on_done)
        self.worker.start()

    def _run_ai_curate_pipeline(self):
        session = next((s for s in self.sessions if s["stem"] == self.active_stem), None)
        if not session:
            return

        blocks_file = session.get("files", {}).get("blocks")
        if not blocks_file:
            QMessageBox.warning(self, "Missing Blocks", "Please run 'Extract Blocks' first.")
            return

        blocks_path = Path(self.folder) / blocks_file
        guide_path = blocks_path.with_name(f"{Path(blocks_file).stem.split('.')[0]}.guide.md")

        # Curation dialog with editable Model text box and API Key
        dialog = QDialog(self)
        dialog.setWindowTitle("Curate Technical Guide with Gemini")
        dialog.resize(480, 240)
        v = QVBoxLayout(dialog)

        model_lbl = QLabel("Gemini Model:")
        model_combo = QComboBox()
        model_combo.setEditable(True)
        model_combo.addItems([
            "gemini-2.5-flash",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
            "gemini-2.0-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-pro",
        ])
        current_model = self.get_model()
        idx = model_combo.findText(current_model)
        if idx >= 0:
            model_combo.setCurrentIndex(idx)
        else:
            model_combo.setEditText(current_model)
        model_combo.setToolTip("Type or select any Gemini model name. It will be remembered automatically.")
        v.addWidget(model_lbl)
        v.addWidget(model_combo)

        api_key_lbl = QLabel("Gemini API Key:")
        api_input = QLineEdit()
        api_input.setEchoMode(QLineEdit.Password)
        api_input.setPlaceholderText("Paste Gemini API Key (or leave blank if set in environment)")
        cur_key = self.get_api_key()
        if cur_key:
            api_input.setText(cur_key)
        v.addWidget(api_key_lbl)
        v.addWidget(api_input)

        bbox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bbox.accepted.connect(dialog.accept)
        bbox.rejected.connect(dialog.reject)
        v.addWidget(bbox)

        if dialog.exec_() != QDialog.Accepted:
            return

        typed_key = api_input.text().strip()
        if typed_key:
            self.set_api_key(typed_key)
            api_key = typed_key
        else:
            api_key = self.get_api_key()

        chosen_model = model_combo.currentText().strip() or self.get_model()
        self.set_model(chosen_model)

        progress = QProgressDialog(f"Calling Gemini ({chosen_model}) to curate Windows guide...", "Cancel", 0, 0, self)
        progress.show()

        self.c_worker = CuratorWorker(
            blocks_path=blocks_path,
            out_path=guide_path,
            api_key=api_key,
            model=chosen_model,
            focus=None,
        )

        def on_done(success: bool, msg: str):
            progress.close()
            if success:
                self.open_folder(self.folder)
                QMessageBox.information(self, "Curation Complete", msg)
            else:
                QMessageBox.critical(self, "Curation Failed", msg)

        self.c_worker.finished.connect(on_done)
        self.c_worker.start()

    def _export_pdf_dialog(self):
        session = next((s for s in self.sessions if s["stem"] == self.active_stem), None)
        if not session:
            return

        guide_file = session.get("files", {}).get("guideMd") or session.get("files", {}).get("transcript")
        if not guide_file:
            QMessageBox.warning(self, "No Document", "No guide or transcript available to export.")
            return

        in_p = Path(self.folder) / guide_file
        out_p = in_p.with_suffix(".pdf")

        progress = QProgressDialog("Generating PDF...", "Cancel", 0, 0, self)
        progress.show()

        self.pdf_worker = PdfWorker(in_p, out_p)

        def on_done(success: bool, msg: str):
            progress.close()
            if success:
                self.open_folder(self.folder)
                QMessageBox.information(self, "PDF Export", msg)
            else:
                QMessageBox.critical(self, "PDF Failed", msg)

        self.pdf_worker.finished.connect(on_done)
        self.pdf_worker.start()


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(_pkg_root / "logo" / "logo.ico")))
    window = ShellbyWinMainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
