# Training the Defect-Detection GNN — Step-by-Step

Practical guide to go from zero to a trained GNN on the Devign dataset.
All scripts live in `experiments/ir_embed_demo/train_gnn/`.

---

## What you need

| Tool | Minimum version | Notes |
|---|---|---|
| Python | 3.10+ | |
| clang | any (14 recommended) | Must be on `PATH` |
| pip | recent | |
| ~3 GB disk | | dataset + IR files + model |

For laptop testing: CPU is fine — use `--subset` to keep runtimes short.
For full training: see the AWS section at the bottom.

---

## Directory layout

```
experiments/ir_embed_demo/train_gnn/
├── requirements.txt        ← Python dependencies
├── preprocess.py           ← download + compile + build graphs
├── train.py                ← train GNN, save checkpoint
└── data/                   ← created by preprocess.py (not in git)
    ├── devign.json         ← raw Devign download
    ├── train.jsonl         ← 80% split
    ├── valid.jsonl         ← 10% split
    ├── test.jsonl          ← 10% split
    ├── train_graphs.pkl    ← compiled + parsed graphs
    ├── valid_graphs.pkl
    └── test_graphs.pkl
```

---

## Step 1 — Install dependencies

```bash
cd experiments/ir_embed_demo/train_gnn

# CPU-only install (laptop):
pip install gdown
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install torch_geometric

# GPU install (AWS — replace cu126 with your CUDA version):
pip install gdown torch torch_geometric
pip install pyg_lib torch_scatter torch_sparse \
    -f https://data.pyg.org/whl/torch-$(python -c "import torch; print(torch.__version__)")+cu126.html
```

Verify:
```bash
python -c "import torch; import torch_geometric; print('ok')"
```

### Optional but strongly recommended: install project headers

Devign functions use FFmpeg and LibTIFF types extensively. Without the
real headers, `AVCodecContext`, `TIFF *`, etc. can't be resolved and
attrition is ~90-95%. With them, it drops to ~40-60%.

```bash
# Fedora / RHEL:
sudo dnf install ffmpeg-free-devel libtiff-devel

# Ubuntu / Debian:
sudo apt install libavcodec-dev libavutil-dev libavformat-dev libtiff-dev

# macOS (Homebrew):
brew install ffmpeg libtiff
```

`preprocess.py` auto-detects these headers and uses them automatically —
no flags needed. It prints which headers it found at startup.

---

## Step 2 — Download the Devign dataset

The dataset is hosted on Google Drive via the CodeXGLUE benchmark.
`preprocess.py` downloads it automatically using `gdown`:

```bash
python preprocess.py --subset 500   # laptop: 500 examples, takes ~2 min
python preprocess.py                # full: 27K examples, takes ~30-60 min
```

What it does:
1. Downloads `devign.json` (~50 MB) from Google Drive to `data/`
2. Splits 80/10/10 into `train.jsonl`, `valid.jsonl`, `test.jsonl`
3. For each C function: prepends common headers, calls `clang -O0 -S -emit-llvm`
4. Parses the LLVM IR to extract a CFG graph with node features
5. Saves pickled graph lists to `data/*_graphs.pkl`

**Expected compile attrition:** 40–70% of Devign functions reference
project-specific types or macros that the stub headers cannot cover (struct
member access on AVCodecContext, sk_buff, CPUState, etc. requires the
actual project headers). This is normal — the dataset is large enough to
absorb it, and the distribution of which functions succeed is close to
random with respect to the vulnerability label.

**Attrition varies by project:** Devign mixes FFmpeg, QEMU, Linux kernel,
and LibTIFF. FFmpeg codec functions have very high attrition (~90%) because
they access many AVCodecContext members. LibTIFF and simpler utility
functions compile at much higher rates. The `--subset` flag now uses a
random balanced sample across the full file rather than taking the first N
functions, so you see representative attrition rather than a worst-case
FFmpeg-only sample.

**Parallel workers:** default is 4. Increase on AWS:
```bash
python preprocess.py --workers 16
```

---

## Step 3 — What the graph looks like

Each basic block becomes a node with 11 features:

| Feature | Description |
|---|---|
| `n_instructions` | Number of IR instructions in this block |
| `out_degree` | Number of outgoing CFG edges |
| `in_degree` | Number of incoming CFG edges |
| `has_call` | 1 if block contains a function call |
| `has_store` | 1 if block writes to memory |
| `has_load` | 1 if block reads from memory |
| `has_icmp` | 1 if block contains a comparison |
| `has_alloca` | 1 if block allocates stack memory |
| `has_getelementptr` | 1 if block does pointer arithmetic |
| `has_ret` | 1 if block is a return block |
| `has_br` | 1 if block ends with a branch |

Edges connect basic blocks that can transfer control flow.
The GNN propagates these features through the graph before classifying.

