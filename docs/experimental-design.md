# Experimental Design: Specialized Tool-Driven Repair Pipelines

This document captures the reasoning behind a potential architectural evolution of
SCAR — moving from a single general-purpose repair pipeline to a set of highly
specialized pipelines, each tuned for a specific bug class and its natural analysis
tool.

---

## Research Foundations

The ideas in this document are not purely speculative — several align with current
published research in the 2024–2026 period on hybrid vulnerability detection for C/C++.

**Irrelevant context degrades small model accuracy measurably.** Research on LLM
reasoning in security contexts has quantified that introducing irrelevant context into
a prompt can reduce reasoning accuracy below 30% in controlled mathematical settings.
This is not a vague concern — it is the primary quantitative justification for
specialized prompts over a universal one, especially on a model with ~3B active
parameters.

**Under-Constrained Symbolic Execution (UCSE) validates the harness generation loop.**
SyzSpec (UC Riverside / ACM CCS 2025) demonstrates automated specification generation
for kernel fuzzers via symbolic pointer reasoning over LLVM IR — starting at a function
boundary rather than program entry, bypassing path explosion. Applied to Syzkaller
against Linux 6.10, it found 86 previously unknown zero-day crashes across 11 bug
classes. The analogous pattern for SCAR is: LLM generates `klee_make_symbolic`
harnesses for user-space functions → KLEE explores paths → specialist LLM repairs
what KLEE finds.

**For user-space programs, harness generation is more tractable than for the kernel.**
SyzSpec solves a hard problem: inferring grammar specifications for stateful,
asynchronous syscall interfaces from deep kernel IR. User-space C functions have
cleaner API boundaries. FuzzGen (Google) does the static-analysis version of this for
library APIs; OSS-Fuzz-gen (Google, 2023–2024) does the LLM version — both generate
libFuzzer harnesses automatically. For KLEE specifically, a function signature like
`int parse_field(char **src, Record *out)` is a natural harness target: the LLM
reads the source, marks the input buffer symbolic, and calls in. This is a simpler
problem than what SyzSpec solves, not a harder one.

**The neuro-symbolic "honest-negative protocol" validates SCAR's triage architecture.**
The research consensus is that LLMs must not be given final verification authority —
they should propose, while a deterministic kernel (Lean 4, Coq, Frama-C, an SMT
solver) accepts or rejects. SCAR's multi-round triage already embodies this: the
skeptical reviewer proposes arguments, the arbiter decides. The natural upgrade path
is inserting Frama-C + E-ACSL between patch generation and triage: LLM synthesizes
ACSL pre/post-conditions and loop invariants, Frama-C translates them to
runtime-checked assertions, SeaHorn verifies via Constrained Horn Clauses. Patches
that pass formal verification skip to a shortened triage (1–2 rounds); those that
fail formal verification go to full triage. This maps exactly to the Frama-C item
in IMPROVEMENTS.md and would constitute a "formal verification specialist track" in
the routing architecture.

---

## The Core Observation

SCAR's current repair loop is generalist by design: every finding, regardless of
origin or class, traverses the same three stages with the same context budget and
the same system prompt. That works well for a capable 70B+ model. For a small model
(35B MoE, ~3B active parameters per token), a universal prompt spreads the model's
attention across irrelevant instructions on every single finding.

The root insight is that **different bug classes need different signals, not more
signals**. A buffer overflow specialist and an authentication logic specialist are
different human engineers — not because one is smarter, but because the domain
knowledge is genuinely different. The LLM equivalent of this is specialized prompts
fed specialized context.

---

## Tool-to-Class Alignment

Each static or dynamic analysis tool already pre-digests findings into a form that
maps naturally to a specific repair strategy:

| Tool | Bug class | What the repair LLM actually needs |
|---|---|---|
| IKOS `boa` / `sio` | Arithmetic bounds overflow | Witness trace + size arithmetic |
| IKOS `dfa` | Double-free / use-after-free | Allocation call graph + ownership |
| IKOS `nullity` | Null pointer dereference | Caller context + guard patterns |
| libFuzzer / AFL++ | Input-triggered crashes | ASan crash trace + concrete input |
| CodeQL / Semgrep | Pattern-matched anti-patterns | Matched pattern + safe alternative |
| KLEE / ESBMC | Path-precise reachability | Concrete counterexample + path trace |
| LLM scan | Semantic / logic / type confusion | Broad context + intent reasoning |

