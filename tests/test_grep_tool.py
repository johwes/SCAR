import tempfile
from pathlib import Path

from scar.grep_tool import extract_directives, execute


def test_extract_directives():
    text = "I need more context.\nGREP: #define MAX_BUF\nGREP: `parse_packet`"
    directives = extract_directives(text)
    assert directives == ["#define MAX_BUF", "parse_packet"]


def test_execute_finds_content():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "config.h").write_text("#define MAX_BUF 1024\n")
        result = execute(["MAX_BUF"], tmpdir)
    assert "MAX_BUF" in result
    assert "1024" in result


def test_execute_empty_patterns_returns_empty():
    result = execute([], "/any/path")
    assert result == ""
