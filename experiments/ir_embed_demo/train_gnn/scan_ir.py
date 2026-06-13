#!/usr/bin/env python3
"""
scan_ir.py — Run the trained GNN on a compiled LLVM IR file.

Usage:
    python scan_ir.py function.ll
    python scan_ir.py function.ll --threshold 0.4
    python scan_ir.py function.ll --model path/to/model.pt
"""

import argparse
import sys
import torch
import torch.nn.functional as F
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from preprocess import ir_to_graph
from train import DefectGNN, N_FEATURES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ir_file", help=".ll file to scan")
    ap.add_argument("--model",     default=str(HERE / "model.pt"))
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    ir_text = Path(args.ir_file).read_text(errors="replace")
    g = ir_to_graph(ir_text)
    if g is None:
        print("ERROR: could not parse IR into a graph (no basic blocks found)")
        sys.exit(1)

    x          = torch.tensor(g["x"],          dtype=torch.float)
    edge_index = torch.tensor(g["edge_index"],  dtype=torch.long)
    edge_type  = torch.tensor(g["edge_type"],   dtype=torch.long)
    if x.shape[0] > 1:
        x = (x - x.mean(0)) / (x.std(0) + 1e-8)
    batch = torch.zeros(x.shape[0], dtype=torch.long)

    model = DefectGNN(N_FEATURES)
    model.load_state_dict(torch.load(args.model, map_location="cpu", weights_only=True))
    model.eval()

    with torch.no_grad():
        prob = torch.sigmoid(model(x, edge_index, edge_type, batch)).item()

    label = "VULNERABLE" if prob >= args.threshold else "safe"
    blocks = x.shape[0]
    print(f"{Path(args.ir_file).name}  [{blocks} blocks]  {prob:.1%}  →  {label}")


if __name__ == "__main__":
    main()
