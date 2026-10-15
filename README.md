# Shellby 🐚

**Shellby** is an intelligent terminal session recorder, command parser, and technical guide generator. It transforms raw shell sessions into clean, structured Markdown transcripts and AI-curated step-by-step guides, complete with direct PDF export and a desktop GUI.

---

## Architecture Pipeline

```text
Phase 1: Recorder (pty / terminal events)
    ↓
session.json & session.raw.log
    ↓
Phase 2: Parser (ANSI emulation via pyte)
    ↓
Reconstructed terminal data
    ↓
Phase 3: Heuristics & Block Extraction
    ↓
blocks.json
    ├──────────────→ Phase 4: Markdown Transcript Export (transcript.md / transcript.pdf)
    │
    └──────────────→ Phase 5: AI Curator (Gemini Flash / Pro curation)
                           ↓
                      guide.md
                           ↓
                     Phase 6: PDF Export (guide.pdf)
```

---

## Features

- **Terminal Recording**: Non-intrusive `pty`-based session recording preserving timestamps, window dimensions, exit codes, and output.
- **ANSI Terminal Emulation**: Emulates terminal state machines via `pyte` to accurately separate shell prompts, user commands, and command outputs.
- **Rule-Based Heuristic Extraction**: Normalizes multiline commands, strips prompt noise, collapses pagination/editor noise, and extracts structured blocks (`blocks.json`).
- **AI Guide Curation**: Uses Google Gemini to transform terminal history into clear, professional documentation (with objective-focused goals and step-by-step instructions).
- **Direct PDF Export**: Generates styled PDF documentation for both raw session transcripts and AI-curated guides with code card styling.
- **Desktop GUI (`shellby_qt.py`)**: Modern PyQt5 desktop interface for browsing recordings, full automated pipelines, filtering by tags/dates/favorites, and managing API keys.

---

## Installation

### Prerequisites

- Python 3.9+
- Linux or Windows WSL2
- Gemini API Key (from [Google AI Studio](https://aistudio.google.com/))

### Setup

```bash
git clone https://github.com/Agnivesh-Anil-Warrier/Shellby.git
cd Shellby

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Quick Start

### 1. Launch Desktop GUI

```bash
python3 shellby_qt.py
```

### 2. Record a Terminal Session via CLI

```bash
python3 shellby.py record --name my_deployment
```

Type commands in the recorded shell as usual. Type `exit` or press `Ctrl+D` when done.

### 3. Parse and Curate

```bash
# Parse terminal commands into blocks.json and transcript.md
python3 shellby.py parse sessions/YYYY-MM-DD/my_deployment/my_deployment.json

# Curate into a technical guide with Gemini
export GEMINI_API_KEY="your-api-key"
python3 shellby.py curate sessions/YYYY-MM-DD/my_deployment/my_deployment.blocks.json

# Export to PDF
python3 shellby.py pdf sessions/YYYY-MM-DD/my_deployment/my_deployment.guide.md
```

---

## Running Tests

```bash
pytest tests/
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
