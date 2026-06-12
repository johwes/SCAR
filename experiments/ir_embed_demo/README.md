# IR Structural Embedding — Hello World

Minimal experiment to test whether LLVM IR opcode-frequency histograms
carry a structural signal that separates vulnerable from fixed code — before
any neural network or contrastive training.

See `docs/research.md` for the research framing.

---

## What it does

1. Compiles each `samples/*_vuln.c` and `samples/*_fixed.c` to LLVM IR
   using `clang -O0 -S -emit-llvm`.
2. Extracts a normalized opcode-frequency histogram from each `.ll` file
   (count of `br`, `icmp`, `store`, `load`, `call`, `getelementptr`, …).
3. Computes pairwise cosine distances across all files.
4. Prints the structural delta per pair and a full distance matrix.

No GPU, no pip installs, no dependencies beyond Python's standard library.

---

## Vulnerable sources

The `*_vuln.c` files are taken directly from
[johwes/SCAR-test-c](https://github.com/johwes/SCAR-test-c) — the
canonical synthetic target used to validate the SCAR pipeline.

| File | CWE | Bug |
|---|---|---|
| `bof_vuln.c` | CWE-121 | `strcpy` without bounds check |
| `divzero_vuln.c` | CWE-369 | Division by zero, no guard |
| `doublefree_vuln.c` | CWE-415 | Pointer freed twice |
| `nullderef_vuln.c` | CWE-476 | Unconditional NULL dereference |
| `oob_read_vuln.c` | CWE-125 | Array indexed beyond bounds |
| `signedoverflow_vuln.c` | CWE-190 | `INT_MAX + 1` |
| `uninit_vuln.c` | CWE-457 | Variable read before assignment |

The `*_fixed.c` files are the patched counterparts, derived from SCAR
accepted patches on the same target.

---

## Running

Requires `clang` (any version) and `python3`. Both are available inside
the `scar-agent` container.

```bash
cd experiments/ir_embed_demo
./run.sh
```

To use the canonical repo sources instead of the local copies:

```bash
./run.sh --from-repo
```

This clones `johwes/SCAR-test-c` and uses those files as the vulnerable
sources, with the local `*_fixed.c` files as their patched counterparts.

The `ir/` directory contains hand-written representative `.ll` files for
five pairs so `demo.py` can be run without `clang`:

```bash
python3 demo.py ir/
```

---

## Initial results (hand-written IR, 5 pairs)

```
avg vuln  ↔ vuln   distance : 0.3144
avg fixed ↔ fixed  distance : 0.4268
avg vuln  ↔ fixed  distance : 0.3787   (1.02× within-class — weak global signal)
```

The global ratio is weak because different vulnerability types (divzero vs
nullderef) are naturally far apart — they're different programs.

The per-pair signal is stronger:

| Pair | own vuln↔fixed | avg vuln↔other fixed | signal |
|---|---|---|---|
| divzero | 0.159 | 0.601 | YES |
| doublefree | 0.012 | 0.447 | YES |
| nullderef | 0.314 | 0.315 | YES (marginal) |
| oob_read | 0.368 | 0.358 | no |
| uninit | 0.106 | 0.400 | YES |

Key finding: `nullderef_V ↔ oob_read_V = 0.081` — both are
"missing-branch" vulnerabilities and cluster closer to each other than
either does to its own fixed version. Vulnerability class structure
survives in the histogram before any training.

---

## Next steps

1. Run `./run.sh` inside the container to replace hand-written IR with
   real clang output and add the remaining two pairs (bof, signedoverflow).
2. Add a contrastive training step: a two-layer network with margin loss
   on (vuln, fixed) pairs that learns which opcode dimensions are most
   discriminative.
3. Extend to function-level IR slices from SCAR's accepted patches on
   real targets (scarnet, zlib) to test generalization beyond toy examples.
