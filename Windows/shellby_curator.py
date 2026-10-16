"""Shellby Phase 5 - AI Guide Curator (Windows Command Prompt Edition).

Takes the structured blocks.json produced by Phase 3 (Heuristics) and
uses the Gemini API to convert the recorded Windows Command Prompt session
into a clean, minimal, step-by-step technical guide.

Windows Features:
    - AI prompt tuned for Windows Command Prompt commands and syntax.
    - Filters out Windows exploratory noise (`ver`, `dir`, `cls`, `cd`, `whoami`).
    - Produces clean step-by-step guides with `cmd` / `bat` code blocks.
    - Secret & credential redaction (AWS, GitHub, OpenAI, Google, passwords, tokens).
    - Network request timeout protection and secure header authentication.
    - Outer Markdown wrapper fence stripping.

Input:
    blocks.json

Output:
    curated Markdown guide
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Optional .env support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
DEFAULT_TIMEOUT_SECONDS = 60

# ---------------------------------------------------------------------------
# Secret & Credential Redaction
# ---------------------------------------------------------------------------

SECRET_PATTERNS = [
    # AWS Access Key
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # GitHub Tokens
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    # OpenAI API Key
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED_OPENAI_KEY]"),
    # Google API Key
    (re.compile(r"\bAIza[0-9A-Za-z-_]{35}\b"), "[REDACTED_GOOGLE_KEY]"),
    # Generic Bearer Tokens
    (re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.=:_+/]{20,}"), "Bearer [REDACTED_TOKEN]"),
    # Generic Password assignment in commands (set PASS=..., password=...)
    (re.compile(r"(?i)(?:password|passwd|pwd|secret|token)\s*[:=]\s*['\"]?([^\s'\"]{6,})['\"]?"), r"\g<0> -> [REDACTED_SECRET]"),
]


def redact_secrets(text: str) -> str:
    """Scrub known API keys, tokens, and passwords from input text."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def strip_outer_markdown_fences(text: str) -> str:
    """Strip redundant outer markdown code fence wrappers (```markdown ... ```)."""
    text = text.strip()
    lines = text.splitlines()
    if len(lines) >= 2:
        first = lines[0].strip()
        last = lines[-1].strip()
        if (first.startswith("```markdown") or first == "```") and last == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


# ---------------------------------------------------------------------------
# AI System Prompt for Windows Command Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Shellby's AI Guide Curator for Windows Command Prompt.

You receive a structured JSON representation of a recorded Windows terminal
session (cmd.exe or PowerShell). The JSON contains session metadata and command
blocks reconstructed by Shellby's deterministic parsing and heuristic stages.

Your job is NOT to reproduce the raw transcript.

Your job is to transform the recorded work into a clean, minimal,
step-by-step technical guide that explains the successful procedure on Windows.

IMPORTANT PRINCIPLES:

1. IDENTIFY THE CORE GOAL
   Determine what the user was actually trying to accomplish in the Command Prompt.
   The final guide should focus on that goal rather than reproducing everything typed.

2. REMOVE NOISE
   Omit commands not necessary for accomplishing the core goal:
   - exploratory checks (whoami, ver, cd, dir, cls, hostname, systeminfo)
   - accidental typos or failed attempts that led nowhere
   - redundant directory listings (dir, dir /s, tree)
   - aborted commands or navigation detours

3. RETAIN ESSENTIAL STEPS
   Preserve commands that:
   - install dependencies or tools (winget, choco, pip, npm)
   - configure files, services, or Windows environments (set, copy, mkdir)
   - build, compile, or package artifacts
   - launch or restart services
   - verify that the procedure succeeded

4. OUTPUT FORMAT
   Return ONLY Markdown conforming to this structure:

   # <Clear Guide Title>

   ## Overview
   <Brief explanation of what the guide accomplishes.>

   ## Prerequisites
   <Prerequisites supported by the recorded session, if any.>

   ## Step 1: <Step Title> [CATEGORY]
   <Concise explanation of what the step does and why it is needed.>
   ```cmd
   <command>
   ```

   ## Step 2: <Step Title> [CATEGORY]
   ...

   ## Verification
   <How to verify the work was successful.>

