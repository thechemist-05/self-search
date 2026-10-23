"""A logged "₹" must not raise on a legacy-code-page console (it caused HTTP 500 on Windows)."""

from __future__ import annotations

import io
import sys

import pytest
import structlog

from src.config.logging import make_console_output_safe


def cp1252_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    buffer = io.BytesIO()
    return io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", write_through=True), buffer


# sys.stdout is patched inside each test body: pytest's capture swaps it after fixture setup.

def test_cp1252_console_raises_without_the_fix() -> None:
    stream, _ = cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        print("chat query: sneakers under ₹4,000", file=stream)


def test_fix_escapes_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    stream, buffer = cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    make_console_output_safe()
    print("chat query: sneakers under ₹4,000", file=stream)
    structlog.PrintLogger(file=stream).msg("query under ₹4,000")
    assert buffer.getvalue().count(b"\\u20b9") == 2


def test_tolerates_streams_without_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    make_console_output_safe()  # no AttributeError
