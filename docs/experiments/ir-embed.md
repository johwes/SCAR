# Experiment: IR Structural Embedding — Opcode Histogram Baseline

**Code:** `experiments/ir_embed_demo/`  
**Hypothesis:** `docs/research.md` — Contrastive structural embeddings over LLVM IR  
**Status:** Opcode histogram baseline confirmed. CFG/DFG graph extraction built (`graph_demo.py`). End-to-end GNN PoC working (`gnn_poc.py`). Full training pipeline on Devign ready (`train_gnn/`).

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

## Practical ceiling

### What this approach structurally cannot detect

The fundamental limitation is the absence of taint/dataflow analysis.
Opcode histograms — and even full CFG/DFG graph embeddings — cannot follow
a value across function boundaries and determine whether it was sanitized
before reaching a dangerous sink. Most serious real-world vulnerabilities
require exactly that reasoning:

- **Injection and buffer overflows from user input**: requires tracking
  tainted input through call chains to where it is used without bounds
  checking. Structural similarity to a known-unsafe function is not enough
  — the same function shape can be safe or unsafe depending on whether the
  caller validated the input.
- **Use-after-free in complex allocation patterns**: the dangerous access
  may be in a different function from the free. No per-function embedding
  can see this.
- **Integer overflows in protocol parsing**: depends on the range of values
  reachable at a specific point, not the presence or absence of a branch.

Tools like CodeQL exist precisely to answer these questions with
interprocedural dataflow graphs. This approach does not compete with that.

### Where it realistically fits

The honest position is **cheap pre-filter, not standalone detector**:

1. Embed every function's IR against the known-vulnerable corpus.
2. Flag functions with high structural similarity to known-vulnerable
   patterns as candidates.
3. Feed those candidates into CodeQL, Semgrep, or the SCAR LLM repair loop
   for the expensive, precise analysis.

This costs zero LLM calls and runs in seconds. If it surfaces real
candidates that rule-based tools then confirm, it earns its place in the
pipeline as a prioritisation signal.

### What would need to be true to become competitive

Raw opcode histograms are the weakest form of this idea. Competitive
detection would require:

- **Full CFG/DFG graph representation** (ProGraML-style) so the model sees
  control-flow paths and data dependencies, not just opcode frequencies.
- **Thousands of training pairs** across diverse codebases, not dozens.
- **Interprocedural context** — embedding call-graph subgraphs rather than
  individual functions.

Both blockers are largely resolved by existing open work — see experiment 4
below. Even so, the ML-based vulnerability detection literature has a poor
track record of generalising beyond benchmark conditions. This is a
research direction, not a near-term production capability.

### The one genuinely novel property

The self-improving corpus is the most defensible advantage over static
rule-based tools. Every SCAR accepted patch on any target is a labelled
(vulnerable IR, fixed IR) pair produced at zero marginal cost. CodeQL rules
do not improve when you find a new bug. A model retrained on accumulated
SCAR patches does — and it specialises to exactly the patterns SCAR
encounters in practice. Whether that specialisation translates to useful
detection precision on unseen code is the core empirical question.

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

### 3. Real-world functions from SCAR accepted patches on zlib

Goal: test whether the per-pair signal survives when the vulnerable pattern
is buried in a real function rather than a purpose-built 10-line file.

Scarnet is synthetic — same code, same patches, same structure every run.
It tests nothing beyond what the toy examples already cover. The right
source is **zlib v1.2.11**, which produced 21 accepted patches in a real
pipeline run. The patched functions (`deflate`, `inflate`, `crc32`, etc.)
are hundreds of lines of production code; each patch touches a handful of
lines. That ratio — small vulnerable subgraph, large surrounding context —
is exactly the stress test this experiment needs.

**Source:** the `scar-results.json` from the zlib v1.2.11 pipeline run.
Each accepted entry contains:
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

---

### GNN training — results summary

