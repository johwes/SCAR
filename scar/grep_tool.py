"""Agentic grep tool.

Parses GREP: directives emitted by the LLM and executes them against the
repository, returning results to enrich the next prompt round.
Prefers ripgrep, falls back to standard grep.
"""

import re
import subprocess
from pathlib import Path

_GREP_PATTERN = re.compile(r"GREP:\s*(.+)")
_MAX_RESULT_LINES = 100


def extract_directives(text: str) -> list[str]:
    """Return grep patterns emitted by the LLM in the form 'GREP: <pattern>'."""
    return [m.group(1).strip().strip("`\"'") for m in _GREP_PATTERN.finditer(text)]


def execute(patterns: list[str], repo_dir: str | Path) -> str:
    """Run each pattern against repo_dir and return aggregated results."""
    if not patterns:
        return ""

    repo = str(repo_dir)
    results: list[str] = []

    for pattern in patterns:
        output = _run_rg(pattern, repo) or _run_grep(pattern, repo)
        if output:
            lines = output.splitlines()[:_MAX_RESULT_LINES]
            results.append(f"GREP: {pattern}\n" + "\n".join(lines))

    return "\n\n".join(results)


def _run_rg(pattern: str, repo: str) -> str:
    try:
        proc = subprocess.run(
            ["rg", "-i", "-n", "--no-heading", pattern, repo],
            capture_output=True, text=True, timeout=10,
        )
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _run_grep(pattern: str, repo: str) -> str:
    try:
        proc = subprocess.run(
            ["grep", "-r", "-i", "-n", pattern, repo],
            capture_output=True, text=True, timeout=10,
        )
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
