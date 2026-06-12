# Context Window Management in SCAR

Token cost and model quality are related but distinct problems. `token-efficiency.md`
covers cost and latency. This document covers what happens to **reasoning quality**
as context grows — and why some parts of SCAR's prompts must not be compressed even
when they are large.

---

## The lost-in-the-middle problem

Language models do not attend equally to all parts of a long prompt. Research
(Liu et al., 2023 — "Lost in the Middle") shows a consistent U-shaped attention
pattern: content near the start and end of a prompt is recalled reliably; content
buried in the middle is attended to less and is more likely to be ignored or
misremembered.

This is not a failure of a specific model — it is a structural property of the
attention mechanism under long-range dependencies. Every current transformer
architecture exhibits it to some degree. A larger context window means the model
*can* accept more tokens; it does not mean the model *uses* all of them equally.

**Rough quality thresholds observed in practice:**

| Prompt size | Behaviour |
|---|---|
| < 4K tokens | Reliable — model attends fully |
| 4K–8K tokens | Generally good; model-dependent |
| 8K–16K tokens | Degradation begins for complex reasoning tasks |
| > 16K tokens | High risk of lost-in-the-middle on critical details |

These are not hard limits. Simple retrieval tasks tolerate larger contexts better
than complex multi-step reasoning tasks like patch generation and triage.

---

## How context accumulates in SCAR

Each finding passes through three LLM stages. The context grows at each stage.

### Stage 1 — context_gen

Input: the C source file + finding location.
Output: a security briefing.

SCAR truncates source to the enclosing function boundary (capped at 300 lines)
rather than sending the full file. This keeps stage 1 prompts in the 2K–4K token
range for typical C files. See `token-efficiency.md` for details.

### Stage 2 — patch_gen

Input: the briefing + finding metadata + occurrence note + full source file.
Output: a unified diff.

The full source file is included here so the model can locate the exact lines to
patch. Function-level truncation from stage 1 is not applied here — the patch
must reference real line numbers and context lines, so the model needs the
complete file. For large files this can push stage 2 prompts to 5K–8K tokens.

### Stage 3 — triage (N rounds + arbiter)

Input: finding + patch + briefing + all prior round responses.
Output: per-round verdict, then final arbiter verdict.

This is where context accumulates most aggressively. Each triage round adds
its full response — typically 500–1000 tokens of detailed reasoning, grep results,
and quoted code — to the context for every subsequent round. By round 3 on a
medium-sized file, the arbiter may see 6K–10K tokens of accumulated reasoning.

At 5 rounds the arbiter context can exceed 12K tokens, putting key evidence from
round 2 or 3 squarely in the "lost middle" zone.

---

## Why triage reasoning cannot be naively compressed

The obvious response to accumulating context is to summarise: extract the verdict
and one key sentence per round, discard the rest. This is appealing — 50 tokens
per round instead of 800 — but it breaks the triage design in several ways.

**Each round builds adversarially on prior reasoning.** Round 2 is not an
independent reviewer — it can challenge, extend, or confirm what round 1 found.
If round 1 ran `GREP: strcpy` and found three call sites, round 2 can say "given
those three sites, the patch is incomplete at line 83." Compress round 1 to a
single sentence and round 2 loses the grep evidence entirely. It may redundantly
re-run the same grep, or miss that the evidence was already gathered.

**The arbiter weighs reasoning quality, not just verdict counts.** An INVALID
verdict backed by grep evidence of an unpatched sibling call site should carry
more weight than a speculative INVALID with no supporting evidence. The arbiter
needs to see both verdicts and the reasoning behind them to produce a calibrated
confidence score. Summaries make all INVALIDs look equivalent.

**A one-sentence extraction is itself a lossy LLM task.** To extract "the right"
sentence from a 1000-token reasoning block, you need to understand which argument
is decisive — and that judgement requires reading the whole block. A regex or
heuristic extraction would often pick a supporting detail rather than the key
claim.

The conclusion: **compressing triage rounds is the wrong lever.** The triage
accumulation is doing real work; compressing it discards that work.

---

## The right levers

If context size becomes a quality problem, pull these in order:

### 1. Cap the briefing (safe)

`context_gen` produces a security briefing that is injected into every subsequent
stage. A briefing that is too long pushes real evidence (grep results, patch
content) toward the middle of the triage prompt. The 300-line function cap in
`context_gen` already addresses this; if briefings are still long, tighten the
cap further. The model can issue `GREP:` directives during triage to fetch any
additional context it needs.

### 2. Cap the source excerpt in patch_gen (safe with care)

For very large files, a ±150-line window centred on the finding line is often
sufficient for patch generation. The risk: if the vulnerable pattern appears in a
different function, the model will not see it. Measure acceptance rate before and
after to confirm the cap does not hurt recall.

### 3. Reduce triage rounds (blunt)

Lowering `--triage-rounds` from 3 to 2 halves the accumulated prior reasoning
in the arbiter context. It also reduces the adversarial pressure on the patch,
so some incorrect patches may slip through. Use only if context quality is
measurably degrading; not recommended for competition use.

### 4. Summarise rounds only for the arbiter (surgical)

If the arbiter alone is seeing too much context, a middle-ground approach is to
pass full round text to each triage round (preserving adversarial depth) and pass
only verdict + key-sentence summaries to the arbiter. The arbiter's role is to
synthesise verdicts, not to perform new analysis, so it is more tolerant of
compressed prior context than the rounds themselves.

This requires an LLM summarisation call per round before the arbiter step. At
the current default of 3 rounds with early-exit behaviour, it is not worth the
extra cost and latency. It becomes relevant at 5+ rounds or on large files.

---

## Warning signals

These indicate that context size is affecting quality:

- **Arbiter verdict contradicts the triage chain.** If rounds 1–3 all say INVALID
  but the arbiter says VALID with high confidence, the arbiter likely lost track
  of the reasoning from middle rounds.
- **Round N raises an objection identical to round 1.** The reviewer has lost
  the prior round context and is retreading the same ground rather than building
  on it.
- **Confidence scores are unusually high on clearly incomplete patches.** The
  model may have attended to the patch and the briefing but not to the middle
  rounds where the objections were raised.
- **Grep results from one round are ignored in subsequent rounds.** Evidence
  gathered in round 1 should influence round 2; if round 2 asks the same grep
  question, the prior response was not attended to.

Inspect `.scar/traces/<id>/3-triage-round-*.md` to diagnose. Cross-reference
with `docs/inspecting-traces.md` for the trace audit workflow.

---

## Observed context sizes (scarnet, default 3-round triage)

| Stage | Typical prompt tokens |
|---|---|
| context_gen | 1,500–3,000 |
| patch_gen | 2,500–5,000 |
| triage round 1 | 2,500–5,000 |
| triage round 2 | 3,500–7,000 |
| triage round 3 (if reached) | 4,500–9,000 |
| arbiter | 5,000–10,000 |

With the default 3-round triage and early-exit on INVALID, the arbiter context
stays below 10K tokens for scarnet-sized files — inside the safe zone for current
models. Monitor this as target codebases grow.