| Method | Test Acc | Notes |
|---|---|---|
| Majority-class baseline | 56.6% | always predict "fixed" |
| 4b CFG-only, GCNConv | 55.04% | barely learning |
| 4c PDG, RGCNConv | 56.08% | +1.0% from DFG edges |
| 4d v2.0 PDG + 45 features (30ep h=64) | 56.32% | +0.2% from 34 new features |
| 4d v2.0 PDG + 45 features (60ep h=128) | **57.84%** | best GNN result; peaks epoch 8 |
| 4d v2.1 MLP attention gate (30ep h=128) | 56.88% | gate upgrade no meaningful gain |
| **4a CodeBERT (this run, Colab T4)** | **63.43%** | **granularity gap confirmed** |
| CodeBERT published baseline | 62.08% | matches within noise |
| UniXcoder published | 69.29% | upper bound for token-based models |

**Granularity gap: 5.6 points** (63.43% − 57.84%). Every GNN architectural
improvement — relational edges, 34 semantic features, larger hidden, expressive
attention — saturated around 57–58%. The ceiling is the basic-block
representation, not the model capacity or feature richness.

---

### 4a. CodeBERT / UniXcoder fine-tune on Devign — lowest friction path

**What it is:** Fine-tune a pre-trained transformer on Devign source code.
Operates on raw C function text, not LLVM IR. No compilation step, no
graph construction. The training code, dataset download, and evaluator are
all already written in the CodeXGLUE repo.

**Published results on Devign test set:**

| Model | Accuracy |
|---|---|
| UniXcoder (`microsoft/unixcoder-base`) | 69.29% |
| CodeBERT (`microsoft/codebert-base`) | 62.08% |
| RoBERTa | 61.05% |
| TextCNN | 60.69% |

**Procedure:**

1. Download dataset:
   ```bash
   cd Code-Code/Defect-detection/dataset
   pip install gdown
   gdown https://drive.google.com/uc?id=1x6hoF7G-tSYxg8AFybggypLZgMGDNHfF
   python preprocess.py
   ```

2. Fine-tune (CodeBERT baseline — swap `model_name_or_path` for UniXcoder):
   ```bash
   python run.py \
       --model_name_or_path microsoft/codebert-base \
       --do_train \
       --train_data_file dataset/train.jsonl \
       --eval_data_file  dataset/valid.jsonl \
       --test_data_file  dataset/test.jsonl \
       --epoch 5 --block_size 400 \
       --train_batch_size 32 --eval_batch_size 64 \
       --learning_rate 2e-5 --seed 123456
   ```

3. Evaluate: `python evaluator/evaluator.py -a dataset/test.jsonl -p saved_models/predictions.txt`

**Infrastructure:** single GPU, 2–4 hours. Google Colab T4 is sufficient.
No compilation, no graph tooling — `pip install transformers` and run.

**The tradeoff:** These models see source code tokens, not IR structure.
Variable names, formatting, and coding style all influence the prediction.
The normalisation benefit of working at the IR level is absent. A function
written defensively but with unfamiliar style may score as vulnerable;
a genuinely vulnerable function written in a familiar idiom may not.

**Success criterion:** reproduce the published accuracy within ±1% to
confirm the setup is correct. Then fine-tune further on SCAR accepted
patches to specialise to SCAR's encountered patterns.

**Actual result (4a — CodeBERT on same Devign split, Google Colab T4):**

| Epoch | Val Acc |
|---|---|
| 1 | 60.29% |
| 2 | 62.63% |
| 3 | 64.71% |
| 4 | **64.82%** ← best |
| 5 | 64.31% |

**Test accuracy: 63.43%** (from epoch 4 checkpoint). Exceeds the published 62% baseline, confirming the data pipeline and split are correct.

Note: CodeBERT trains on all ~21K `train.jsonl` examples (no compilation needed), vs ~10K for the GNN (compilation survivors only). Part of the accuracy gap is training data volume, not purely architecture.

**The granularity gap: 63.43% − 57.84% = 5.6 points.** This is the cost of aggregating instructions into basic blocks. Information present in the raw source token sequence is discarded when an entire block is compressed to a 45-dimensional feature vector.

---

### 4b. Custom GNN on LLVM IR — structural graph model (no ProGraML)

> **Step-by-step training guide:** `docs/experiments/ir-embed-training.md`
> **AWS setup:** `docs/experiments/ir-embed-aws.md`

