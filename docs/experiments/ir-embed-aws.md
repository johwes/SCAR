# Training the ProGraML Defect Detector on AWS

Practical setup guide for experiment 4b: train a GNN over ProGraML
CFG/DFG graphs on the Devign dataset, using a rented AWS GPU instance.

---

## Instance selection

| Instance | GPU | VRAM | vCPUs | RAM | On-demand price |
|---|---|---|---|---|---|
| `g4dn.xlarge` | T4 | 16 GB | 4 | 16 GB | ~$0.53/hr |
| `g5.xlarge` | A10G | 24 GB | 4 | 16 GB | ~$1.01/hr |
| `p3.2xlarge` | V100 | 16 GB | 8 | 61 GB | ~$3.06/hr |

**Recommended:** `g4dn.xlarge` for a first run. 27K training samples fit
comfortably on a T4. Budget ~6 hours of instance time: 1–2 hours for
preprocessing, 3–4 hours training. Total cost: ~$3–4.

Use a **Spot Instance** to cut the price by 60–70% if you can tolerate
interruption. For a one-shot training job, spot is usually fine — save a
checkpoint every epoch so you can resume if interrupted.

---

## Launch

1. In the EC2 console, choose AMI: **Deep Learning OSS Nvidia Driver AMI
   (Ubuntu 22.04)** — search "Deep Learning" in the AMI catalog. This ships
   with CUDA 12.x, PyTorch, and NVIDIA drivers pre-installed. No manual
   CUDA setup needed.

2. Storage: add a 50 GB EBS volume (the default 8 GB is too small for
   the dataset + IR files + model checkpoints).

3. Security group: allow inbound SSH (port 22) from your IP only.

4. After launch, SSH in:
   ```bash
   ssh -i your-key.pem ubuntu@<instance-public-ip>
   ```

---

## Environment setup

```bash
# Activate the PyTorch conda environment that ships with the DLAMI
conda activate pytorch

# Verify GPU is visible
python -c "import torch; print(torch.cuda.get_device_name(0))"

# Install ProGraML
pip install programl

# Install PyTorch Geometric
# Match the CUDA version shown by: python -c "import torch; print(torch.version.cuda)"
TORCH=$(python -c "import torch; print(torch.__version__)")
CUDA=$(python -c "import torch; print('cu' + torch.version.cuda.replace('.',''))")
pip install torch_geometric
pip install pyg_lib torch_scatter torch_sparse \
    -f https://data.pyg.org/whl/torch-${TORCH}+${CUDA}.html

# Dataset download tool
pip install gdown
```

> **LLVM version note:** ProGraML's C/C++ backend (`programl.from_cpp()`)
> bundles its own LLVM 10 toolchain, so you do not need to install a
> specific LLVM version separately. Use `from_cpp()` rather than
> `from_llvm_ir()` + external clang for Devign source functions. The
> `from_llvm_ir()` path requires LLVM 3.8, 6.0, or 10.0 — not the modern
> LLVM 14+ that apt installs today.

---

## Dataset

```bash
mkdir ~/devign && cd ~/devign

# Download raw Devign JSON (~50 MB)
gdown https://drive.google.com/uc?id=1x6hoF7G-tSYxg8AFybggypLZgMGDNHfF

# Clone only the preprocess script (shallow, no large files)
git clone --depth=1 --filter=blob:none --sparse \
    https://github.com/microsoft/CodeXGLUE.git cxg
cd cxg
git sparse-checkout set Code-Code/Defect-detection/dataset
cp Code-Code/Defect-detection/dataset/preprocess.py ~/devign/
cd ~/devign

python preprocess.py
# Produces train.jsonl (21,854), valid.jsonl (2,732), test.jsonl (2,732)
```

Each line in the `.jsonl` files:
```json
{"func": "int foo(...) { ... }", "target": 1, "idx": 42}
```

---

## Graph construction

ProGraML converts each C function to a `ProgramGraph` protocol buffer
containing control-flow, data-flow, and call-flow edges.

