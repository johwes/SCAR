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
finding, a security briefing with the relevant source context, and a candidate \
patch. Use GREP: <pattern> to look up any additional code you need.

Your job is to find reasons the patch is WRONG or INSUFFICIENT:
- Does it actually fix the root cause, or just the symptom?
- Does it introduce a new vulnerability?
- Is the fixed code path actually reachable by an attacker?
- Are there other call sites with the same bug pattern left unpatched?
- Does the patch change observable behaviour OUTSIDE the vulnerable code \
  path — e.g. altering protocol semantics, changing return values for \
  intentional edge cases, or modifying logic that was working correctly?
- Does the patch introduce a resource leak — memory allocated but never \
  freed, file descriptors left open, or state that is never reset?
- Could the patch be treating a deliberate design choice as a bug and \
  silently breaking the intended behaviour of the code?

If the patch introduces any regression — even while fixing the reported \
vulnerability — verdict must be INVALID.

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
    briefing: str = "",
    rounds: int = 5,
    tag: str = "",
    trace_dir: Path | None = None,
) -> TriageResult:
    # Use the pre-truncated briefing rather than the full source file.
    # Falls back to reading the full file only if no briefing was provided.
    prefix = f"{tag} " if tag else ""
    context_body = briefing if briefing else Path(source_path).read_text(encoding="utf-8", errors="replace")
    prior: list[str] = []
    verdicts: list[str] = []

    base_context = (
        f"Finding: {finding.rule_id} at {finding.file_path}:{finding.line}\n"
        f"Message: {finding.message}\n\n"
        f"Patch:\n```diff\n{patch}\n```\n\n"
        f"Security Briefing:\n{context_body}"
    )

    review_model = llm.review_model()

    for i in range(rounds):
        print(f"{prefix}[triage round {i+1}/{rounds}] reviewing...", flush=True)
        prior_text = "\n\n---\n".join(prior) if prior else "None yet."
        user_content = (
            f"{base_context}\n\nPrior reasoning:\n{prior_text}"
        )
        messages = [
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        raw_response = llm.chat(messages, model=review_model, temperature=0.2)

        grep_results = ""
        directives = grep_tool.extract_directives(raw_response)
        if directives:
            r = grep_tool.execute(directives, repo_dir)
            if r:
                grep_results = r

        response = raw_response
        if grep_results:
            response += f"\n\n{grep_results}"

        if trace_dir is not None:
            llm.write_trace(
                trace_dir / f"3-triage-round-{i+1}.md",
                title=f"Triage Round {i+1}/{rounds}",
                messages=messages,
                response=raw_response,
                model=review_model,
                temperature=0.2,
                extra_sections={"Grep Results": grep_results} if grep_results else None,
            )

        prior.append(response)
        match = _VERDICT_RE.search(response)
        verdict = match.group(1) if match else "UNCERTAIN"
        verdicts.append(verdict)
        print(f"{prefix}[triage round {i+1}/{rounds}] {verdict}", flush=True)
        # A clear INVALID rarely recovers in later rounds — skip remaining
        # rounds to save tokens; the arbiter still reads all completed reasoning.
        if verdict == "INVALID" and i < rounds - 1:
            print(f"{prefix}[triage] early exit after round {i+1} — INVALID", flush=True)
            break

    # Arbiter round
    print(f"{prefix}[arbiter] issuing final verdict...", flush=True)
    arbiter_input = (
        f"{base_context}\n\nAll prior reasoning:\n" + "\n\n---\n".join(prior)
    )
    arbiter_messages = [
        {"role": "system", "content": ARBITER_PROMPT},
        {"role": "user", "content": arbiter_input},
    ]
    arbiter_response = llm.chat(arbiter_messages, model=review_model, temperature=0.0)

    if trace_dir is not None:
        llm.write_trace(
            trace_dir / "4-arbiter.md",
            title="Arbiter — Final Verdict",
            messages=arbiter_messages,
            response=arbiter_response,
            model=review_model,
            temperature=0.0,
        )

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
