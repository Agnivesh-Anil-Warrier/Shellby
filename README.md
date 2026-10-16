# Shellby 🐚

**Shellby** is an intelligent terminal session recorder, ANSI command parser, and technical guide generator. It transforms raw interactive shell sessions into clean, structured Markdown transcripts and AI-curated step-by-step guides, complete with publication-grade PDF export and desktop GUI applications.

Shellby provides dedicated, first-class implementations for both **Linux / POSIX** and **native Windows**:
- 🐧 **Linux Edition (`Linux/`)**: Built on Unix Pseudo-Terminals (`pty`), `/proc` filesystem tracking, and POSIX shell conventions (Bash, Zsh).
- 🪟 **Windows Edition (`Windows/`)**: Built on the Windows ConPTY API (`kernel32.dll`), native `cmd.exe` / PowerShell prompt parsers, interactive console runners, batch launchers, and standalone PyInstaller executable packaging.

---

## Architecture Pipeline

```text
Phase 1: Terminal Recorder
    ├── Linux: POSIX PTY (pty.fork / termios / TIOCSWINSZ)
    └── Windows: Windows ConPTY (CreatePseudoConsole) & Interactive Runner
         ↓
    session.json & session.events.jsonl (or raw log)
         ↓
Phase 2: Screen & Stream Parser
    VT100 / ANSI terminal state emulation via pyte (with Windows CRLF handling)
         ↓
    Reconstructed screen text & stream lines
         ↓
Phase 3: Rule-Based Heuristic Extraction
    ├── Prompt stripping ($ / # / ➜ / ❯ or C:\path> / PS C:\path>)
    ├── Multiline command normalization (\ or ^)
    ├── Subprompt detection ([sudo], [Y/N]) & CWD tracking
         ↓
    blocks.json
         ├──────────────→ Phase 4: Markdown Transcript Export (transcript.md / transcript.pdf)
         │
         └──────────────→ Phase 5: AI Guide Curation (Google Gemini Flash / Pro)
                                ↓
                           guide.md (step-by-step technical guide)
                                ↓
                          Phase 6: Styled PDF Export (ReportLab code cards)
```

---

## Repository Structure

```text
Shellby/
├── Linux/                      # Linux / macOS / WSL2 edition
│   ├── shellby.py              # CLI orchestrator & POSIX PTY recorder
│   ├── shellby_parser.py       # VT100 / ANSI screen emulation
│   ├── shellby_heuristics.py   # Heuristic block extraction for Unix shells
│   ├── shellby_markdown.py     # Markdown transcript exporter
│   ├── shellby_curator.py      # AI technical guide curation via Gemini
│   ├── shellby_pdf.py          # ReportLab styled PDF generation
│   ├── shellby_qt.py           # PyQt5 desktop GUI
│   ├── requirements.txt        # Python requirements for Linux
│   ├── tests/                  # Pytest test suite for Linux core
│   └── logo/                   # Visual branding assets
├── Windows/                    # Windows Command Prompt & PowerShell edition
│   ├── shellby.py              # Windows CLI orchestrator & ConPTY recorder
│   ├── shellby_parser.py       # VT100 / ANSI parser with CRLF support
│   ├── shellby_heuristics.py   # Windows prompt regex & CWD block extraction
│   ├── shellby_markdown.py     # Windows Markdown transcript exporter
│   ├── shellby_curator.py      # Windows-tailored Gemini AI curator
│   ├── shellby_pdf.py          # ReportLab styled PDF generation
│   ├── shellby_qt.py           # Classic PyQt5 desktop GUI
│   ├── shellby_qt2.py          # Advanced PyQt5 desktop GUI (tabbed, search, themes)
│   ├── run_shellby.bat         # Batch launcher for Command Prompt
│   ├── Shellby.spec            # PyInstaller specification for building .exe
│   ├── requirements.txt        # Python requirements for Windows
│   ├── test_windows.py         # Pytest test suite for Windows features
│   ├── README.md               # Dedicated Windows guide and architecture notes
│   └── logo/                   # Windows icon & splash assets
├── .gitignore                  # Unified cross-platform Git ignore configuration
└── README.md                   # Main project overview and guide
```

---

## Platform Feature Comparison

