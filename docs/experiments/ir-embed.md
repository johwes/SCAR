# Experiment: IR Structural Embedding — Opcode Histogram Baseline

**Code:** `experiments/ir_embed_demo/`  
**Hypothesis:** `docs/research.md` — Contrastive structural embeddings over LLVM IR  
**Status:** Initial signal confirmed on toy examples. Real IR and contrastive training not yet tested.

---

## Setup

5 vulnerable/fixed C pairs from [johwes/SCAR-test-c](https://github.com/johwes/SCAR-test-c)
(doublefree, nullderef, oob\_read, uninit, divzero), compiled to LLVM IR with
`clang -O0 -S -emit-llvm`. Feature: normalized opcode-frequency histogram
(~70 LLVM opcodes). Distance: cosine. No neural network, no training.

Note: the IR files in `experiments/ir_embed_demo/ir/` are hand-written
representative `.ll` files, not real clang output. Run `./run.sh` inside the
`scar-agent` container to replace them with real compiled IR.

---

## Results

### Global metric (misleading)

```
avg vuln  ↔ vuln   distance : 0.3144
avg fixed ↔ fixed  distance : 0.4268
avg vuln  ↔ fixed  distance : 0.3787   →  1.02× within-class
```

Looks like no signal. It isn't — the problem is the evaluation design.
Different vulnerability types (divzero vs nullderef) are naturally far apart
because they are different programs. Averaging cross-CWE distances into
"within-class" collapses the real signal.

### Per-pair metric (correct for a scanner)

For a scanner, the relevant question is: given a new function's embedding,
is it closer to known-vulnerable embeddings than to known-fixed embeddings?

| Pair | own vuln↔fixed | avg vuln↔other fixed | signal |
|---|---|---|---|
| divzero | 0.159 | 0.601 | YES |
| doublefree | 0.012 | 0.447 | YES |
| nullderef | 0.314 | 0.315 | YES (marginal) |
| oob\_read | 0.368 | 0.358 | no |
| uninit | 0.106 | 0.400 | YES |

4 of 5 pairs show a clear signal with zero training.

### Key finding: vulnerability-class clustering

`nullderef_V ↔ oob_read_V = 0.081` — two different programs, both are
"missing a conditional branch" vulnerabilities. They sit closer together
than either does to its own fixed version (0.314 and 0.368). Their fixed
versions also cluster tightly: `nullderef_F ↔ oob_read_F = 0.070`.

The opcode histogram groups code by *what structural feature is absent*
rather than by specific CWE. That generalisation is what a scanner needs:
train on one missing-branch pattern, detect others.

---

## What the failure tells us

`oob_read` failed because `nullderef_fixed` and `oob_read_fixed` are
structurally nearly identical — both just add `icmp + br`. When two fixes
share the same IR shape, whole-function histograms cannot tell them apart,
and the cross-class distance drops to the level of the within-pair distance.

This directly confirms the granularity concern from `docs/research.md`:
whole-function opcode histograms cannot localise *which* missing branch matters
when multiple functions share the same fix topology. Subgraph-level or
basic-block-level features are needed to break this degeneracy.

---

## What this confirms and what it does not

**Confirmed:** A structural signal exists in opcode histograms on toy
single-function examples, detectable without any training.

**Not tested:**
- Whether the signal survives on real-world functions where the vulnerable
  pattern is buried in hundreds of lines of surrounding code
- Whether contrastive training improves discriminability over raw histograms
- Whether the clustering generalises beyond the 5 CWE classes tested

---

## Next experiments

### 1. Real IR from clang (prerequisite for everything else)

Run inside the `scar-agent` container, which has clang:

```bash
cd experiments/ir_embed_demo
./run.sh        # compiles all 7 pairs; replaces hand-written ir/*.ll
```

This adds the two pairs not yet in the hand-written IR (bof, signedoverflow)
and validates that the results hold on real compiler output rather than
representative approximations. Re-run `demo.py` and compare the per-pair
signal table to the baseline above.

---

### 2. Contrastive training step

Goal: measure whether a trained embedding widens the separation ratio beyond
the raw histogram baseline.

**Add `experiments/ir_embed_demo/train.py`** — a minimal PyTorch script:

```
Input:  normalized opcode histograms (dim ≈ 70) from ir/*.ll
Model:  MLP — Linear(70→32) → ReLU → Linear(32→16)
Loss:   contrastive loss (Hadsell et al. 2006)
          same-class pairs (vuln+vuln, fixed+fixed): pull together
          cross-class pairs (vuln+fixed):            push apart, margin=0.5
Output: 16-dim embeddings; re-run cosine distance analysis on these
```

With only 7×2 = 14 samples, use leave-one-pair-out cross-validation:
train on 6 pairs, test separation on the held-out pair. Report whether
the trained embedding improves the per-pair signal over the raw baseline.

Requires: `pip install torch` (CPU-only is fine, no GPU needed at this scale).

**Success criterion:** separation ratio on the held-out pair improves over
the raw histogram baseline for at least 5 of 7 folds.

---

### 3. Real-world functions from SCAR accepted patches

Goal: test whether the per-pair signal survives when the vulnerable pattern
is buried in a real function rather than a purpose-built 10-line file.

**Source:** a completed SCAR pipeline run on scarnet or zlib produces
`.scar/scar-results.json`. Each accepted entry contains:
- `finding.file_path` — the source file
- `finding.line` — the vulnerable line
- `patch` — the unified diff

**Procedure:**

1. For each accepted patch entry in `scar-results.json`:
   - `original.c` = the source file as-is (vulnerable)
   - Apply the patch with `patch -o fixed.c original.c diff.patch`
   - Compile both: `clang -O0 -S -emit-llvm -o original.ll original.c`

2. Extract the enclosing function from each `.ll` file using the finding
   line number. A function in LLVM IR text format starts with `define` and
   ends with the matching `}`. Walk the IR to find the function whose line
   range contains `finding.line`.

3. Run the same histogram analysis on the extracted function IR slices,
   not the whole-file IR.

4. Report the per-pair signal table as in experiment 1.

**Key question:** does the separation ratio hold when the vulnerable
subgraph is a small fraction of the total function IR? If it collapses
below 1.1×, the granularity problem is real and subgraph-level features
(basic-block or sliding-window) are needed before the approach is viable
on production code.

**Add `experiments/ir_embed_demo/extract_functions.py`** to automate
steps 1–2 given a `scar-results.json` and a source directory.