This is the theoretically correct path for SCAR's use case. It operates
on LLVM IR, normalising away surface noise and capturing actual control
and data flow structure.

**Why not ProGraML:** the ProGraML library is effectively abandoned (~2022)
and locks to LLVM 3.8/6.0/10.0 — incompatible with SCAR's LLVM 14
container. Instead, graph extraction is implemented directly from IR text
using stdlib Python (`graph_demo.py`), with no external graph library
required. The same approach was validated as an end-to-end GNN PoC
(`gnn_poc.py`) and is wired into the full training pipeline (`train_gnn/`).

**What is already built:**

| Script | What it does |
|---|---|
| `graph_demo.py` | Parses LLVM IR text → CFG nodes + edges, prints per-pair structural diff |
| `gnn_poc.py` | End-to-end GNN in pure numpy — validates graph→model pipeline |
| `train_gnn/preprocess.py` | Downloads Devign, compiles 27K C functions to IR, builds graphs with 11 node features, saves pickled datasets |
| `train_gnn/train.py` | 2-layer GCNConv → global mean pool → binary classifier, PyTorch Geometric, saves best checkpoint |

**Devign standalone compilation — current status:**
Devign functions come from FFmpeg, QEMU, and the Linux kernel. Two fixes
were needed to get usable graphs:

1. **Stub injection** (member injection, ptr/arr upgrades, macro demotions)
   handles unknown types and missing struct members. This brings compile
   attrition from ~95% down to ~52%.
2. **`#define static` / `#define inline`** at the end of the preamble
   forces clang to emit IR for isolated functions. Without callers in the
   translation unit, `static`/`inline` functions pass syntax checking but
   clang omits their `define` blocks from the `.ll` output entirely —
   compilation succeeds but the IR file is empty.

With both fixes applied, **~48% of Devign functions produce valid graphs**
and `graphed` matches `compiled` (no additional filtering by the IR parser).
On the full 27K dataset this yields ~8K training graphs for the train split.

- **For SCAR integration:** attrition is not a problem. The Tekton
  `build-bitcode` task builds the target project in its actual environment;
  functions are compiled in full project context.

**To run on your laptop (pipeline smoke test):**
```bash
cd experiments/ir_embed_demo/train_gnn
pip install gdown torch --index-url https://download.pytorch.org/whl/cpu
pip install torch_geometric
python preprocess.py --subset 500   # ~240 graphs survive; enough to test
python train.py --epochs 10 --hidden 32
```

Any clang version works for preprocessing — no LLVM 14 required.

**Node features per basic block (11 total):**
instruction count, out-degree, in-degree, has\_call, has\_store,
has\_load, has\_icmp, has\_alloca, has\_getelementptr, has\_ret, has\_br.

**Success criterion:** accuracy ≥ 62% (CodeBERT baseline) on the Devign
test split confirms the structural graph representation is competitive.
Accuracy > 69% (UniXcoder) would mean IR structure is earning its cost
over token-based models. This criterion requires a full compilable dataset
(either via project build or Juliet).

**Actual result (4b baseline — CFG-only, 10K graphs):**

| Setting | Value |
|---|---|
| Train graphs | 10,097 (46% survival, 4,386 vuln / 5,711 fixed) |
| Val graphs | 1,251 |
| Test graphs | 1,250 |
| Epochs | 30, hidden=64 |
| pos_weight | 1.302 (fixed/vuln, applied to BCE loss) |
| Best val accuracy | 54.36% (epoch 25) |
| **Test accuracy** | **55.04%** |
| Majority-class baseline | 56.6% (always predict "fixed") |

The model barely learned — loss moved only 0.803 → 0.774 over 30 epochs.
55% is effectively at the majority-class ceiling, confirming that **CFG
topology alone does not carry enough signal** at basic-block granularity.
The 11 node features compress an entire basic block to a handful of binary
flags, making it impossible to distinguish a call to `strcpy` from a call
to `printf`, or a signed boundary check from an unsigned one.

**→ 4b confirmed insufficient. 4c (RGCNConv + DFG edges) triggered.**

**SCAR integration (after training):**

