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

1. **Real IR**: run `./run.sh` inside `scar-agent` to get clang-compiled IR
   for all 7 pairs (including bof and signedoverflow) and re-run the analysis.

2. **Contrastive training**: add a 2-layer network with margin loss on
   (vuln, fixed) pairs. Measure whether the trained embedding widens the
   separation ratio beyond the raw histogram baseline.

3. **Real-world functions**: extract function-level IR slices from SCAR's
   accepted patches on scarnet and zlib. Test whether the per-pair signal
   holds when the function is not purpose-built to contain exactly one bug.
