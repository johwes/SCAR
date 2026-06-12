# SCAR — Research Directions

Ideas that are plausible and worth thinking about, but far enough from
current practice that they belong in a lab notebook rather than a roadmap.
None of these are implementation tasks. They are starting points for
research conversations.

---

## Contrastive structural embeddings over LLVM IR

**The idea**

Build an embedding model that learns to place vulnerable and fixed versions
of the same code *far apart* in vector space, using LLVM IR as input rather
than source text. At inference time, embed each function's IR and measure
distance to the vulnerable cluster. High proximity to the cluster is a
finding signal.

**Why IR, not source**

Source code embeddings are noisy: variable names, comments, formatting, and
syntactic idioms all vary between codebases while the underlying computation
stays the same. LLVM IR normalises all of that away. A bounds-check is a
bounds-check regardless of whether it was written by a C programmer who
prefers early-return guards or one who prefers nested conditionals.

**Why structural, not semantic**

Standard semantic embeddings capture what the code *says*. The signal for
vulnerability detection is often what the code *doesn't say* — a missing
conditional branch, a missing data-dependency edge between an input and a
bounds check. A structural embedding over the control-flow graph (CFG) and
data-flow graph (DFG) captures the *topology* of the code. A function
missing a bounds check has a structurally different CFG from a function that
has one: fewer nodes, one fewer conditional branch, one fewer comparison
edge. That topological difference survives embedding; it does not survive
token averaging.

**The training objective**

Supervised contrastive learning with (vulnerable IR, fixed IR) pairs as the
(anchor, negative) class. The loss explicitly pushes fixed code away from
vulnerable code in the embedding space, rather than trying to maximise cosine
similarity to semantically related code.

**Relevant prior work**

- **ProGraML** (Cummins et al., 2020) — graph-based representations over
  LLVM IR combining control-flow, data-flow, and call edges. Demonstrated
  that GNNs over this representation substantially outperform token-based and
  AST-based approaches on program analysis tasks. The LLVM bitcode artifact
  SCAR already produces from `build-bitcode` is the direct input.
- **Inst2Vec** (Ben-Nun et al., 2018) — statement-level embeddings of LLVM IR
  using co-occurrence statistics, used as node features in ProGraML.
- **VulChecker** — applies GNNs over program dependence graphs for
  vulnerability detection; closest to what is described here.

**Natural integration point in SCAR**

The `build-bitcode` task already emits the LLVM bitcode. A structural scanner
task would consume it directly — no extra compilation step — embed each
function, score against the vulnerable cluster, and write the top-K findings
to `findings-ir-embed.json`. It runs in seconds, produces zero LLM cost, and
feeds the repair loop as any other findings source does.

**The self-improving property**

Every accepted patch SCAR produces is a (vulnerable IR, fixed IR) pair. The
corpus grows with every pipeline run. An embedding model retrained
periodically on accumulated pairs becomes progressively more accurate on the
patterns SCAR encounters. This is the part worth getting excited about: the
repair pipeline and the detection model improve together.

**Experimental results:** `docs/experiments/ir-embed.md`

**Open questions**

- *Granularity*: whole-function embeddings may be too coarse. A missing check
  deep in a large function is a small subgraph of the total CFG. Basic-block-
  level or sliding-window subgraph embeddings with attention pooling may be
  necessary for precision.
- *Training data bootstrap*: SCAR's existing accepted patches are dozens, not
  thousands. Public datasets (BigVul, PatchDB, CVEfixes) at source level exist;
  compiling them to IR at scale requires a build infrastructure investment.
- *Novel classes*: the model detects code structurally similar to what it was
  trained on. Vulnerability classes absent from the training corpus will not be
  found — same fundamental limitation as any supervised detector.
- *Embedding model choice*: GNNs over the full CFG/DFG (ProGraML-style) are
  the principled choice. Simpler alternatives — sequence models over linearised
  IR, or GGNN with Inst2Vec node features — trade some expressiveness for
  dramatically lower training cost and may be the right starting point.
