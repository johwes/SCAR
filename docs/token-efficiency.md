# Token Efficiency in SCAR

Token count drives both cost and latency. On a toy corpus like scar-test-c
(7 small files, ~30 lines each) a full v2 run uses ~168k tokens. On a real
codebase like scarnet (a multi-file TCP server) the same pipeline uses ~483k
tokens. Understanding where tokens go — and how SCAR manages them — helps
when tuning the pipeline or extending it with new tools.

## Where tokens go

**Source context size** is the dominant cost on real codebases. Every finding
sends source code through context generation, patch generation, and triage ×N
rounds. A finding in a 600-line handler file sends 600 lines in each prompt;
a finding in a 30-line toy file sends 30. This compounds across rounds: by
triage round 3 the model also accumulates all prior round responses.

**The false-positive tax** is the second driver. A finding that is ultimately
rejected by the arbiter still pays the full token budget for context generation,
patch generation, and every triage round before the rejection. On scarnet, 5 of
15 findings were rejected — those 5 paid the same per-finding cost as the 10
that were accepted.

**Completion tokens** often exceed prompt tokens on reasoning-heavy models.
Qwen3 and similar chain-of-thought models generate long internal reasoning
traces before emitting the actual patch or verdict. A 170k-prompt run on scarnet
produced 310k completion tokens — a 1.8:1 ratio.

## Optimisations in place

### Function-boundary truncation

Instead of sending the entire source file, `context_gen.py` extracts only the
C function enclosing the vulnerable line. It uses a brace-counting heuristic
to find the function's opening and closing braces, then includes the function
signature above and caps the result at 300 lines.

- On large files this can cut prompt tokens by 60–80% per finding.
- If the heuristic fails (unusual macro-heavy code), it falls back to a ±100-line
  window centred on the vulnerable line.
- If the function itself exceeds 300 lines, a window centred on the target line
  is used, with a header noting the truncation.
- Cross-function context (callers, constants, type definitions) is still
  reachable via the agentic `GREP:` directives the LLM can emit during triage.

### Early-exit triage

The skeptical triage loop exits after any round that returns `INVALID`, rather
than always running all N rounds. The arbiter still receives and reads all
completed reasoning before issuing its final verdict.

A clear `INVALID` on round 1 almost never recovers to `VALID` in later rounds —
later rounds typically add more objections rather than refuting earlier ones.
Skipping the remaining rounds saves 1–2 rounds × (source + patch + accumulated
prior reasoning) in completion tokens for every rejected finding.

The triage chain in the output reflects the rounds that actually ran:
- `I` — rejected on round 1, 2 rounds skipped
- `VI` — passed round 1, rejected on round 2, 1 round skipped
- `VVV` — all 3 rounds ran, all VALID (no early exit)

### Parallel task execution

`ikos-analyze`, `llm-scan`, and `osscrs-scan` run in parallel after
`build-bitcode`. IKOS contributes deterministic findings at zero scan-phase
token cost — it uses abstract interpretation rather than LLM calls. Adding
IKOS to v1 (LLM-only) increased total tokens by 41% but improved score by 76%,
because IKOS findings arrive with proven witness traces that shorten the
patch-generation and triage prompts.

### LLM retry with backoff

Transient server disconnects previously aborted entire pipeline runs, wasting
all tokens already spent. `llm.py` retries up to 3 times with exponential
backoff (3s, 6s, 12s), converting what were hard failures into brief pauses.

### Per-task token accounting

`llm-scan` runs in a separate container and writes its token count to
`token-usage-llm-scan.json`. The repair loop merges all `token-usage-*.json`
partials at the end of the run before writing the final `token-usage.json`.
This gives the `report` task accurate totals across all containers and makes
per-stage token costs visible in the dashboard submission.

### Python patch fallback

When `patch --fuzz=3 -l` fails (typically because the LLM hallucinated context
lines), a Python fallback in `validator.py` locates the change block by line
number with a ±15-line search and applies only the `+`/`-` lines, ignoring
context entirely. This avoids a second full LLM call to regenerate the patch.

## Further optimisations (not yet implemented)

These are tracked in [`IMPROVEMENTS.md`](../IMPROVEMENTS.md):

**Per-file context caching** — if multiple findings point at the same source
file, `context_gen` currently runs independently for each. One briefing per
file, shared across all findings in that file, would eliminate the redundant
calls.

**Pre-filter pass** — a cheap first-pass prompt ("is this finding plausible?")
using a small or fast model before committing to full context generation and
patch synthesis. Trades some recall for lower cost on high-finding-count runs.
Not recommended for competition use where maximising recall matters.

**Parallel repair loop** — findings are currently processed sequentially.
Grouping by file and processing file-groups concurrently with a thread pool
would cut wall-clock time roughly in half. See IMPROVEMENTS.md for the
patch-dependency tracking needed to make this safe.

**Program slicing** — instead of function-level truncation, extract a precise
data-flow slice around the vulnerable statement from the LLVM IR. Much smaller
context, much higher signal — but requires deeper LLVM tooling.

## Observed token budgets

| Run | Target | Findings in | Accepted | Prompt tokens | Completion tokens | Total |
|---|---|---|---|---|---|---|
| v1 | scar-test-c | 4 | 4 | ~40k | ~79k | 118k |
| v2 | scar-test-c | 7 | 7 | ~45k | ~123k | 168k |
| v2 | scarnet | 15 | 10 | 172k | 311k | 483k |

The completion:prompt ratio grows with model reasoning depth and source file
size. Function-boundary truncation and early-exit triage primarily reduce prompt
tokens (smaller context per round) and completion tokens (fewer rounds for
rejected findings) respectively.
