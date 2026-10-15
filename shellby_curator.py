"""Shellby Phase 5 - AI Guide Curator.

Takes the structured blocks.json produced by Phase 3 (Heuristics) and
uses the Gemini API to convert the recorded terminal session into a
clean, minimal, step-by-step technical guide.

Rectifications:
    - Secret & credential redaction (AWS, GitHub, OpenAI, Google, passwords, tokens)
      before transmitting session data to the LLM (Issue 4.1).
    - Default model name: 'gemini-3.5-flash-lite' (Issue 4.2).
    - Unbounded context mitigation: output truncation and raw_output omission (Issue 4.3).
    - Network request timeout protection (Issue 4.4).
    - Secure API key transport via 'x-goog-api-key' header instead of URL parameter (Issue 4.5).
    - Stripping of outer markdown wrapper fences from generated output (Issue 4.6).
    - Dedicated 'system_instruction' payload parameter preventing prompt injection (Issue 4.7).
    - Informative error logging instead of broad exception silencing (Issue 4.8).

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

# ---------------------------------------------------------------------------
# Optional .env support
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
DEFAULT_TIMEOUT_SECONDS = 60

# ---------------------------------------------------------------------------
# Secret & Credential Redaction (Issue 4.1)
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
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"), "Bearer [REDACTED_TOKEN]"),
    # SSH / RSA / PGP Private Key blocks
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    # Password & token assignments in shell commands or exports
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api_key)\s*([:=])\s*([^\s'\";]+)"), r"\1\2[REDACTED]"),
    # Basic auth embedded credentials in URLs
    (re.compile(r"(https?://[^:\s]+):([^@\s]+)@"), r"\1:[REDACTED]@"),
]


def redact_secrets(text: str) -> str:
    """Sanitize secrets, passwords, and API keys from text before external transmission."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def strip_outer_markdown_fences(text: str) -> str:
    """Strip outermost markdown code fence wrappers (Issue 4.6)."""
    text = text.strip()
    lines = text.splitlines()
    if len(lines) >= 2:
        first = lines[0].strip()
        last = lines[-1].strip()
        if (first.startswith("```markdown") or first == "```") and last == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


# ---------------------------------------------------------------------------
# AI System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Shellby's AI Guide Curator.

You receive a structured JSON representation of a recorded terminal
session. The JSON contains session metadata and command blocks reconstructed
by Shellby's deterministic parsing and heuristic stages.

Your job is NOT to reproduce the raw transcript.

Your job is to transform the recorded work into a clean, minimal,
step-by-step technical guide that explains the successful procedure.

IMPORTANT PRINCIPLES:

1. IDENTIFY THE CORE GOAL
   Determine what the user was actually trying to accomplish in the terminal session.
   The final guide should focus on that goal rather than reproducing everything typed.

2. REMOVE NOISE
   Omit commands not necessary for accomplishing the core goal:
   - exploratory checks (whoami, uname, pwd, date, hostname)
   - accidental typos or failed attempts that led nowhere
   - redundant directory listings (ls, ls -la)
   - aborted commands or navigation detours

3. RETAIN ESSENTIAL STEPS
   Preserve commands that:
   - install dependencies or tools
   - configure files, services, or environments
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
   ```bash
   <command>
   ```
   <Optional expected result if useful.>

   ## Step 2: <Step Title> [CATEGORY]
   ...

   ## Result
   <Brief description of the final successful result.>

