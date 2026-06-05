"""Stage 3: Skeptical triage (adapted from nano-analyzer).

Multi-round adversarial verification of a candidate patch. A Skeptical
Reviewer attempts to disprove the patch's correctness over N rounds, then an
Arbiter issues a final VALID / INVALID / UNCERTAIN verdict with a confidence
score.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from . import llm, grep_tool
from .sarif_bridge import Finding

TRIAGE_SYSTEM_PROMPT = """\
You are a skeptical security reviewer. You will be given a vulnerability \
finding, the original source file, and a candidate patch.

Your job is to find reasons the patch is WRONG or INSUFFICIENT:
- Does it actually fix the root cause, or just the symptom?
- Does it introduce a new vulnerability?
- Is the fixed code path actually reachable by an attacker?
- Are there other call sites with the same bug pattern left unpatched?

Use GREP: <pattern> to search for related code in the repository.
End your response with one of: VALID, INVALID, or UNCERTAIN.
"""

ARBITER_PROMPT = """\
You are the final arbiter. Review all prior reasoning rounds below and issue \
a definitive verdict.

Respond with:
VERDICT: <VALID|INVALID|UNCERTAIN>
CONFIDENCE: <0-10>
REASON: <one sentence>
"""

_VERDICT_RE = re.compile(r"\b(VALID|INVALID|UNCERTAIN)\b")
_ARBITER_RE = re.compile(
    r"VERDICT:\s*(VALID|INVALID|UNCERTAIN).*?CONFIDENCE:\s*(\d+).*?REASON:\s*(.+)",
    re.DOTALL,
)


@dataclass
class TriageResult:
    verdict: str        # VALID | INVALID | UNCERTAIN
    confidence: float   # 0.0 – 1.0
    chain: str          # e.g. "VVIVV"
    reason: str
    rounds: int


def run(
    finding: Finding,
    patch: str,
    source_path: str | Path,
    repo_dir: str | Path,
    *,
    rounds: int = 5,
) -> TriageResult:
    source = Path(source_path).read_text(encoding="utf-8", errors="replace")
    prior: list[str] = []
    verdicts: list[str] = []

    base_context = (
        f"Finding: {finding.rule_id} at {finding.file_path}:{finding.line}\n"
        f"Message: {finding.message}\n\n"
        f"Patch:\n```diff\n{patch}\n```\n\n"
        f"Source:\n```c\n{source}\n```"
    )

    for _ in range(rounds):
        prior_text = "\n\n---\n".join(prior) if prior else "None yet."
        user_content = (
            f"{base_context}\n\nPrior reasoning:\n{prior_text}"
        )
        messages = [
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        response = llm.chat(messages, temperature=0.2)

        directives = grep_tool.extract_directives(response)
        if directives:
            grep_results = grep_tool.execute(directives, repo_dir)
            if grep_results:
                response += f"\n\n{grep_results}"

        prior.append(response)
        match = _VERDICT_RE.search(response)
        verdicts.append(match.group(1) if match else "UNCERTAIN")

    # Arbiter round
    arbiter_input = (
        f"{base_context}\n\nAll prior reasoning:\n" + "\n\n---\n".join(prior)
    )
    arbiter_messages = [
        {"role": "system", "content": ARBITER_PROMPT},
        {"role": "user", "content": arbiter_input},
    ]
    arbiter_response = llm.chat(arbiter_messages, temperature=0.0)

    return _parse_result(arbiter_response, verdicts, rounds)


def _parse_result(arbiter: str, verdicts: list[str], rounds: int) -> TriageResult:
    chain = "".join(v[0] for v in verdicts)
    valid_count = verdicts.count("VALID")
    confidence = valid_count / rounds if rounds else 0.0

    match = _ARBITER_RE.search(arbiter)
    if match:
        verdict = match.group(1)
        confidence = int(match.group(2)) / 10.0
        reason = match.group(3).strip()
    else:
        verdict = "UNCERTAIN"
        reason = arbiter.strip()[:200]

    return TriageResult(
        verdict=verdict,
        confidence=confidence,
        chain=chain,
        reason=reason,
        rounds=rounds,
    )