| Feature | Linux Edition (`Linux/`) | Windows Edition (`Windows/`) |
| :--- | :--- | :--- |
| **Terminal Subsystem** | POSIX PTY (`pty.fork`, `termios`, `fcntl`) | **Windows ConPTY** (`kernel32.dll`) + Interactive Runner |
| **Supported Shells** | `/bin/bash`, `/bin/zsh`, `/bin/sh` | **`cmd.exe`** (`%COMSPEC%`) & **PowerShell** |
| **Prompt Extraction** | Unix regex (`$`, `#`, `%`, `❯`, `➜`) | **Windows Prompt Regex** (`C:\path>`, `(venv) C:\path>`, `PS C:\path>`) |
| **CWD Tracking** | `/proc/{child_pid}/cwd` symlink | **Drive/Path prompt parsing** (`C:\Users\username\dir`) |
| **Line Continuations** | Unix Backslash (`\`) | Windows Caret (`^`) & Backslash (`\`) |
| **Subprompts** | `[sudo]`, `(yes/no)` | `Terminate batch job (Y/N)?`, `Are you sure (Y/N)?`, `[Y/N]` |
| **GUI Applications** | PyQt5 GUI (`shellby_qt.py`) | PyQt5 GUI (`shellby_qt.py` & enhanced `shellby_qt2.py`) |
| **Convenience Launcher**| Native `python3` | **`run_shellby.bat`** and native `python.exe` |
| **Binary Packaging** | Standalone Python scripts | **`Shellby.spec`** (PyInstaller standalone executable) |

---

## Getting Started: Linux / WSL2

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/Agnivesh-Anil-Warrier/Shellby.git
cd Shellby/Linux

# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure Gemini API Key
export GEMINI_API_KEY="your-gemini-api-key"
```

### 2. Launch GUI or Record Session

```bash
# Launch Desktop GUI:
python3 shellby_qt.py

# Record an interactive shell session via CLI:
python3 shellby.py record --name my_deployment
```

### 3. Pipeline Subcommands

```bash
# Parse session into blocks.json:
python3 shellby.py parse sessions/YYYY-MM-DD/my_deployment/my_deployment.json

# Curate into AI technical guide:
python3 shellby.py curate sessions/YYYY-MM-DD/my_deployment/my_deployment.blocks.json --out guide.md

# Export Markdown to PDF:
python3 shellby.py pdf guide.md --out guide.pdf
```

---

## Getting Started: Windows Command Prompt & PowerShell

For comprehensive Windows instructions, see [Windows/README.md](Windows/README.md).

### 1. Installation

```cmd
:: Navigate to Windows directory
cd Windows

:: Set up virtual environment
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt

:: Configure Gemini API Key
set GEMINI_API_KEY=your-gemini-api-key
```

### 2. Quick Start via Batch Launcher

```cmd
:: Launch enhanced Desktop GUI:
run_shellby.bat gui

:: Record a Command Prompt session:
run_shellby.bat record --title my_deployment
```

### 3. Direct Python CLI Commands

```cmd
:: Launch GUI:
python shellby_qt2.py

:: Record session:
python shellby.py record --title my_deployment

:: Curate blocks into guide:
python shellby.py curate sessions\YYYY-MM-DD\my_deployment\my_deployment.blocks.json --out guide.md

:: Export to styled PDF:
python shellby.py pdf guide.md --out guide.pdf
```

### 4. Build Standalone Windows Executable

```cmd
pip install pyinstaller
pyinstaller Shellby.spec
```
Outputs a standalone application in `Windows\dist\Shellby\`.

---

## Running Tests

Shellby includes comprehensive test suites for both platforms:

### Linux Test Suite
```bash
# From repository root:
pytest Linux/tests/

# Or from within Linux/:
cd Linux && pytest tests/
```

### Windows Test Suite
```cmd
:: From repository root:
pytest Windows/test_windows.py

:: Or from within Windows/:
cd Windows && pytest test_windows.py
```

---

## Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | Google Gemini API key used for Phase 5 AI curation | None (required for curation) |
| `GEMINI_MODEL` | Gemini model name used for curation | `gemini-3.5-flash-lite` |

---

## License

MIT License. See [LICENSE](LICENSE) for details.

