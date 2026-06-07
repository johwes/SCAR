"""Stage 2: LLM patch synthesis.

Given an IKOS finding and a security briefing from context_gen, produces a
unified diff patch that fixes the vulnerability while respecting embedded
safety constraints (no malloc, no banned string functions).
"""

from pathlib import Path

from . import llm
from .sarif_bridge import Finding

PATCH_SYSTEM_PROMPT = """\
You are an expert C security engineer. You will be given:
- A security briefing describing the file's architecture and data flows
- A specific vulnerability found by a static analyzer
- The full source file content

Produce a minimal unified diff (--- a/file / +++ b/file format) that fixes \
the vulnerability. Use only 1 line of context (not the default 3) in each \
hunk — the smallest context window that unambiguously locates the change. \
Your patch must:
- Before writing the diff, scan the ENTIRE source file for every occurrence \
  of the same vulnerable pattern (e.g. the same unsafe function call or the \
  same missing check). Fix ALL occurrences in a single multi-hunk patch — \
  leaving any identical instance unpatched makes the fix incomplete.
- Fix only the reported vulnerability pattern — no unrelated refactoring
- Never add new calls to malloc, free, realloc, calloc, or alloca that did \
  not exist in the original code — preserving an existing allocation call \
  while adding a bounds check around it is fine
- Never use strcpy, strcat, sprintf, vsprintf, or gets
- Preserve all existing function signatures and struct layouts
- Use bounded alternatives: strncpy, snprintf, memcpy with explicit length checks
- Do NOT change behaviour outside the vulnerable code path — if a design \
  choice looks unusual but is intentional (e.g. a deliberate delimiter, a \
  deliberate cleanup strategy), leave it alone

Output ONLY the unified diff, no explanation.
"""


def generate(
    finding: Finding,
    briefing: str,
    source_path: str | Path,
) -> str:
    """Return a unified diff patch for the given finding."""
    source = Path(source_path).read_text(encoding="utf-8", errors="replace")

    user_content = (
        f"Security Briefing:\n{briefing}\n\n"
        f"IKOS Finding:\n"
        f"  Rule: {finding.rule_id}\n"
        f"  File: {finding.file_path}:{finding.line}\n"
        f"  Message: {finding.message}\n\n"
        f"Source file ({source_path}):\n```c\n{source}\n```"
    )

    messages = [
        {"role": "system", "content": PATCH_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    return llm.chat(messages, model=llm.patch_model(), temperature=0.1)