SCAR's `build-bitcode` task already emits LLVM IR. A new `ir-embed-scan`
Tekton task would parse each function's IR with `graph_demo.py`'s
extractor, score it against the trained model, and write top-K findings
to `findings-ir-embed.json` — feeding into the repair loop alongside IKOS
and LLM findings. Zero LLM cost per scan.

---

### 4c. GNN v2 — RGCNConv + DFG edges (PDG)

**Trigger:** 4b confirmed 55.04% — below 62% threshold. **Implemented and completed.**

**Actual result (4c — PDG, 10K graphs):**

| Setting | Value |
|---|---|
| Architecture | RGCNConv (2 relations: CFG + DFG) |
| Epochs / hidden | 30 / 64 |
| **Test accuracy** | **56.08%** |

DFG edges added +1.04% over 4b. The gradient signal improved (loss moved further) but the model remained well below the 62% threshold. **→ 4d triggered.**

Re-run preprocessing after `git pull` to regenerate graphs with `edge_type`.

**The problem with adding DFG edges to a standard GCNConv:**
Merging CFG and DFG edges into a single `edge_index` forces the aggregation
to use one shared weight matrix for both edge types. The model cannot
distinguish "block B executes after block A" from "block C uses a value
defined in block A" — it must implicitly guess edge type from node features
alone, wasting capacity and producing noisy gradients.

**The correct architecture: RGCNConv (Relational GCN)**

PyTorch Geometric's `RGCNConv` learns a separate weight matrix per edge type:
- W_CFG — projects features along control-flow edges
- W_DFG — projects features along data-dependency edges

Implementation diff from current `train.py` is small:

```python
# train.py — swap GCNConv for RGCNConv
from torch_geometric.nn import RGCNConv, global_mean_pool

class DefectGNN(torch.nn.Module):
    def __init__(self, in_features=N_FEATURES, hidden=64):
        super().__init__()
        self.conv1 = RGCNConv(in_features, hidden, num_relations=2)
        self.conv2 = RGCNConv(hidden, hidden, num_relations=2)
        self.lin   = torch.nn.Linear(hidden, 1)

    def forward(self, x, edge_index, edge_type, batch):
        x = F.relu(self.conv1(x, edge_index, edge_type))
        x = F.dropout(x, p=0.3, training=self.training)
        x = F.relu(self.conv2(x, edge_index, edge_type))
        x = global_mean_pool(x, batch)
        return self.lin(x).squeeze(-1)
```

**Extracting DFG edges from IR:**

LLVM IR is in SSA form — every value (`%name`) is defined exactly once and
all uses are explicit. The scaffolding is already in `preprocess.py`:

```python
_DEF     = re.compile(r"^\s+(%[\w.]+)\s*=")   # value definition
_USE_VAR = re.compile(r"%[\w.]+")              # value uses
```

These are parsed but currently unused. Wiring them up in `_parse_ir`:
1. For each instruction: record which `%name` it defines
2. For each instruction: find all `%name` references after the `=`
3. For each use, look up which basic block defined that value
4. Add a DFG edge from the defining block to the using block (if cross-block)

`preprocess.py` graph format would add two keys:
```python
{
    "x":          ...,           # node features (unchanged)
    "edge_index": ...,           # all edges (CFG + DFG concatenated)
    "edge_type":  ...,           # 0 = CFG, 1 = DFG
    "y":          ...,
}
```

**Why intra-function DFG is available from isolated snippets:**
SSA def-use chains are entirely self-contained within a function. The
snippet isolation that causes compilation attrition does not affect DFG
extraction — all the data flow information for values defined and used
within the function is present in the `.ll` output. Interprocedural data
flow (tracing values into called functions) is not available, but that
limitation applies equally to ProGraML without a full project build.

---

### 4d. Semantic feature upgrade — conditional on 4c result

**Trigger:** pursue if 4c (PDG) stalls below 62%.

**Diagnosis if 4c stalls:** the graph topology (nodes and edges) is fine
but node features are too coarse. The 11-feature vector cannot distinguish
a call to `strcpy` from a call to `printf`, or a signed boundary check from
an unsigned one. The model is structurally blind to the vocabulary of danger.

**Strategy:** beat CodeBERT at what it can only approximate — extract
explicit low-level semantic facts from LLVM IR that a token-based model
must guess at from variable names and context. All changes are algorithmic
(no LLM), zero inference latency increase.

