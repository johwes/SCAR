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
- A specific vulnerability found by a static analyzer (IKOS)
- The full source file content

Produce a minimal unified diff (--- a/file / +++ b/file format) that fixes \
the vulnerability. Your patch must:
- Fix only the reported issue — no refactoring
- Never introduce malloc, free, realloc, calloc, or alloca
- Never use strcpy, strcat, sprintf, vsprintf, or gets
- Preserve all existing function signatures and struct layouts
- Use bounded alternatives: strncpy, snprintf, memcpy with explicit length checks

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

    return llm.chat(messages, temperature=0.1)
