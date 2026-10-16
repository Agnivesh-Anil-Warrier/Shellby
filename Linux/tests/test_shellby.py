"""Tests for shellby.py addressing issues 1.1, 1.3, 1.4, 1.6, and 5.3."""

import os
from pathlib import Path
from typer.testing import CliRunner
from shellby import app, _write_all, _find_venv, _get_default_sessions_dir

runner = CliRunner()


def test_find_venv(tmp_path: Path, monkeypatch):
    """Test virtual environment discovery across standard conventions (Issue 1.6)."""
    # 1. Test VIRTUAL_ENV env var
    fake_venv = tmp_path / "custom_venv"
    fake_bin = fake_venv / "bin"
    fake_bin.mkdir(parents=True)
    fake_python = fake_bin / "python"
    fake_python.touch(mode=0o755)

    monkeypatch.setenv("VIRTUAL_ENV", str(fake_venv))
    discovered = _find_venv()
    assert discovered is not None
    assert str(fake_python) == str(discovered)

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def test_default_sessions_dir(monkeypatch, tmp_path: Path):
    """Test SHELLBY_SESSIONS_DIR configuration (Issue 1.3)."""
    custom_dir = tmp_path / "my_sessions"
    monkeypatch.setenv("SHELLBY_SESSIONS_DIR", str(custom_dir))

    d = _get_default_sessions_dir()
    assert d == custom_dir


def test_write_all(tmp_path: Path):
    """Test _write_all writes complete byte stream (Issue 1.4)."""
    test_file = tmp_path / "output.bin"
    data = b"hello shellby " * 500

    fd = os.open(str(test_file), os.O_WRONLY | os.O_CREAT)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)

    assert test_file.read_bytes() == data


def test_cli_help():
    """Test CLI help output and available flags."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Shellby - Terminal Session Analyser" in result.output
    assert "record" in result.output
    assert "guide" in result.output
    assert "curate" in result.output
