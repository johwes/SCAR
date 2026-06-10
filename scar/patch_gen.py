"""Stage 2: LLM patch synthesis.

Given an IKOS finding and a security briefing from context_gen, produces a
unified diff patch that fixes the vulnerability while respecting embedded
safety constraints (no malloc, no banned string functions).
"""

import difflib
import json
from pathlib import Path

from . import llm
from .sarif_bridge import Finding

PATCH_SYSTEM_PROMPT = """\
You are an expert C security engineer. You will be given:
- A security briefing describing the file's architecture and data flows
- A specific vulnerability found by a static analyzer
- The full source file content
- An occurrence count telling you exactly how many times the vulnerable \
  pattern appears in the file

Produce a minimal unified diff (--- a/file / +++ b/file format) that fixes \
the vulnerability. Use only 1 line of context (not the default 3) in each \
hunk — the smallest context window that unambiguously locates the change. \
Your patch must:
- Generate exactly as many hunks as the occurrence count states — no more, \
  no fewer. Do not invent occurrences that are not present in the source.
- Fix only the reported vulnerability pattern — no unrelated refactoring
- Never add new calls to malloc, free, realloc, calloc, or alloca that did \
  not exist in the original code — preserving an existing allocation call \
  while adding a bounds check around it is fine
- Never use strcpy, strcat, sprintf, vsprintf, or gets
- Preserve all existing function signatures and struct layouts
- Use bounded alternatives: strncpy, snprintf, memcpy with explicit length checks
- When fixing multiple occurrences in the same function, do not re-declare \
  the same local variable in each hunk — declare it once before the affected \
  block, or use distinct variable names per hunk to avoid C redeclaration errors
- Do NOT change behaviour outside the vulnerable code path — if a design \
  choice looks unusual but is intentional (e.g. a deliberate delimiter, a \
  deliberate cleanup strategy), leave it alone

Output ONLY the unified diff, no explanation.
"""


STRUCTURED_SYSTEM_PROMPT = """\
You are an expert C security engineer. You will be given:
- A security briefing describing the file's architecture and data flows
- A specific vulnerability found by a static analyzer
- The full source file content
- An occurrence count telling you exactly how many times the vulnerable \
  pattern appears in the file

Respond with a JSON object containing two fields:

"reasoning": Think step-by-step before writing any code. Identify exactly \
which lines are vulnerable, why, and how you will fix each one. Reason about \
variable scope when fixing multiple occurrences in the same function — do not \
re-declare the same local variable in each fix; use block scoping \
({ long v = ...; }) or distinct names instead.

"changes": An array of line edits, one entry per source line that must change. \
Each entry has:
  "line"  — 1-based line number in the source file
  "old"   — the exact original line text (including indentation)
  "new"   — the replacement text; use \\n to insert multiple lines

Rules for the changes:
- Generate exactly as many entries as the occurrence count states
- Fix only the reported vulnerability pattern — no unrelated changes
- Never add new calls to malloc, free, realloc, calloc, or alloca
- Never use strcpy, strcat, sprintf, vsprintf, or gets
- Preserve all existing function signatures and struct layouts
- Use bounded alternatives: strncpy+NUL, snprintf, memcpy with explicit \
  length checks
"""

_STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "old":  {"type": "string"},
                    "new":  {"type": "string"},
                },
                "required": ["line", "old", "new"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reasoning", "changes"],
    "additionalProperties": False,
}


def _build_diff_from_changes(changes: list[dict], source_path: str | Path) -> str:
    """Construct a unified diff from a list of {line, old, new} change dicts.

    Applies changes to a copy of the source (in reverse line order so earlier
    line numbers stay valid), then uses difflib to produce a correct unified
    diff with 1 line of context — matching SCAR's existing patch convention.
    """
    source_path = Path(source_path)
    original = source_path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)
    modified = list(lines)

    for change in sorted(changes, key=lambda c: c["line"], reverse=True):
        idx = change["line"] - 1
        if idx < 0 or idx >= len(modified):
            continue
        new_lines = [l + "\n" for l in change["new"].split("\\n")]
        modified[idx : idx + 1] = new_lines

    rel = str(source_path)
    diff = difflib.unified_diff(lines, modified, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=1)
    return "".join(diff)