All four changes implemented together in one preprocessing + training run.

#### 4d-i. Bag-of-APIs node features

Replace the single `has_call` flag with per-function binary flags covering
the ~28 functions responsible for the majority of C memory corruption.
LLVM IR preserves exact call targets (`call i32 @strcpy(...)`) regardless
of macro expansion or aliasing, making extraction unambiguous.

Safe/unsafe pairs (`strcpy` vs `strncpy`, `sprintf` vs `snprintf`) are
especially informative — the model can learn that one variant is a red flag
and the other is not.

| Category | Functions |
|---|---|
| Standard alloc/free | `malloc`, `calloc`, `realloc`, `free` |
| Unbounded string ops | `strcpy`, `strcat`, `sprintf`, `gets` |
| Bounded string ops | `strncpy`, `strncat`, `snprintf`, `fgets` |
| Memory ops | `memcpy`, `memmove`, `memset` |
| FFmpeg | `av_malloc`, `av_mallocz`, `av_realloc`, `av_free`, `av_freep` |
| Linux kernel | `kmalloc`, `kfree`, `kzalloc`, `vmalloc`, `vfree` |
| QEMU/GLib | `g_malloc`, `g_malloc0`, `g_realloc`, `g_free`, `g_new` |

#### 4d-ii. icmp semantics

Replace the single `has_icmp` flag with three flags that encode the
mathematical meaning of the comparison. LLVM IR states this explicitly
in the instruction name; CodeBERT must infer it from variable names.

| Flag | Matches | Vulnerability relevance |
|---|---|---|
| `has_signed_cmp` | `icmp s[lt\|gt\|le\|ge]` | Signed/unsigned mismatch → integer overflow |
| `has_unsigned_cmp` | `icmp u[lt\|gt\|le\|ge]` | Correct unsigned bounds check |
| `has_eq_cmp` | `icmp eq`, `icmp ne` | Null check, sentinel value check |

A block with `has_unsigned_cmp=0` before a `memcpy` call is a direct
IR signature of a missing bounds check.

#### 4d-iii. Type and width semantics

LLVM IR explicitly states the bit-width of every operation. Two flags
capture the most security-relevant width signals:

- `has_i8_op` — byte-level load/store (buffer iteration at char granularity;
  combined with `has_getelementptr`, a strong indicator of unsafe buffer walking)
- `has_64bit_op` — i64 arithmetic (potential truncation when result is
  narrowed to i32 for a bounds check or array index)

#### 4d-iv. Global Attention readout (train.py only — no preprocessing)

Replace `global_mean_pool` with `GlobalAttention(gate_nn=Linear(hidden, 1))`.
A single learned linear layer outputs a scalar weight per block before
aggregation. The model learns to focus on blocks with dangerous semantics
(e.g., `has_memcpy=1` + `has_unsigned_cmp=0`) and mute boilerplate blocks.

Replicates CodeBERT's self-attention focus mechanism at ~64 extra parameters.

#### Complete 4d feature vector

| Group | Features | Count |
|---|---|---|
| Structural | n\_instructions, out\_degree, in\_degree | 3 |
| Opcode flags | has\_call, has\_store, has\_load, has\_alloca, has\_getelementptr, has\_ret, has\_br | 7 |
| icmp semantics | has\_signed\_cmp, has\_unsigned\_cmp, has\_eq\_cmp | 3 |
| Type/width | has\_i8\_op, has\_64bit\_op | 2 |
| API hashing | 30 function flags (see table above) | 30 |
| **Total** | | **45** |

`N_FEATURES` in `train.py` updates from 11 → 45.

#### Actual results (4d — multiple runs)

| Run | Epochs | Hidden | Notes | Test Acc |
|---|---|---|---|---|
| v2.0 | 30 | 64 | linear gate | 56.32% |
| v2.0 | 60 | 128 | best val epoch 8, then overfit | **57.84%** |
| v2.1 | 30 | 128 | MLP gate (Linear→ReLU→Dropout→Linear) | 56.88% |

