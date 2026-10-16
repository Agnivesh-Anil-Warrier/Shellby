"""Shellby Phase 4 - Markdown Export (Windows Command Prompt Edition).

Converts the structured blocks.json produced by Phase 3 (Heuristics)
into a faithful, human-readable Markdown transcript formatted for
Windows Command Prompt (cmd.exe).

Windows Features:
    - Renders working directory with native Windows prompt style (`C:\\path> command`).
    - Uses `bat` / `cmd` code fence highlighting.
    - Captures and displays non-zero exit status codes.
    - Ensures CommonMark-compliant structure and safe code fences.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _safe_fence(content: str) -> str:
    """Return a Markdown code fence longer than any backtick run in content."""
    longest = max(
        (len(run) for run in re.findall(r"`+", content)),
        default=0,
    )
    return "`" * max(3, longest + 1)


def _render_block(block: dict, is_powershell: bool = False) -> str:
    """Render one command block as Markdown with Windows prompt and exit code metadata."""
    command = str(block.get("command", "")).strip()
    cwd = block.get("cwd")
    exit_code = block.get("exit_code")
    raw_prompt = block.get("prompt")

    # Native prompt formatting
    if raw_prompt:
        prompt_line = f"{raw_prompt} {command}"
    elif is_powershell:
        prompt_line = f"PS {cwd}> {command}" if cwd else f"PS> {command}"
    elif cwd:
        prompt_line = f"{cwd}> {command}"
    else:
        prompt_line = f"> {command}"

    content = prompt_line

    output = block.get("output", [])
    if output:
        content += "\n" + "\n".join(str(line) for line in output)

    if exit_code is not None and exit_code != 0:
        content += f"\n[Process exited with code {exit_code}]"

    fence = _safe_fence(content)
    lang = "powershell" if is_powershell else "cmd"

    return "\n".join(
        [
            f"{fence}{lang}",
            content,
            fence,
        ]
    )


def _load_blocks(blocks_path: Path) -> dict:
    """Load and validate a shellby blocks.json file."""
    if not blocks_path.exists():
        raise FileNotFoundError(f"Blocks file not found: {blocks_path}")

    try:
        payload = json.loads(blocks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {blocks_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("blocks.json must contain a JSON object.")

    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("blocks.json does not contain a valid 'blocks' list.")

    return payload


def render_markdown(blocks_path: Path) -> str:
    """Render blocks.json as a faithful Markdown transcript for Windows."""
    payload = _load_blocks(blocks_path)
    shell_val = str(payload.get("shell", "cmd.exe"))
    is_powershell = "powershell" in shell_val.lower() or "pwsh" in shell_val.lower()

    parts = [
        "# Shellby Session Transcript",
        "",
        f"- **Session ID:** {payload.get('session_id', '')}",
        f"- **Shell:** {shell_val}",
        f"- **Started:** {payload.get('started_at', '')}",
        f"- **Ended:** {payload.get('ended_at', '')}",
        f"- **Duration:** {payload.get('duration', '')}s",
        "",
        "---",
        "",
    ]

    blocks = payload.get("blocks", [])
    for block in blocks:
        parts.append(_render_block(block, is_powershell=is_powershell))
        parts.append("")

    return "\n".join(parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python shellby_markdown.py <path_to_blocks.json>")
    print(render_markdown(Path(sys.argv[1])))
