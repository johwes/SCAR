---
language: c
license: mit
tags:
  - vulnerability-detection
  - graph-neural-network
  - llvm-ir
  - static-analysis
  - security
datasets:
  - devign
metrics:
  - accuracy
---

# scar-gnn-defect-detector

A lightweight graph neural network that classifies C functions as vulnerable or safe
using their LLVM IR control-flow graphs. Designed as a **zero-cost pre-filter** for
LLM-based vulnerability triage pipelines.

## Model Description

- **Architecture:** `DefectGNN` — two `RGCNConv` layers (3 relation types: CFG, DFG,
  global context edges), GRU-based graph readout, two-layer MLP classifier
- **Input:** LLVM IR compiled with `clang -O0 -fno-inline -S -emit-llvm`; each basic
  block becomes a node with 45 semantic features (opcode distribution, branch density,
  memory op ratio, call density, phi count, block size)
- **Output:** Probability score ∈ [0, 1] that the function is vulnerable
- **Parameters:** ~305 KB (`hidden=128`)

## Training

| Dataset | Split | Functions |
|---|---|---|
| Devign (FFmpeg, QEMU, Linux, LibreSSL) | train | ~21,854 |
| Devign | validation | ~2,732 |
| Devign | test | ~2,732 |

Training: 60 epochs, Adam lr=1e-3, cosine LR decay, hidden=128.

## Performance

| Setting | Accuracy |
|---|---|
| Majority-class baseline | 56.60% |
| **This model (Devign test set)** | **57.84%** |
| CodeBERT (source text) | 63.43% |

The 1.24 pp gap over baseline is modest on Devign's balanced test set. On real-world
code the **ranking behaviour** matters more than the binary accuracy: the model assigns
meaningfully higher scores to vulnerable functions, making it useful as a ranker even
when it does not clear a hard decision threshold.

### Real-world validation: scarnet

Applied to `johwes/scarnet` (a small intentionally-vulnerable C server, 19 functions
across 5 source files, 13 known-vulnerable):

| Metric | Value |
|---|---|
| Known-vulnerable functions in top-13 of 19 | **10 / 13 (77%)** |
| Precision at top-13 | **77%** |
| Recall at top-13 | **77%** |

Compilation flag used: `clang -O0 -fno-inline -S -emit-llvm` (required — `-O1` inlines
small functions into their callers, hiding them from per-function analysis).

**False negatives (3):** `handle_set` (format-string bug), `handle_del` (null deref
on missing key), `scar_log` (off-by-one in format buffer). All three are semantic bugs
with no structural IR signature — the wrong opcode is never emitted, only the wrong
_argument_ or _comparison operand_. These are LLM domain, not GNN domain.

**False positives (3):** `main`, `session_free`, `handle_get` — structurally complex
functions that score high but are not vulnerable. All are immediately dismissible in
a one-sentence LLM triage step.

## Intended Use

This model is a **zero-cost ranker**, not a hard gate. Recommended pipeline:

```
clang -O0 -fno-inline -S -emit-llvm src/*.c -I include/ -o fn.ll
python scan_ir.py fn.ll --all-functions --threshold 0.5
# → ranked list of functions by vulnerability score
# → feed top-N to LLM for semantic triage
```

Use it to decide *which functions to show an LLM*, not to make final vulnerability
decisions. The LLM handles the semantic bugs the GNN misses.

## Limitations

- **Topology-only:** node features are opcode categories; identifier names, string
  literals, and type tokens are discarded. Semantic bugs (wrong comparison operator,
  wrong format string, off-by-one in a constant) produce identical IR topology to
  correct code and are undetectable.
- **Block-level granularity:** each basic block is one node. Fine for function-level
  ranking; not suitable for pinpointing the exact buggy line.
- **Devign distribution:** trained on C from large open-source projects; may not
  generalise well to embedded, kernel, or heavily macro-expanded code.
- **Contrastive learning does not help:** three contrastive experiments (SupCon,
  block-level triplet, instruction-level triplet) all collapsed because 98–99% of
  nodes are identical between a vulnerable function and its patch. The ranking
  capability comes from the supervised classifier, not metric learning.

## Repository & Reproducibility

Source code, training scripts, and experiment documentation:
**[johwes/SCAR](https://github.com/johwes/SCAR)**

Key files:
- `experiments/ir_embed_demo/train_gnn/train.py` — training script
- `experiments/ir_embed_demo/train_gnn/preprocess.py` — IR → graph extractor
- `experiments/ir_embed_demo/train_gnn/scan_ir.py` — inference CLI
- `docs/experiments/ir-embed.md` — full experiment log (§4a–§9)

## Citation

If you use this model, please cite the SCAR repository:

```
@misc{scar-gnn-2024,
  title  = {SCAR GNN Defect Detector},
  author = {johwes},
  year   = {2024},
  url    = {https://huggingface.co/johwes/scar-gnn-defect-detector}
}
```
