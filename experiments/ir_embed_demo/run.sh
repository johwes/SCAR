#!/bin/bash
# Compile sample C files to LLVM IR and run the structural embedding demo.
#
# Requirements: clang (any version), python3
# No GPU, no pip installs, no dependencies beyond the standard library.
#
# Usage:
#   cd experiments/ir_embed_demo
#   ./run.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SAMPLES_DIR="$SCRIPT_DIR/samples"
IR_DIR="$SCRIPT_DIR/ir"

if ! command -v clang &>/dev/null; then
    echo "error: clang not found — install clang or run inside the scar-agent container"
    exit 1
fi

mkdir -p "$IR_DIR"

echo "Compiling samples to LLVM IR..."
for src in "$SAMPLES_DIR"/*.c; do
    name="$(basename "$src" .c)"
    ll="$IR_DIR/$name.ll"
    # -O0: no optimisations — keep the IR close to the source structure
    # -S -emit-llvm: human-readable IR text format (.ll)
    clang -O0 -S -emit-llvm -Wno-everything -o "$ll" "$src" 2>/dev/null
    echo "  $name.c → $name.ll"
done

echo ""
python3 "$SCRIPT_DIR/demo.py" "$IR_DIR"
