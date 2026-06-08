"""Stage 1: Security context generation (adapted from nano-analyzer).

Generates a structured security briefing for a source file before the patch
generation stage. The briefing identifies entry points, buffer sizes, tainted
data flows, and likely bug classes — reducing LLM hallucination in Stage 2.
"""

from pathlib import Path

from . import llm, grep_tool, ikos_witness


def _extract_function_context(source: str, target_line: int, *, max_lines: int = 300) -> str:
    """Return the source lines for the C function enclosing target_line (1-indexed).

    Uses a brace-counting heuristic — works well for C but may misidentify
    boundaries in files with heavy preprocessor macros. Falls back to a
    ±100-line window if the enclosing function cannot be determined.
    Hard-caps at max_lines to protect the context window.
    """
    lines = source.splitlines(keepends=True)
    n = len(lines)
    target_idx = min(max(target_line - 1, 0), n - 1)

    # Scan backwards: each } crossed means we entered a nested scope going up;
    # each { means we exited one. When the running total goes negative, the {
    # on this line is the enclosing function's opening brace.
    depth = 0
    func_open = None
    for i in range(target_idx, -1, -1):
        depth += lines[i].count('}') - lines[i].count('{')
        if depth < 0:
            func_open = i
            break

    if func_open is None:
        start = max(0, target_idx - 100)
        end = min(n, target_idx + 101)
        return "".join(lines[start:end])

    # Walk back past the function signature (return type, multi-line params).
    sig_start = func_open
    while sig_start > 0:
        prev = lines[sig_start - 1].strip()
        if not prev or prev.endswith('}') or prev.endswith(';'):
            break
        sig_start -= 1

    # Scan forward to find the matching closing brace.
    depth = 0
    func_close = func_open
    for i in range(func_open, n):
        depth += lines[i].count('{') - lines[i].count('}')
        if depth <= 0 and i > func_open:
            func_close = i
            break

    start, end = sig_start, func_close + 1
    total = end - start

    if total <= max_lines:
        return "".join(lines[start:end])

    # Function exceeds cap — centre the window on the target line.
    half = max_lines // 2
    win_start = max(start, target_idx - half)
    win_end = min(end, win_start + max_lines)
    win_start = max(start, win_end - max_lines)
    header = f"[note: function is {total} lines; showing {max_lines} around line {target_line}]\n"
    return header + "".join(lines[win_start:win_end])


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


def generate(
    source_path: str | Path,
    repo_dir: str | Path,
    witness_db: Path | None = None,
    finding_line: int | None = None,
) -> str:
    """Return a security briefing for source_path, enriched with grep results
    and (when available) the IKOS counterexample witness trace."""
    source = Path(source_path).read_text(encoding="utf-8", errors="replace")

    if finding_line is not None:
        context_source = _extract_function_context(source, finding_line)
        source_header = f"File: {source_path} (function context around line {finding_line})\n\n"
    else:
        context_source = source
        source_header = f"File: {source_path}\n\n"

    messages = [
        {"role": "system", "content": CONTEXT_GEN_PROMPT},
        {"role": "user", "content": source_header + context_source},
    ]

    briefing = llm.chat(messages, model=llm.patch_model(), temperature=0.1)

    directives = grep_tool.extract_directives(briefing)
    if directives:
        grep_results = grep_tool.execute(directives, repo_dir)
        if grep_results:
            briefing += f"\n\n--- Grep Results ---\n{grep_results}"

    if witness_db is not None and finding_line is not None:
        trace = ikos_witness.extract(witness_db, str(source_path), finding_line)
        if trace:
            briefing += f"\n\n--- IKOS Witness Trace ---\n{trace}"

    return briefing
