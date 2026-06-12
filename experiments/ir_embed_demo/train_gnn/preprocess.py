#!/usr/bin/env python3
"""
preprocess.py — Download Devign, compile to LLVM IR, build CFG graphs.

Run once before training. Outputs pickled graph lists under data/.

Usage:
    python preprocess.py                   # full dataset (~27K functions)
    python preprocess.py --subset 1000     # quick laptop test
    python preprocess.py --workers 8       # parallel compilation (default: 4)
"""

import argparse
import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

HERE    = Path(__file__).parent
DATA    = HERE / "data"
DEVIGN_ID = "1x6hoF7G-tSYxg8AFybggypLZgMGDNHfF"   # Google Drive file ID

# Base preamble: standard headers + common primitive typedefs
PREAMBLE = """\
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <limits.h>
#include <assert.h>
#include <stdarg.h>
#include <errno.h>
#include <sys/types.h>

/* suppress GCC/clang extensions that confuse a plain compile */
#define __attribute__(x)
#define __extension__
#define __inline__ inline
#define __volatile__ volatile
#define __asm__(x)
#define __builtin_expect(x,y) (x)
#define likely(x)   (x)
#define unlikely(x) (x)
#define __must_check
#define __user
#define __iomem
#define __force
#define __rcu
#define __percpu
#define __init
#define __exit
#define noinline
#define __always_inline inline
#define EXPORT_SYMBOL(x)
#define EXPORT_SYMBOL_GPL(x)
#define MODULE_LICENSE(x)
#define MODULE_AUTHOR(x)
#define MODULE_DESCRIPTION(x)
#define BUG()           ((void)0)
#define BUG_ON(x)       ((void)(x))
#define WARN_ON(x)      ((void)(x))
#define BUILD_BUG_ON(x) ((void)(x))
#define container_of(ptr,type,member) ((type*)((char*)(ptr) - offsetof(type,member)))
#define ARRAY_SIZE(x)   (sizeof(x)/sizeof((x)[0]))
#define min(a,b)        ((a)<(b)?(a):(b))
#define max(a,b)        ((a)>(b)?(a):(b))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))

typedef unsigned int        uint;
typedef unsigned long       ulong;
typedef unsigned char       uchar;
typedef long long           s64;
typedef unsigned long long  u64;
typedef unsigned int        u32;
typedef unsigned short      u16;
typedef unsigned char       u8;
typedef signed char         s8;
typedef int                 s32;
typedef short               s16;
typedef u64                 __u64;
typedef u32                 __u32;
typedef u16                 __u16;
typedef u8                  __u8;
typedef s64                 __s64;
typedef s32                 __s32;
typedef unsigned long       size_t;
typedef long                ssize_t;
typedef long                off_t;
typedef int                 pid_t;
typedef unsigned int        uid_t;
typedef unsigned int        gid_t;
typedef unsigned int        mode_t;
typedef unsigned long       ino_t;
typedef int                 dev_t;
"""

# Regexes to detect fixable clang errors and extract the symbol name
_ERR_UNKNOWN_TYPE  = re.compile(r"error: unknown type name '(\w+)'")
_ERR_UNDECL_IDENT  = re.compile(r"error: use of undeclared identifier '(\w+)'")
_ERR_IMPLICIT_FUNC = re.compile(r"warning: implicit declaration of function '(\w+)'")
_ERR_INCOMPLETE    = re.compile(r"error: incomplete definition of type '(?:struct|union|enum) (\w+)'")

# ---------------------------------------------------------------------------
# Step 1 — Download + split Devign
# ---------------------------------------------------------------------------

def download_devign():
    DATA.mkdir(parents=True, exist_ok=True)
    raw = DATA / "devign.json"
    if raw.exists():
        print(f"  {raw} already exists, skipping download.")
    else:
        print("  Downloading Devign from Google Drive (~50 MB)...")
        subprocess.run(
            [sys.executable, "-m", "gdown", DEVIGN_ID, "-O", str(raw)],
            check=True
        )

    # Split 80 / 10 / 10
    print("  Splitting train / valid / test ...")
    with open(raw) as f:
        rows = json.load(f)

    n = len(rows)
    splits = {
        "train": rows[:int(n * 0.8)],
        "valid": rows[int(n * 0.8):int(n * 0.9)],
        "test":  rows[int(n * 0.9):],
    }
    for name, subset in splits.items():
        out = DATA / f"{name}.jsonl"
        with open(out, "w") as f:
            for item in subset:
                f.write(json.dumps({"func": item["func"],
                                    "target": item["target"],
                                    "idx": item.get("idx", 0)}) + "\n")
        print(f"    {name}: {len(subset)} examples → {out}")


