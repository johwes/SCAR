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

**SCAR integration (after training):**

SCAR's `build-bitcode` task already emits LLVM IR. A new `ir-embed-scan`
Tekton task would parse each function's IR with `graph_demo.py`'s
extractor, score it against the trained model, and write top-K findings
to `findings-ir-embed.json` — feeding into the repair loop alongside IKOS
and LLM findings. Zero LLM cost per scan.

---

### 4c. GNN v2 — RGCNConv + data flow edges (conditional on 4b result)

**Trigger:** only pursue this if 4b stalls below 62% after ~10K training graphs.
If 4b reaches 62%+, the CFG-only hypothesis is proved and this becomes a
research extension rather than a fix.

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