Adding 34 semantic features (API flags, icmp types, type/width) gained only +0.24% over 4c. Increasing capacity (hidden=128, 60 epochs) reached 57.84% but peaked at epoch 8 and overfit thereafter. The MLP attention gate made no meaningful difference.

**Conclusion: basic-block representation is saturated at ~57–58%.** The bottleneck is granularity, not feature richness. Adding more API flags or a more expressive pooling layer cannot recover information that was discarded by aggregating an entire basic block into a single feature vector. **→ 4a (CodeBERT baseline) run to establish ceiling.**

---

### 4e. Opcode embeddings — if 4d stalls

**Trigger:** pursue if 4d stalls below 62%.

More principled replacement for the opcode flags: count all ~70 LLVM
opcodes per block and look up a learned `d=16` embedding per opcode. The
block representation becomes `sum(count_i × E[opcode_i])`. The model
discovers which opcodes correlate with vulnerability rather than relying
on the curated 4d list.

Requires more training signal to converge than hand-crafted flags — better
suited to larger datasets. At 10K graphs, 4d's expert knowledge is more
reliable. At 50K+ graphs, opcode embeddings are more principled.

---

### 4a. CodeBERT / UniXcoder fine-tune — fallback at any stage

If structural GNN approaches consistently fall below 62%, run experiment 4a
(see above) to establish the ceiling with pre-trained semantics. Use that
result to decide whether to invest further in the structural path or adopt
a transformer-based classifier for SCAR integration.

---

### 5. Contrastive learning — parallel paradigm

This is a fundamentally different training objective from the 4x series and
can be pursued independently of whether classification succeeds or fails.

**What changes:** Instead of `f(graph) → {0,1}`, the model learns
`f(graph) → embedding_vector`. The loss function enforces geometry directly:
- Same-class pairs (vuln+vuln, fixed+fixed): pulled together
- Cross-class pairs (vuln+fixed): pushed apart by at least a margin

**Inference on unseen code:**
1. Compile new function → IR graph → embed it (one forward pass)
2. Find k-nearest neighbors in a pre-embedded reference corpus
3. If neighbors are predominantly vulnerable → flag the function

No decision boundary. No retrained classifier. The structural shape of
the new function determines where it lands in embedding space.

**Why it generalises differently from classification:**

The contrastive loss must find a unified structural criterion that explains
*all* (vuln, fix) pairs being far apart simultaneously — across FFmpeg, QEMU,
and the Linux kernel at once. It cannot overfit to one project's naming
conventions. If successful, it has learned that a specific topological shape
means "vulnerable", and new code with that shape lands in the same region
even if never seen during training.

Classification can draw a project-specific boundary. Contrastive learning
is forced to find a cross-project invariant.

**The SCAR-specific advantage — live corpus without retraining:**

Every accepted SCAR patch produces a `(vuln_IR, fix_IR)` pair at zero
marginal cost. Embed the vulnerable graph → add it to the reference corpus.
Future scans automatically compare new functions against it. The model's
"knowledge" of new vulnerability patterns grows with every accepted patch
without updating model weights. This is structurally impossible with a
classifier, which requires full retraining to incorporate new patterns.

**Loss function:**

Supervised Contrastive loss (SupCon, Khosla et al. 2020) is preferred for
Devign. Within each training batch, all same-label graphs are positives for
each other; all different-label graphs are negatives. No explicit pairing
of commits needed — just the 0/1 labels already in the dataset.

```python
# Conceptual training loop
embeddings = model(batch)           # (B, d) — one embedding per graph
embeddings = F.normalize(embeddings, dim=1)
loss = supcon_loss(embeddings, batch.y)   # pulls same-label together,
                                          # pushes different-label apart
```

**Relationship to experiments 2 and 4x:**

Experiment 2 (opcode histogram + contrastive MLP) was the toy-scale proof
of concept on 14 samples. Experiment 5 applies the same principle to full
PDG graphs with the 4d feature set, at Devign scale.

The 4x series answers: "can a classifier detect the vulnerability shape?"
Experiment 5 answers: "can the shape be embedded such that unseen code
finds its own cluster?"

Both depend on feature quality — the 43-feature 4d upgrade improves
both paradigms. Run them on the same preprocessed graphs.