5. CONVENTIONS
   - Valid categories in step titles: [SETUP], [CONFIGURATION], [DEPENDENCIES], [BUILD], [DEPLOYMENT], [VERIFICATION], [MAINTENANCE].
   - Use `cmd` or `bat` for code block language identifiers.
   - Windows path conventions: use backslashes `\\` for Windows paths.
   - Do NOT wrap the entire response in outer ```markdown code fences.
"""


def _get_api_key(explicit_key: str | None = None) -> str | None:
    if explicit_key:
        return explicit_key
    return os.environ.get("GEMINI_API_KEY")


def call_gemini(
    user_content: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Call Gemini to curate the session data into a technical guide."""
    key = _get_api_key(api_key)
    if not key:
        raise ValueError(
            "Gemini API key is required. Set GEMINI_API_KEY environment variable "
            "or pass --api-key / specify in .env."
        )

    # 1. Try official google-genai SDK if installed
    sdk_error = None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
            ),
        )
        if response and response.text:
            return strip_outer_markdown_fences(response.text)
    except Exception as exc:
        sdk_error = exc

    # 2. Fallback: Direct REST API with timeout and x-goog-api-key header
    import urllib.error
    import urllib.request

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {
                "parts": [{"text": user_content}]
            }
        ],
        "generationConfig": {
            "temperature": 0.2
        }
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            response_bytes = response.read()

        response_json = json.loads(response_bytes.decode("utf-8"))
        candidates = response_json.get("candidates", [])

        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts).strip()

        if not text:
            raise RuntimeError("Gemini returned an empty response.")

        return strip_outer_markdown_fences(text)

    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        detail = f"Gemini API request failed ({exc.code}): {error_body}"
        if sdk_error:
            detail += f" (Initial SDK error: {sdk_error})"
        raise RuntimeError(detail) from exc

    except urllib.error.URLError as exc:
        detail = f"Could not connect to Gemini API: {exc}"
        if sdk_error:
            detail += f" (Initial SDK error: {sdk_error})"
        raise RuntimeError(detail) from exc


MAX_OUTPUT_LINES = 60
HEAD_LINES = 35
TAIL_LINES = 20


def _truncate_block_output(block: dict) -> dict:
    """Cap oversized output streams and omit raw_output escape sequences."""
    cleaned = dict(block)
    cleaned.pop("raw_output", None)

    output = cleaned.get("output", [])
    if isinstance(output, list) and len(output) > MAX_OUTPUT_LINES:
        omitted = len(output) - HEAD_LINES - TAIL_LINES
        truncated = (
            output[:HEAD_LINES]
            + [f"... [output truncated: {omitted} lines omitted] ..."]
            + output[-TAIL_LINES:]
        )
        cleaned["output"] = truncated

    return cleaned


def _load_input(input_path: Path) -> str:
    """Load, sanitize, and truncate blocks artifact for the AI prompt."""
    data = json.loads(input_path.read_text(encoding="utf-8"))

    if isinstance(data, dict) and "blocks" in data and isinstance(data["blocks"], list):
        data["blocks"] = [_truncate_block_output(b) for b in data["blocks"]]

    raw_json = json.dumps(data, indent=2)
    return redact_secrets(raw_json)


def curate_file(
    input_path: Path,
    out_path: Path | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    focus: str | None = None,
) -> str:
    """Read blocks.json, call Gemini with Windows-adapted prompt, and return/save the guide."""
    sanitized_input = _load_input(input_path)

    prompt = (
        "Here is the recorded Windows Command Prompt session (blocks.json):\n\n"
        f"```json\n{sanitized_input}\n```\n\n"
    )

    if focus:
        prompt += f"User focus/hint: {focus}\n\n"

    prompt += "Generate the curated technical guide following your principles."

    curated = call_gemini(user_content=prompt, api_key=api_key, model=model)

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(curated, encoding="utf-8")

    return curated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shellby AI Guide Curator (Windows Edition)")
    parser.add_argument("input", type=Path, help="Path to blocks.json")
    parser.add_argument("-o", "--out", type=Path, help="Output markdown guide path")
    parser.add_argument("-k", "--api-key", help="Gemini API key")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help="Gemini model")
    parser.add_argument("-f", "--focus", help="Session focus hint")
    args = parser.parse_args()

    result = curate_file(
        input_path=args.input,
        out_path=args.out,
        api_key=args.api_key,
        model=args.model,
        focus=args.focus,
    )
    if not args.out:
        print(result)
