"""Tests for shellby_pdf.py addressing issues 5.4, 5.5, 5.6, 5.7, and 5.8."""

from pathlib import Path
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import Paragraph, Table
from shellby_pdf import (
    render_markdown_to_pdf,
    parse_markdown_to_flowables,
    _build_styles,
    _clean_inline_markdown,
    _wrap_code_text,
    NumberedCanvas,
)


def test_clean_inline_markdown():
    """Test inline markdown links, bold, italic, and code formatting (Issue 5.4)."""
    text = "Check [Google](https://google.com) and **bold** and *italic* and `code`"
    cleaned = _clean_inline_markdown(text)
    assert '<a href="https://google.com"' in cleaned
    assert "<b>bold</b>" in cleaned
    assert "<i>italic</i>" in cleaned
    assert '<font name="Courier"' in cleaned


def test_multiline_paragraphs_and_lists(tmp_path: Path):
    """Test multi-line paragraph joining, ordered lists, and nested lists (Issue 5.4)."""
    md = (
        "# Main Title\n\n"
        "This is the first line of a paragraph\n"
        "and this is the second line of the same paragraph\n"
        "which should be joined into a continuous flowable.\n\n"
        "1. First step\n"
        "2. Second step\n\n"
        "- Top bullet\n"
        "  - Nested bullet\n"
    )

    styles = _build_styles()
    flowables = parse_markdown_to_flowables(md, styles, content_width=540)

    # Paragraph flowables should include joined continuous text
    paras = [f for f in flowables if isinstance(f, Paragraph)]
    assert any("first line of a paragraph and this is the second line" in p.text for p in paras)
    assert any("<b>1.</b> First step" in p.text for p in paras)
    assert any("• Top bullet" in p.text for p in paras)
    assert any("◦ Nested bullet" in p.text for p in paras)


def test_markdown_tables_and_blockquotes():
    """Test markdown table and multi-line blockquote flowable generation (Issue 5.4)."""
    md = (
        "| Parameter | Type | Description |\n"
        "| --- | --- | --- |\n"
        "| api_key | string | Gemini API key |\n"
        "| model | string | Model name |\n\n"
        "> Note: This is a multi-line\n"
        "> blockquote that should be rendered\n"
        "> in a single callout box.\n"
    )

    styles = _build_styles()
    flowables = parse_markdown_to_flowables(md, styles, content_width=540)

    tables = [f for f in flowables if isinstance(f, Table)]
    assert len(tables) >= 2  # One table for markdown table, one for callout


def test_code_wrapping_and_large_block_chunking():
    """Test code line wrapping and large block chunking (Issue 5.5)."""
    long_line = "x" * 150
    wrapped = _wrap_code_text([long_line], max_len=80)
    assert "\n" in wrapped
    assert len(wrapped.splitlines()[0]) <= 80

    # 100 lines of code
    huge_code = "```bash\n" + "\n".join(f"echo {i}" for i in range(100)) + "\n```"
    styles = _build_styles()
    flowables = parse_markdown_to_flowables(huge_code, styles, content_width=540)
    tables = [f for f in flowables if isinstance(f, Table)]
    # Should chunk into multiple tables to avoid ReportLab Table page overflow
    assert len(tables) >= 2


def test_render_pdf_letter_and_a4(tmp_path: Path):
    """Test generating valid PDFs with NumberedCanvas in Letter and A4 (Issues 5.6 & 5.7)."""
    md = (
        "# Technical Guide\n\n"
        "## Step 1 [SETUP]\n\n"
        "Install required dependencies.\n\n"
        "```bash\n"
        "pip install -r requirements.txt\n"
        "```\n\n"
        "| Name | Version |\n"
        "| --- | --- |\n"
        "| shellby | 1.0 |\n\n"
        "> Finished setup successfully.\n"
    )

    letter_pdf = tmp_path / "guide_letter.pdf"
    render_markdown_to_pdf(md, letter_pdf, page_size=letter)
    assert letter_pdf.exists()
    assert letter_pdf.stat().st_size > 1000

    a4_pdf = tmp_path / "guide_a4.pdf"
    render_markdown_to_pdf(md, a4_pdf, page_size=A4)
    assert a4_pdf.exists()
    assert a4_pdf.stat().st_size > 1000