# ---------------------------------------------------------------------------
# Step 2 — Compile C function to LLVM IR (with automatic stub injection)
# ---------------------------------------------------------------------------

def _try_compile(full_source: str) -> tuple[str | None, str]:
    """Single compilation attempt. Returns (ir_text_or_None, stderr)."""
    src_path = ir_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
            f.write(full_source)
            src_path = Path(f.name)
        ir_path = src_path.with_suffix(".ll")
        result = subprocess.run(
            ["clang", "-O0", "-S", "-emit-llvm",
             "-Wno-everything", "-ferror-limit=0",
             "-o", str(ir_path), str(src_path)],
            capture_output=True, timeout=15,
        )
        stderr = result.stderr.decode(errors="replace")
        if result.returncode == 0 and ir_path.exists():
            return ir_path.read_text(errors="replace"), stderr
        return None, stderr
    except Exception:
        return None, ""
    finally:
        if src_path:
            src_path.unlink(missing_ok=True)
        if ir_path:
            ir_path.unlink(missing_ok=True)


def compile_to_ir(func_source: str, max_retries: int = 4) -> str | None:
    """
    Compile one C function string to LLVM IR.

    On failure, parses clang's stderr for unknown types and undeclared
    identifiers, injects forward declarations, and retries up to
    max_retries times. Handles the majority of Devign functions that
    use project-specific types (AVCodecContext, kmem_cache, etc.).
    """
    preamble = PREAMBLE
    seen_stubs: set[str] = set()

    for attempt in range(max_retries):
        ir, stderr = _try_compile(preamble + "\n" + func_source)
        if ir is not None:
            return ir

        new_stubs: list[str] = []

        for line in stderr.splitlines():
            # Unknown type name  →  typedef void* TypeName;
            m = _ERR_UNKNOWN_TYPE.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs:
                    new_stubs.append(f"typedef void* {t};")
                    seen_stubs.add(t)

            # Undeclared identifier  →  int identifier = 0; (as a global)
            m = _ERR_UNDECL_IDENT.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs:
                    new_stubs.append(f"static int {t} = 0;")
                    seen_stubs.add(t)

            # Implicit function declaration  →  extern void* fn();
            m = _ERR_IMPLICIT_FUNC.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs:
                    new_stubs.append(f"extern void* {t}();")
                    seen_stubs.add(t)

            # Incomplete struct/union  →  add empty definition
            m = _ERR_INCOMPLETE.search(line)
            if m:
                t = m.group(1)
                stub = f"struct {t}"
                if stub not in seen_stubs:
                    new_stubs.append(f"struct {t} {{}};")
                    seen_stubs.add(stub)

        if not new_stubs:
            return None   # error type we can't auto-fix
        preamble += "\n" + "\n".join(new_stubs)

    return None


# ---------------------------------------------------------------------------
# Step 3 — IR text → graph with node features
# ---------------------------------------------------------------------------

_BB_LABEL  = re.compile(r"^([\w.]+):")
_DEF       = re.compile(r"^\s+(%[\w.]+)\s*=")
_USE_VAR   = re.compile(r"%[\w.]+")
_BR_COND   = re.compile(r"br i1 .+?label %(\w+).+?label %(\w+)")
_BR_UNCOND = re.compile(r"br label %(\w+)")

# Opcodes we track as per-block binary features
_TRACKED_OPS = ["call", "store", "load", "icmp", "alloca",
                 "getelementptr", "ret", "br"]


def _parse_ir(ir_text: str) -> list[dict]:
    """
    Parse the first function in an IR file.
    Returns a list of basic-block dicts:
      { name, lines, successors, predecessors, defs, uses }
    """
    in_func   = False
    blocks    = []
    current   = None

    for line in ir_text.splitlines():
        if re.match(r"^define\b", line):
            in_func  = True
            current  = {"name": "entry", "lines": [], "successors": []}
            blocks   = [current]
            continue

        if not in_func:
            continue

        if line.strip() == "}":
            break

        m = _BB_LABEL.match(line)
        if m:
            current = {"name": m.group(1), "lines": [], "successors": []}
            blocks.append(current)
            continue

        if current is None:
            continue

        current["lines"].append(line)

        m = _BR_COND.search(line)
        if m:
            current["successors"] += [m.group(1), m.group(2)]
        else:
            m = _BR_UNCOND.search(line)
            if m:
                current["successors"].append(m.group(1))

    return blocks


