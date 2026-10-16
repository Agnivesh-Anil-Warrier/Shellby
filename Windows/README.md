# Shellby for Windows Command Prompt (`Windows`) 🐚🪟

**Shellby (Windows Edition)** is the dedicated Windows-compatible edition of **Shellby**, architected specifically for **Windows Command Prompt (`cmd.exe`)** and **PowerShell** on native Windows systems (Windows 10, Windows 11, and Windows Server).

---

## Why Windows Edition? (Linux vs. Windows Command Prompt)

The original Shellby implementation relies on Unix-only APIs and conventions:
- Unix Pseudo-Terminals (`pty`, `termios`, `fcntl`, `signal.SIGWINCH`).
- Linux `/proc/{pid}/cwd` filesystem inspection.
- POSIX shell conventions (`/bin/bash`, `$`, `#`, `➜`, `❯`).

The Windows edition rectifies these platform constraints by providing native Windows implementations:

| Feature | Linux Core (`Linux/`) | Windows Edition (`Windows/`) |
| :--- | :--- | :--- |
| **Terminal Subsystem** | POSIX PTY (`pty.fork`, `termios`, `fcntl`) | **Windows ConPTY** (`kernel32.dll` PseudoConsole API) + Interactive Console Runner |
| **Default Shell** | `/bin/bash` (`$SHELL`) | **`cmd.exe`** (`%COMSPEC%`) & PowerShell |
| **Prompt Extraction** | Unix regex (`$`, `#`, `%`, `❯`, `➜`) | **Windows Prompt Regex** (`C:\path>`, `(venv) C:\path>`, `PS C:\path>`) |
| **CWD Tracking** | `/proc/{child_pid}/cwd` symlink | **Drive/Path prompt parsing** (`C:\Users\username\dir`) |
| **Line Continuations** | Backslash (`\`) | Windows Caret (`^`) & Unix Backslash (`\`) |
| **Subprompts** | `[sudo]`, `(yes/no)` | `Terminate batch job (Y/N)?`, `Are you sure (Y/N)?`, `[Y/N]` |
| **GUI Recording** | X11/Wayland terminal emulators | **Native `cmd.exe /k`** and **Windows Terminal (`wt.exe`)** |
| **Launcher** | Bash scripts | **`run_shellby.bat`** and native `python.exe` |
| **Binary Build** | N/A | **`Shellby.spec`** (PyInstaller standalone executable) |

---

## Architecture Pipeline

```text
Phase 1: Windows Console Recorder (ConPTY / cmd.exe)
    ↓
session.json & session.events.jsonl
    ↓
Phase 2: Parser (VT100/ANSI screen emulation via pyte with CRLF handling)
    ↓
Reconstructed plain text lines
    ↓
Phase 3: Windows Heuristics (cmd prompt parsing, CWD extraction, blocks.json)
    ↓
blocks.json
    ├──────────────→ Phase 4: Markdown Transcript Export (transcript.md / transcript.pdf)
    │
    └──────────────→ Phase 5: AI Curator (Gemini Flash / Pro curation tailored for Windows)
                           ↓
                      guide.md
                           ↓
                     Phase 6: PDF Export (guide.pdf)
```

---

## Installation (Windows Command Prompt)

### Prerequisites

- Windows 10 (Version 1809+ for ConPTY) or Windows 11
- Python 3.9+ (Installed from [python.org](https://www.python.org/) with *"Add Python to PATH"* checked)
- Google Gemini API Key (from [Google AI Studio](https://aistudio.google.com/))

### Setup in Command Prompt (`cmd.exe`)

```cmd
:: 1. Navigate to the Windows directory
cd Windows

:: 2. Create and activate a Python virtual environment
python -m venv .venv
call .venv\Scripts\activate.bat

:: 3. Install required packages
pip install -r requirements.txt

:: 4. Set your Gemini API Key
set GEMINI_API_KEY=your-gemini-api-key
```

Or from the repository root:
```cmd
pip install -r Windows\requirements.txt
```

---

## Quick Start

### 1. Launch the Desktop GUI

```cmd
:: Launch enhanced GUI via batch script:
run_shellby.bat gui

:: Or launch directly via Python:
python shellby_qt2.py
```

- Click **"Record Session"** to launch a native Command Prompt recording window.
- Browse recordings, extract command blocks, run AI curation, and export styled PDFs.

---

### 2. Record a Session via Command Prompt CLI

```cmd
:: Using batch launcher:
run_shellby.bat record --title my_deployment

:: Or using python directly:
python shellby.py record --title my_deployment
```

- An interactive Command Prompt session begins.
- Type any Windows commands (`dir`, `mkdir`, `copy`, `type`, `python app.py`, `git status`, etc.).
- Type `exit` to stop recording.
- Shellby automatically generates:
  - `sessions\YYYY-MM-DD\my_deployment\my_deployment.json`
  - `sessions\YYYY-MM-DD\my_deployment\my_deployment.events.jsonl`
  - `sessions\YYYY-MM-DD\my_deployment\my_deployment.blocks.json`
  - `sessions\YYYY-MM-DD\my_deployment\my_deployment.transcript.md`
  - `sessions\YYYY-MM-DD\my_deployment\my_deployment.guide.md` (AI-curated step-by-step guide)
  - `sessions\YYYY-MM-DD\my_deployment\my_deployment.guide.pdf` (Production-ready PDF)

---

### 3. Pipeline Subcommands

```cmd
:: Reconstruct plain scrollback text from session
python shellby.py parse sessions\2026-10-16\my_deployment\my_deployment.json

:: Curate existing blocks.json into an AI guide
python shellby.py curate sessions\2026-10-16\my_deployment\my_deployment.blocks.json --out guide.md

:: Export Markdown to PDF
python shellby.py pdf guide.md --out guide.pdf
```

---

### 4. Build Standalone Executable (PyInstaller)

To compile a standalone Windows `.exe` application:

```cmd
pip install pyinstaller
pyinstaller Shellby.spec
```
The compiled output will be generated inside `dist\Shellby\`.

---

## Running Tests

From the repository root or the `Windows/` folder:

```cmd
:: From repository root:
pytest Windows\test_windows.py

:: From Windows/ directory:
pytest test_windows.py
```

---

## File Structure

```text
Windows/
├── __init__.py               # Windows package initialization
├── requirements.txt          # Python requirements for Windows
├── run_shellby.bat           # Windows Command Prompt batch launcher
├── Shellby.spec              # PyInstaller executable build specification
├── shellby.py                # Windows CLI, ConPTY recorder, & pipeline orchestrator
├── shellby_parser.py         # Phase 2: VT100/ANSI screen emulation & CRLF handling
├── shellby_heuristics.py     # Phase 3: Command Prompt regex, CWD extraction, blocks
├── shellby_markdown.py       # Phase 4: Windows Command Prompt Markdown transcript export
├── shellby_curator.py        # Phase 5: Gemini AI curation for Windows environments
├── shellby_pdf.py            # Phase 6: ReportLab PDF generator with code cards
├── shellby_qt.py             # Classic PyQt5 Desktop GUI
├── shellby_qt2.py            # Advanced PyQt5 Desktop GUI (enhanced search, tabs, themes)
├── test_windows.py           # Unit and regression tests for Windows features
├── README.md                 # This documentation
└── logo/                     # Vector and raster logo and splash assets
```

