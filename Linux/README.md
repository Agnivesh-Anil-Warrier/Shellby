# Shellby for Linux & macOS / WSL2 (`Linux/`) 🐚🐧

**Shellby (Linux Edition)** is the core POSIX implementation of **Shellby**, architected for Linux distributions, macOS, and Windows Subsystem for Linux (WSL2).

---

## Technical Highlights

- **POSIX Pseudo-Terminal Recorder**: Employs Python `pty.fork`, `termios`, and `fcntl` to capture raw interactive terminal sessions transparently without altering shell behavior or terminal modes.
- **Window Resize Synchronization**: Handles `signal.SIGWINCH` with `ioctl(TIOCSWINSZ)` to track dynamic terminal resizing.
- **Current Working Directory Tracking**: Inspects `/proc/{child_pid}/cwd` symlinks for accurate, per-command directory history on Linux systems.
- **ANSI Terminal Emulation**: Emulates VT100 / ANSI terminal state machines via `pyte.Screen` to cleanly decouple prompts, user commands, and multi-line outputs.
- **POSIX Heuristics**: Strips standard POSIX prompts (`$`, `#`, `%`, `❯`, `➜`), recognizes backslash (`\`) line continuations, and isolates subprompts (e.g. `[sudo]`, `(yes/no)`).
- **Google Gemini AI Curation**: Integrates with Google AI Studio (`google-genai`) to curate terminal streams into structured technical markdown documentation.
- **ReportLab PDF Export**: Renders publication-grade PDF documents with syntax-highlighted code cards.
- **PyQt5 Desktop GUI**: Graphical manager with recording triggers, automated pipeline processing, search filters, and PDF previews.

---

## Installation

### Prerequisites

- Python 3.9+
- Linux (Ubuntu, Debian, Fedora, Arch, etc.) or Windows WSL2
- Gemini API Key (from [Google AI Studio](https://aistudio.google.com/))

### Setup

```bash
# 1. Navigate to the Linux edition directory
cd Linux

# 2. Create and activate a Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your Gemini API Key
export GEMINI_API_KEY="your-gemini-api-key"
```

---

## Usage

### 1. Launch the Desktop GUI (`shellby_qt.py`)

```bash
python3 shellby_qt.py
```

Features:
- Live session browser organized by date.
- One-click recording launch inside an embedded or external terminal.
- Automated pipeline execution (Parse -> Curate -> PDF).
- Search, favorite, and tag filter capabilities.

---

### 2. Record a Session via CLI

```bash
python3 shellby.py record --name my_deployment
```

- Type commands as usual in your native shell (`ls`, `git`, `docker`, `python`, etc.).
- When finished, type `exit` or press `Ctrl+D`.
- Automatically outputs:
  - `sessions/YYYY-MM-DD/my_deployment/my_deployment.json` (Structured event log)
  - `sessions/YYYY-MM-DD/my_deployment/my_deployment.raw.log` (Raw PTY stream)
  - `sessions/YYYY-MM-DD/my_deployment/my_deployment.blocks.json` (Parsed command blocks)
  - `sessions/YYYY-MM-DD/my_deployment/my_deployment.transcript.md` (Clean Markdown transcript)
  - `sessions/YYYY-MM-DD/my_deployment/my_deployment.guide.md` (AI-curated guide)
  - `sessions/YYYY-MM-DD/my_deployment/my_deployment.guide.pdf` (Styled PDF document)

---

### 3. Pipeline Subcommands

```bash
# Parse session into blocks.json and transcript.md
python3 shellby.py parse sessions/YYYY-MM-DD/my_deployment/my_deployment.json

# Curate existing blocks.json into a technical guide
python3 shellby.py curate sessions/YYYY-MM-DD/my_deployment/my_deployment.blocks.json --out guide.md

# Convert Markdown guide or transcript to PDF
python3 shellby.py pdf guide.md --out guide.pdf
```

---

## Running Tests

From the `Linux/` directory:

```bash
pytest tests/
```

Or from the repository root:

```bash
pytest Linux/tests/
```

---

## Directory Structure

```text
Linux/
├── shellby.py              # Main CLI entry point & POSIX PTY session recorder
├── shellby_parser.py       # VT100 / ANSI terminal screen emulator (pyte)
├── shellby_heuristics.py   # Heuristic command block extractor for Unix shells
├── shellby_markdown.py     # Markdown transcript exporter
├── shellby_curator.py      # AI technical guide curator (Google Gemini API)
├── shellby_pdf.py          # ReportLab PDF documentation generator
├── shellby_qt.py           # PyQt5 desktop GUI application
├── requirements.txt        # Python package dependencies
├── tests/                  # Unit and integration test suite
│   ├── test_shellby.py
│   ├── test_parser.py
│   ├── test_heuristics.py
│   ├── test_markdown.py
│   ├── test_curator.py
│   ├── test_pdf.py
│   └── test_qt.py
└── logo/                   # Visual branding, icons, and splash screens
```