def ir_to_graph(ir_text: str) -> dict | None:
    """
    Convert LLVM IR text to a graph dict:
      x          : (n_nodes, n_features) float32
      edge_index : (2, n_edges) int64  — CFG edges
    Returns None if the IR has fewer than 2 nodes (trivial function).
    """
    blocks = _parse_ir(ir_text)
    if len(blocks) < 1:
        return None

    name_to_idx = {b["name"]: i for i, b in enumerate(blocks)}
    n = len(blocks)

    # CFG edges
    src_list, dst_list = [], []
    in_degree = defaultdict(int)
    for b in blocks:
        for succ in b["successors"]:
            if succ in name_to_idx:
                si = name_to_idx[b["name"]]
                di = name_to_idx[succ]
                src_list.append(si)
                dst_list.append(di)
                in_degree[di] += 1

    # Node features per basic block
    #   [n_instructions, out_degree, in_degree,
    #    has_call, has_store, has_load, has_icmp,
    #    has_alloca, has_getelementptr, has_ret, has_br]
    features = []
    for i, b in enumerate(blocks):
        text      = " ".join(b["lines"])
        n_instr   = len([l for l in b["lines"] if l.strip()])
        out_deg   = len(b["successors"])
        in_deg    = in_degree[i]
        op_flags  = [1.0 if op in text else 0.0 for op in _TRACKED_OPS]
        features.append([float(n_instr), float(out_deg), float(in_deg)] + op_flags)

    x          = np.array(features,  dtype=np.float32)
    edge_index = np.array([src_list, dst_list], dtype=np.int64) if src_list \
                 else np.zeros((2, 0), dtype=np.int64)

    return {"x": x, "edge_index": edge_index}


# ---------------------------------------------------------------------------
# Step 4 — Process one item (called in parallel)
# ---------------------------------------------------------------------------

def process_item(item: dict) -> dict | None:
    ir = compile_to_ir(item["func"])
    if ir is None:
        return None
    g = ir_to_graph(ir)
    if g is None:
        return None
    g["y"]   = int(item["target"])
    g["idx"] = item.get("idx", 0)
    return g


def process_split(jsonl_path: Path, subset: int | None, workers: int) -> list[dict]:
    with open(jsonl_path) as f:
        items = [json.loads(l) for l in f]

    if subset:
        # Keep class balance in the subset
        vuln  = [x for x in items if x["target"] == 1][:subset // 2]
        fixed = [x for x in items if x["target"] == 0][:subset // 2]
        items = vuln + fixed

    graphs, ok, fail = [], 0, 0
    print(f"  Processing {len(items)} functions with {workers} workers ...")

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(process_item, it): it for it in items}
        for i, fut in enumerate(as_completed(futs), 1):
            g = fut.result()
            if g:
                graphs.append(g)
                ok += 1
            else:
                fail += 1
            if i % 500 == 0:
                print(f"    {i}/{len(items)}  ok={ok}  failed={fail}")

    print(f"  Done: {ok} graphs built, {fail} functions failed to compile "
          f"({fail/len(items)*100:.0f}% attrition)")
    return graphs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset",  type=int, default=None,
                    help="Use only N examples per split (laptop test)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel compilation workers")
    ap.add_argument("--skip-download", action="store_true",
                    help="Skip download if data/*.jsonl already exist")
    args = ap.parse_args()

    if not args.skip_download:
        print("\n── Download ─────────────────────────────────────────────")
        download_devign()

    for split in ["train", "valid", "test"]:
        src = DATA / f"{split}.jsonl"
        dst = DATA / f"{split}_graphs.pkl"
        if not src.exists():
            print(f"Missing {src} — run without --skip-download first.")
            sys.exit(1)
        print(f"\n── {split} ───────────────────────────────────────────────")
        graphs = process_split(src, subset=args.subset, workers=args.workers)
        with open(dst, "wb") as f:
            pickle.dump(graphs, f)
        print(f"  Saved {len(graphs)} graphs → {dst}")

    print("\nDone. Run train.py next.\n")


if __name__ == "__main__":
    main()
