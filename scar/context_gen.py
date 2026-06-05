"""Stage 1: Security context generation (adapted from nano-analyzer).

Generates a structured security briefing for a source file before the patch
generation stage. The briefing identifies entry points, buffer sizes, tainted
data flows, and likely bug classes — reducing LLM hallucination in Stage 2.
"""

from pathlib import Path

from . import llm, grep_tool

CONTEXT_GEN_PROMPT = """\
You are a security researcher preparing a briefing for a colleague who will \
audit this C source file for memory safety vulnerabilities.

Your briefing must cover:
1. Where this code sits in the architecture (network parser, file handler, etc.)
2. All untrusted input entry points and which variables carry attacker data
3. Fixed-size buffers and their exact sizes — use GREP: <symbol> to resolve #defines
4. Data flow from untrusted sources to dangerous sinks (memcpy, strcpy, etc.)
5. Pointers that may be NULL after fallible calls (malloc, lookup, parse)
6. Public API functions vs internal static helpers (trust boundary)
7. Most likely bug classes given the code structure

Do NOT identify specific vulnerabilities yet — only provide context.
If you need to resolve a macro or find a caller, emit: GREP: <pattern>
"""


def generate(source_path: str | Path, repo_dir: str | Path) -> str:
    """Return a security briefing for source_path, enriched with grep results."""
    source = Path(source_path).read_text(encoding="utf-8", errors="replace")

    messages = [
        {"role": "system", "content": CONTEXT_GEN_PROMPT},
        {"role": "user", "content": f"File: {source_path}\n\n{source}"},
    ]

    briefing = llm.chat(messages, temperature=0.1)

    directives = grep_tool.extract_directives(briefing)
    if directives:
        grep_results = grep_tool.execute(directives, repo_dir)
        if grep_results:
            briefing += f"\n\n--- Grep Results ---\n{grep_results}"

    return briefing