The key point: a fuzzer gives you a concrete crashing input — extraordinarily
high-value context that a general prompt dilutes by mixing it with IKOS witness
injection, safe-string-alternatives tables, and MISRA rules that are irrelevant to
the crash class. A specialist prompt uses *only* what matters for that class.

---

## Proposed Architecture

### Current model (batch, generalist)

```
[tools in parallel] ──► merge findings ──► universal repair loop ──► results
```

### Proposed model (batch, specialist tracks)

```
[tools in parallel]
       │
       ▼
 classify-findings          (pure logic, no LLM — rule_id / keyword routing)
       │
       ├──► repair-arithmetic   (IKOS boa/sio/dbz)
       ├──► repair-memory       (IKOS dfa)
       ├──► repair-nullderef    (IKOS nullity)
       ├──► repair-string-func  (strcpy-class, LLM or Semgrep detected)
       ├──► repair-crash        (fuzzer / ASan findings)
       ├──► repair-symbolic     (KLEE / ESBMC findings)
       └──► repair-semantic     (LLM scan — catch-all for logic/type/intent)
                   │
                   ▼
            merge-results ──► submit ──► report
```

Each specialist track runs in parallel. Each reads only its own findings file from
the shared workspace. Each has a tighter system prompt and only the context signals
relevant to its class — no IKOS witness for string-func bugs, no safe-alternatives
table for null-deref cases, no malloc rules for semantic logic errors.

In Tekton this is implementable today using `when` expressions gating tasks on the
string results of the classify task (`has_arithmetic`, `has_crash`, etc.). No
experimental Tekton features required.

---

## Prompt Specialization Compounds Tool Alignment

Removing irrelevant instructions isn't just token savings — it reduces the
probability the model hallucinates a fix that addresses the wrong issue class.

Example: a `strcpy` buffer overflow found by the LLM scan.

