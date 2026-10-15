"""Tests for shellby_curator.py addressing issues 4.1 through 4.8."""

import json
from pathlib import Path
from shellby_curator import (
    redact_secrets,
    strip_outer_markdown_fences,
    _truncate_block_output,
    DEFAULT_MODEL,
    SYSTEM_PROMPT,
)


def test_secret_redaction():
    """Test regex redaction of AWS, GitHub, OpenAI, Google keys, passwords, and tokens (Issue 4.1)."""
    raw_text = (
        "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        "export GITHUB_TOKEN=ghp_123456789012345678901234567890123456\n"
        "export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz1234\n"
        "curl -H 'Authorization: Bearer secret_bearer_token_string_here_12345'\n"
        "git clone https://username:supersecretpassword@github.com/repo.git\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n"
    )

    sanitized = redact_secrets(raw_text)

    assert "AKIAIOSFODNN7EXAMPLE" not in sanitized
    assert "[REDACTED_AWS_KEY]" in sanitized
    assert "ghp_1234567890" not in sanitized
    assert "[REDACTED_GITHUB_TOKEN]" in sanitized
    assert "sk-abcdefgh" not in sanitized
    assert "[REDACTED_OPENAI_KEY]" in sanitized
    assert "secret_bearer_token" not in sanitized
    assert "supersecretpassword" not in sanitized
    assert "-----BEGIN RSA PRIVATE KEY-----" not in sanitized
    assert "[REDACTED_PRIVATE_KEY]" in sanitized


def test_valid_default_model():
    """Test default Gemini model setting."""
    assert DEFAULT_MODEL == "gemini-3.5-flash-lite"


def test_output_truncation_and_raw_output_omission():
    """Test output truncation and raw_output omission for token overflow protection (Issue 4.3)."""
    huge_block = {
        "command": "npm install",
        "raw_output": "\x1b[2K\x1b[1GInstalling...\x1b[0m" * 1000,
        "output": [f"npm info {i}" for i in range(200)],
    }

    truncated = _truncate_block_output(huge_block)
    assert truncated.get("raw_output") is None
    assert len(truncated["output"]) < 100
    assert any("output truncated" in line for line in truncated["output"])


def test_strip_outer_markdown_fences():
    """Test stripping outermost markdown code fences without affecting inner fences (Issue 4.6)."""
    llm_output = (
        "```markdown\n"
        "# Guide Title\n\n"
        "## Step 1\n"
        "```bash\n"
        "echo hello\n"
        "```\n\n"
        "## Result\n"
        "Success.\n"
        "```"
    )

    stripped = strip_outer_markdown_fences(llm_output)
    assert stripped.startswith("# Guide Title")
    assert stripped.endswith("Success.")
    assert "```bash\necho hello\n```" in stripped