def _find_occurrences(source: str, finding_line: int) -> str:
    """Return an occurrence-count note for injection into the patch prompt.

    Looks up the exact text at finding_line, counts how many lines in the
    full source match it exactly (after stripping leading/trailing whitespace),
    and returns a sentence telling the LLM precisely how many hunks to produce.

    This prevents the LLM from hallucinating additional occurrences when the
    "fix all occurrences" instruction is active but only one real instance exists.
    """
    lines = source.splitlines()
    if finding_line < 1 or finding_line > len(lines):
        return ""
    target = lines[finding_line - 1].strip()
    if not target:
        return ""
    matches = [i + 1 for i, ln in enumerate(lines) if ln.strip() == target]
    if not matches:
        return ""
    if len(matches) == 1:
        return (
            f"The vulnerable line appears exactly once in this file "
            f"(line {matches[0]}). Generate exactly 1 hunk."
        )
    lines_str = ", ".join(str(n) for n in matches)
    return (
        f"The vulnerable line appears {len(matches)} times in this file "
        f"(lines {lines_str}). Generate exactly {len(matches)} hunks — one per occurrence."
    )


def generate(
    finding: Finding,
    briefing: str,
    source_path: str | Path,
    trace_dir: Path | None = None,
) -> str:
    """Return a unified diff patch for the given finding."""
    source = Path(source_path).read_text(encoding="utf-8", errors="replace")

    occurrence_note = _find_occurrences(source, finding.line)

    user_content = (
        f"Security Briefing:\n{briefing}\n\n"
        f"IKOS Finding:\n"
        f"  Rule: {finding.rule_id}\n"
        f"  File: {finding.file_path}:{finding.line}\n"
        f"  Message: {finding.message}\n\n"
        + (f"Occurrence count: {occurrence_note}\n\n" if occurrence_note else "")
        + f"Source file ({source_path}):\n```c\n{source}\n```"
    )

    messages = [
        {"role": "system", "content": PATCH_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    model = llm.patch_model()
    patch = llm.chat(messages, model=model, temperature=0.1)

    if trace_dir is not None:
        llm.write_trace(
            trace_dir / "2-patch-gen.md",
            title=f"Patch Generation — {Path(source_path).name}:{finding.line}",
            messages=messages,
            response=patch,
            model=model,
            temperature=0.1,
        )

    return patch


def generate_structured(
    finding: Finding,
    briefing: str,
    source_path: str | Path,
    trace_dir: Path | None = None,
) -> str:
    """Fallback patch synthesis using JSON-schema constrained generation.

    Requests {reasoning, changes} instead of a raw unified diff, then
    constructs the diff programmatically via difflib. The reasoning field
    acts as chain-of-thought, which improves scope and null-termination
    handling on small models.

    Raises ValueError if the endpoint returns invalid JSON or an empty
    changes list so the caller can treat it as another validation failure.
    """
    source = Path(source_path).read_text(encoding="utf-8", errors="replace")
    occurrence_note = _find_occurrences(source, finding.line)

    user_content = (
        f"Security Briefing:\n{briefing}\n\n"
        f"IKOS Finding:\n"
        f"  Rule: {finding.rule_id}\n"
        f"  File: {finding.file_path}:{finding.line}\n"
        f"  Message: {finding.message}\n\n"
        + (f"Occurrence count: {occurrence_note}\n\n" if occurrence_note else "")
        + f"Source file ({source_path}):\n```c\n{source}\n```"
    )

    messages = [
        {"role": "system", "content": STRUCTURED_SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name":   "patch",
            "strict": True,
            "schema": _STRUCTURED_SCHEMA,
        },
    }

    model = llm.patch_model()
    raw = llm.chat(messages, model=model, temperature=0.1, response_format=response_format)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"structured patch response is not valid JSON: {exc}") from exc

    changes = parsed.get("changes", [])
    if not changes:
        raise ValueError("structured patch response contains no changes")

    patch = _build_diff_from_changes(changes, source_path)

    if trace_dir is not None:
        llm.write_trace(
            trace_dir / "2-patch-gen-structured.md",
            title=f"Patch Generation (structured) — {Path(source_path).name}:{finding.line}",
            messages=messages,
            response=raw,
            model=model,
            temperature=0.1,
            extra_sections={"Reasoning": parsed.get("reasoning", "")},
        )

    return patch