**Current universal prompt context:**
- IKOS witness trace: 0 tokens useful (IKOS doesn't catch string overflows), but
  the prompt still explains witness trace format
- Safe-string-alternatives table: relevant
- malloc/free rules: irrelevant but included
- MISRA arithmetic rules: irrelevant but included

**String-func specialist prompt context:**
- Macro-expanded buffer sizes (`MAX_KEY_LEN → 64`) instead of a grep round-trip
- Focused safe-alternatives reference: exactly `strncpy+NUL` vs `snprintf`, when
  each applies
- Nothing else — no witness trace explanation, no memory ownership rules

For a 3B-active model, that reduction in noise per finding is meaningful. The model
isn't deciding between ownership patterns and string safety rules when the answer
is clearly just "use `snprintf`."

---

## The KLEE Specialization — A Particularly Interesting Case

KLEE operates on LLVM bitcode — the same artifact the `build-bitcode` task already
produces for IKOS — making it a near-zero-cost addition to the existing build
pipeline.

KLEE uses SMT solving to enumerate all feasible execution paths through a function
and generates a concrete `.ktest` input for each path that reaches an error. Unlike
a fuzzer, it finds exact-boundary bugs (a buffer that only overflows at precisely
`N+1` bytes) in one pass rather than waiting for random mutation to hit the boundary.

KLEE is a user-space tool — it runs directly on LLVM bitcode compiled from ordinary
C, not kernel IR. The kernel-equivalent, SyzSpec, had to solve the considerably
harder problem of inferring grammar specifications for stateful syscall interfaces
from deep kernel IR. A user-space function like `int parse_field(char **src, Record
*out)` is a clean, bounded harness target by comparison. The LLM reads the signature
and surrounding source, marks the input buffer symbolic, and calls in directly —
no kernel-level pointer tracing required. This makes LLM-generated KLEE harnesses
for targets like scarnet a tractable near-term step, not a research project.

### The annotation feedback loop

The specialized design opens an interesting pattern not possible with a generalist
pipeline: the LLM can *participate in guiding the tool*, not just repair its
findings.

Before KLEE runs, a lightweight LLM task reads each source function flagged by the
LLM scan or IKOS and generates a `klee_make_symbolic` harness — a small C wrapper
that marks the relevant input buffer symbolic and calls into the function under
analysis. This is Under-Constrained Symbolic Execution: starting at an API boundary
rather than `main` avoids whole-program path explosion while retaining full symbolic
precision over the function being tested.

```
llm-scan findings
       │
       ▼
generate-klee-harness    (LLM generates klee_make_symbolic wrappers)
       │
       ▼
klee-analyze             (KLEE runs on the harness, emits .ktest + error reports)
       │
       ▼
repair-symbolic          (specialist LLM: concrete counterexample + path trace)
```

This creates a **two-stage LLM loop** — one LLM call to guide the tool, one to repair
what the tool found. The repair LLM receives something no generalist pipeline can
produce: a mathematically proven execution path with concrete variable values at each
step. That is the strongest possible input signal for patch generation.

A secondary use: KLEE-generated `.ktest` inputs seed the libFuzzer corpus, giving the
fuzzer a systematically chosen baseline rather than a random start.

---

## The Accuracy vs. Wall Time Tradeoff

```
Accuracy
  ↑
  │                                    ● (specialized tools + specialized LLMs)
  │                 ● (current SCAR)
  │   ● (pure LLM scan, no tools)
  │
  └──────────────────────────────────────────────────► Wall time
     fast                medium                slow
```

The accuracy jump from generalist to specialist is real and not incremental —
specialists genuinely outperform generalists on well-defined classes. The wall time
cost is **front-loaded in the analysis phase**, not the LLM repair phase. Specialized
prompts are actually cheaper and faster per finding. It is the tools themselves that
are slow:

- CodeQL on a medium C project: 10–30 minutes
- AFL++ with meaningful coverage: hours
- KLEE on non-trivial functions: unpredictable
- IKOS whole-program analysis: 5–20 minutes

---

## The Key Architectural Question

Is the pipeline a **batch job** (run all tools, then repair everything) or an
**event-driven system** (findings arrive continuously, repair fires as they land)?

SCAR is currently a batch job — clean, predictable, fits Tekton's DAG model well.
Full event-driven orchestration (findings arriving asynchronously from long-running
tools, repair firing as they land) requires something outside Tekton's native model:
a message queue, a controller watching for findings files, or Tekton Triggers
responding to file-creation events. That is a meaningful complexity jump.

### Proposed middle ground: tiered batch

Rather than choosing between pure batch and full event-driven, a **tiered batch
model** preserves Tekton's simplicity while capturing most of the accuracy benefit:

**Tier 1 — fast tools (minutes):** IKOS, Semgrep, LLM scan. These run in parallel,
findings merge, specialist repair fires immediately. Total wall time: 15–30 minutes
on a medium codebase.

**Tier 2 — slow tools (hours):** libFuzzer, KLEE, CodeQL. These run as a separate
pipeline triggered after Tier 1 completes, or on a separate schedule. Their findings
feed into the same specialist repair infrastructure but don't block Tier 1 results
from being submitted.

This means competition runs (time-bounded) use Tier 1. Deep analysis runs (overnight,
or triggered on a PR merge) use both tiers. The scoring infrastructure receives
results from whichever tiers complete within the time window.

---

## Risks and Open Questions

**Classification accuracy.** A finding that looks like a string-func bug to the
classifier might be a semantic-logic issue where the fix requires understanding intent,
not just swapping to `strncpy`. The semantic catch-all track must exist for this case
and must be no worse than current SCAR behavior.

**Cross-class conflicts.** A function with both a null-deref and a buffer overflow
generates two patches from two specialists that may conflict at the line level. The
merge step needs to detect overlapping line ranges in the same file and flag the
conflict rather than concatenating blindly.

**Maintenance surface.** Multiple system prompts drift independently. The mitigation
is a shared `BASE_SAFETY_RULES` constant that every specialist extends, rather than
five completely independent prompts.

**KLEE harness quality.** The LLM-generated harness may under-constrain the symbolic
inputs (too broad, causing path explosion) or over-constrain them (missing the
vulnerable path). Harness generation is itself a prompt engineering problem — a new
failure mode to manage.

**Diminishing returns on well-covered classes.** IKOS arithmetic bugs already have
a high fix rate with the current pipeline. Specialization there may yield marginal
improvement. The biggest gains are likely in the crash and symbolic tracks, where
the input signal is richer than anything the current pipeline receives.

---

## Summary

The generalist pipeline is the right starting point — it proves the end-to-end loop
works before optimizing. Specialization is the natural next step once the loop is
stable, because the accuracy gains come from *removing* irrelevant context rather
than adding more. The KLEE annotation loop is the most architecturally interesting
case: it turns the LLM from a pure repair agent into an active participant in the
analysis itself, guiding symbolic execution toward the vulnerable paths rather than
waiting for the tool to find them independently.