Do not include commentary or analysis outside the Markdown guide.
"""


# ---------------------------------------------------------------------------
# Gemini API Interaction
# ---------------------------------------------------------------------------

def _call_gemini_api(
    user_content: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Send user prompt with system instruction to Gemini with fallback."""

    key = api_key or os.environ.get("GEMINI_API_KEY")

    if not key:
        raise ValueError(
            "Gemini API key is required. "
            "Set GEMINI_API_KEY environment variable or pass --api-key."
        )

    # -----------------------------------------------------------------------
    # Attempt 1: Official Google GenAI SDK (with dedicated system_instruction)
    # -----------------------------------------------------------------------
    sdk_error = None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
        )

        response = client.models.generate_content(
            model=model,
            contents=user_content,
            config=config,
        )

        if response and response.text:
            return strip_outer_markdown_fences(response.text.strip())

    except Exception as exc:
        sdk_error = exc
        # Informative logging (Issue 4.8)
        print(f"[Shellby Curator] GenAI SDK attempt failed: {exc}. Trying REST API fallback...", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Attempt 2: Direct REST API (with x-goog-api-key header and timeout)
    # -----------------------------------------------------------------------
    import urllib.error
    import urllib.request

    # Secure header instead of query param (Issue 4.5)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    # Dedicated system_instruction preventing injection (Issue 4.7)
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
        # Enforce timeout (Issue 4.4)
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

        # Strip outer markdown wrapper code fences (Issue 4.6)
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


# ---------------------------------------------------------------------------
# Input handling & Truncation (Issue 4.3)
# ---------------------------------------------------------------------------

MAX_OUTPUT_LINES = 60
HEAD_LINES = 35
TAIL_LINES = 20


def _truncate_block_output(block: dict) -> dict:
    """Cap oversized output streams and omit raw_output escape sequences (Issue 4.3)."""
    cleaned = dict(block)
    # Strip raw_output to avoid sending megabytes of escape codes
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
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {input_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("blocks.json must contain a JSON object.")

    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("blocks.json does not contain a valid 'blocks' list.")

    sanitized_blocks = [_truncate_block_output(b) for b in blocks]
    payload["blocks"] = sanitized_blocks

    compact_json = json.dumps(payload, indent=2, ensure_ascii=False)
    # Redact sensitive credentials (Issue 4.1)
    return redact_secrets(compact_json)


def build_user_prompt(
    blocks_json: str,
    focus: str | None = None,
) -> str:
    """Construct the user portion of the Gemini prompt."""
    parts = [
        "SHELLBY SESSION DATA:",
        "",
        blocks_json,
    ]

    if focus:
        parts.extend(
            [
                "",
                "USER-SPECIFIED FOCUS / GOAL:",
                "",
                focus,
            ]
        )

    parts.extend(
        [
            "",
            "Now produce the clean, step-by-step technical Markdown guide.",
        ]
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Curation entry points
# ---------------------------------------------------------------------------

def curate_guide(
    input_path: Path,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    focus: str | None = None,
) -> str:
    """Generate a curated Markdown guide from blocks.json."""
    blocks_json = _load_input(input_path)
    user_prompt = build_user_prompt(blocks_json=blocks_json, focus=focus)

    return _call_gemini_api(
        user_content=user_prompt,
        api_key=api_key,
        model=model,
    )


def curate_file(
    input_path: Path,
    out_path: Path | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    focus: str | None = None,
) -> str:
    """Curate blocks.json and optionally write the guide to disk."""
    curated = curate_guide(
        input_path=input_path,
        api_key=api_key,
        model=model,
        focus=focus,
    )

    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(curated, encoding="utf-8")

    return curated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shellby AI Curator - convert blocks.json into a clean technical guide using Gemini."
    )
    parser.add_argument("input", type=Path, help="Path to Shellby blocks.json")
    parser.add_argument("--out", "-o", type=Path, help="Path to save the curated Markdown guide")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    parser.add_argument("--focus", "-f", help="Optional objective or focus to guide the curation")
    parser.add_argument("--api-key", "-k", help="Gemini API key (or set GEMINI_API_KEY environment variable)")

    args = parser.parse_args()

    try:
        curated = curate_file(
            input_path=args.input,
            out_path=args.out,
            api_key=args.api_key,
            model=args.model,
            focus=args.focus,
        )

        if args.out:
            print(f"Curated guide written to: {args.out}")
        else:
            print(curated)

    except Exception as exc:
        sys.exit(f"AI curation failed: {exc}")


if __name__ == "__main__":
    main()