```python
import json
import programl
from pathlib import Path

def build_graphs(jsonl_path, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    skipped = 0
    with open(jsonl_path) as f:
        for line in f:
            item = json.loads(line)
            try:
                # from_cpp handles LLVM toolchain internally — no external clang needed
                graph = programl.from_cpp(item["func"])
                programl.save_graphs(out_dir / f"{item['idx']}.graph", [graph])
            except Exception:
                skipped += 1  # ~20% of functions fail (missing types, macros, etc.)
    print(f"Skipped {skipped} functions that failed to parse")

build_graphs("train.jsonl", "graphs/train")
build_graphs("valid.jsonl",  "graphs/valid")
build_graphs("test.jsonl",   "graphs/test")
```

Expect ~1–2 hours for the full 27K functions on a 4-vCPU instance.
Parallelise with `multiprocessing.Pool` to use all cores.

---

## Training

ProGraML graphs → PyTorch Geometric `Data` objects → GNN classifier.

```python
import torch
import programl
import networkx as nx
from torch_geometric.data import Data, DataLoader
from torch_geometric.nn import GCNConv, global_mean_pool
import torch.nn.functional as F
from pathlib import Path
import json

# --- Convert a saved ProGraML graph to a PyG Data object ---
def graph_to_pyg(graph_path, label):
    graphs = programl.load_graphs(graph_path)
    g = programl.to_networkx(graphs[0])
    # Node features: one-hot over node type (instruction, variable, constant)
    node_types = [d.get("type", 0) for _, d in g.nodes(data=True)]
    x = torch.tensor(node_types, dtype=torch.float).unsqueeze(1)
    edges = list(g.edges())
    if not edges:
        return None
    edge_index = torch.tensor(edges, dtype=torch.long).t().contiguous()
    return Data(x=x, edge_index=edge_index, y=torch.tensor([label]))

# --- Simple GNN classifier ---
class DefectGNN(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = GCNConv(1, 64)
        self.conv2 = GCNConv(64, 64)
        self.lin   = torch.nn.Linear(64, 2)

    def forward(self, data):
        x, edge_index, batch = data.x, data.edge_index, data.batch
        x = F.relu(self.conv1(x, edge_index))
        x = F.relu(self.conv2(x, edge_index))
        x = global_mean_pool(x, batch)
        return self.conv1(x)  # logits

# --- Training loop ---
device = torch.device("cuda")
model  = DefectGNN().to(device)
opt    = torch.optim.Adam(model.parameters(), lr=1e-3)

for epoch in range(20):
    model.train()
    for batch in train_loader:
        batch = batch.to(device)
        opt.zero_grad()
        out  = model(batch)
        loss = F.cross_entropy(out, batch.y)
        loss.backward()
        opt.step()
    # Save checkpoint every epoch so spot interruption is recoverable
    torch.save(model.state_dict(), f"checkpoint_epoch{epoch}.pt")
```

This is a minimal skeleton. A real training script adds validation accuracy
logging, early stopping, and a larger node feature space (Inst2Vec embeddings
rather than a single type integer).

---

## Retrieving the trained model

Copy the final checkpoint back before terminating the instance:

```bash
# From your local machine
scp -i your-key.pem ubuntu@<ip>:~/devign/checkpoint_epoch19.pt ./

# Or push to S3
aws s3 cp ~/devign/checkpoint_epoch19.pt s3://your-bucket/scar-models/
```

Terminate the instance immediately after — there is no reason to leave it
running once the checkpoint is saved.

---

## ProGraML maintenance caveat

The ProGraML repository has not been actively updated since ~2022. It works,
but check GitHub issues for compatibility problems with recent Python or
system library versions before investing time in the full pipeline. If
`programl.from_cpp()` proves unstable, the fallback is to install LLVM 10
via apt (`clang-10`) and use `programl.from_llvm_ir()` with IR compiled by
that specific version.

If ProGraML turns out to be too brittle, experiment 4a (CodeBERT/UniXcoder)
requires no C compilation toolchain and is the safer first bet.

---

## Estimated total cost

| Step | Time | Instance cost |
|---|---|---|
| Environment setup | 20 min | ~$0.18 |
| Graph preprocessing (27K functions) | 90 min | ~$0.80 |
| Training (20 epochs) | 3–4 hr | ~$1.60–2.10 |
| **Total** | **~5–6 hr** | **~$3–4** |

Storage (50 GB EBS for the session): ~$0.20. Negligible.