---

## Step 4 — Train on your laptop (CPU)

```bash
python train.py --epochs 10 --hidden 32
```

With `--subset 500` from the preprocess step this runs in a few minutes
and is enough to confirm the pipeline works. Don't expect high accuracy
at this scale — you're checking the code runs cleanly.

Output:
```
Device: cpu
Loading graphs ...
  train=400  valid=50  test=50
  train class balance: 200 vuln / 200 fixed

Model: DefectGNN(in=11, hidden=32)  params=2,305

Epoch    Loss   Val Acc
----------------------------
    1  0.6821    53.00%
    5  0.6103    58.00%  ← best
   10  0.5814    61.00%  ← best
```

---

## Step 5 — Full training on AWS

See `docs/experiments/ir-embed-aws.md` for instance selection and setup.
Short version:

- **Instance:** `g4dn.xlarge` (~$0.53/hr, T4 GPU)
- **AMI:** Deep Learning OSS Nvidia Driver AMI (Ubuntu 22.04)
- **Storage:** 50 GB EBS

Once the instance is running:

```bash
# Copy scripts
scp -i your-key.pem -r experiments/ir_embed_demo/train_gnn \
    ubuntu@<ip>:~/train_gnn

ssh -i your-key.pem ubuntu@<ip>

# On the instance
conda activate pytorch
cd train_gnn
pip install gdown torch_geometric

python preprocess.py --workers 8      # ~45 min
python train.py --epochs 30           # ~2-3 hr on T4

# Copy checkpoint back
exit
scp -i your-key.pem ubuntu@<ip>:~/train_gnn/model.pt .

# Terminate immediately to stop billing
```

Full training hyperparameters:
```bash
python train.py \
    --epochs 30 \
    --hidden 64 \
    --lr 1e-3 \
    --batch-size 32 \
    --checkpoint model.pt
```

---

## Expected results

| Scale | Accuracy target | Notes |
|---|---|---|
| 500 samples, 10 epochs (laptop) | 55–62% | Sanity check only |
| Full Devign, 30 epochs (AWS) | 62–68% | On par with CodeBERT |

62% matches the published CodeBERT baseline on Devign. The GNN operates
on IR structure rather than source tokens, so with enough data it should
approach that range. Whether it exceeds UniXcoder (69.3%) is the
interesting research question — that would mean the structural
representation is earning its extra complexity.

---

## Troubleshooting

**`clang: command not found`**
Install clang: `sudo apt install clang` (Linux) or via Homebrew on Mac.
On the scar-agent container clang 14 is pre-installed.

**`gdown` fails / Google Drive quota exceeded**
Try again later, or download the file manually from:
`https://drive.google.com/file/d/1x6hoF7G-tSYxg8AFybggypLZgMGDNHfF`
and place it at `data/devign.json`.

**`torch_geometric` import error**
PyG version must match your PyTorch version. Check:
`python -c "import torch; print(torch.__version__)"` and reinstall PyG
against that version.

**Very high attrition (>80%) even with random sampling**
This is the fundamental ceiling of the standalone compilation approach.
Devign functions from FFmpeg, QEMU, and the Linux kernel access dozens of
struct members (`avctx->width`, `skb->data`, `cpu->env`, etc.) — the
actual project headers are required to resolve these. `no member named 'X'
in 'T'` errors cannot be fixed by stub injection without knowing the struct
layout, and there is no way to infer field types from a single function.

**Two paths forward:**

*Option A — Use the Devign source code directly (no compilation).*
The 4a experiment (CodeBERT/UniXcoder) trains on raw C source tokens and
achieves 62–69% on the Devign test set. No clang, no IR, no attrition.
Friction is minimal: `pip install transformers` and run the CodeXGLUE
`run.py` script. This is the right path for getting a trained classifier
on Devign.

*Option B — Build the projects with real headers.*
Clone FFmpeg, QEMU, Linux, and LibTIFF, build each with
`-flto -fembed-bitcode` to emit whole-program bitcode, then use
`llvm-extract --func=NAME` to pull per-function IR. This is the approach
ProGraML used. Attrition drops to near zero but setup takes several hours.

*Option C — Use a standalone-compilable dataset.*
The NIST Juliet Test Suite (~28K C/C++ cases, CWE-labeled, no external
headers required) compiles standalone. Attrition with the stub injector
will be <10%. Trade-off: synthetic code, limited generalization to
real-world vulnerability patterns.

For SCAR's own integration target (the `ir-embed-scan` Tekton task), the
attrition problem does not exist — the full build system is available and
functions are compiled in their proper project context.

**Training loss stuck at ~0.69 (log(2))**
The model is predicting 50/50. Try:
- More epochs
- Lower learning rate (`--lr 1e-4`)
- Larger hidden dimension (`--hidden 128`)
