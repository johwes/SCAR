#!/usr/bin/env python3
"""
preprocess.py — Download Devign, compile to LLVM IR, build CFG graphs.

Run once before training. Outputs pickled graph lists under data/.

Usage:
    python preprocess.py                   # full dataset (~27K functions)
    python preprocess.py --subset 1000     # quick laptop test (random balanced sample)
    python preprocess.py --workers 8       # parallel compilation (default: 4)
    python preprocess.py --seed 0          # different random subset sample
"""

import argparse
import json
import os
import pickle
import random
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

# ---------------------------------------------------------------------------
# Preamble construction
#
# The static section covers standard C + common kernel macros + primitive
# typedefs.  If project dev headers are installed (FFmpeg, LibTIFF, QEMU)
# we prepend them so that project-specific types are fully resolved by the
# real headers rather than our stub injector.  This alone drops attrition
# on Devign from ~95% to ~40-60%.
#
# Install on Fedora:   sudo dnf install ffmpeg-free-devel libtiff-devel
# Install on Ubuntu:   sudo apt install libavcodec-dev libtiff-dev
# ---------------------------------------------------------------------------

# Project headers to include when present — ordered by impact on Devign.
# FFmpeg covers ~40% of functions; LibTIFF ~10%; QEMU needs internal
# headers not shipped in distro packages so we skip it.
_PROJECT_HEADER_CANDIDATES: list[tuple[str, str]] = [
    # FFmpeg — avcodec.h pulls avutil, so check all individually
    ("/usr/include/libavcodec/avcodec.h",   "#include <libavcodec/avcodec.h>"),
    ("/usr/include/libavutil/avutil.h",     "#include <libavutil/avutil.h>"),
    ("/usr/include/libavformat/avformat.h", "#include <libavformat/avformat.h>"),
    ("/usr/include/libavfilter/avfilter.h", "#include <libavfilter/avfilter.h>"),
    ("/usr/include/libswscale/swscale.h",   "#include <libswscale/swscale.h>"),
    # LibTIFF
    ("/usr/include/tiff.h",                 "#include <tiff.h>"),
    ("/usr/include/tiffio.h",               "#include <tiffio.h>"),
    # GLib — covers some QEMU utility functions
    ("/usr/include/glib-2.0/glib.h",        "#include <glib.h>"),
]

def _detect_project_headers() -> list[str]:
    """Return include lines for project dev headers found on this system."""
    return [inc for path, inc in _PROJECT_HEADER_CANDIDATES if Path(path).exists()]

# Project headers go BEFORE the __attribute__ suppressor so they compile
# cleanly, then our macros handle the remaining kernel extensions.
_PROJECT_INCLUDES = _detect_project_headers()

_PREAMBLE_STATIC = """\
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

/* suppress GCC/clang extensions */
#define __attribute__(x)
#define __extension__
#define __inline__      inline
#define __volatile__    volatile
#define __asm__(x)
#define __builtin_expect(x,y) (x)
#define likely(x)       (x)
#define unlikely(x)     (x)
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
#define __packed
#define __aligned(x)
#define __printf(a,b)
#define EXPORT_SYMBOL(x)
#define EXPORT_SYMBOL_GPL(x)
#define MODULE_LICENSE(x)
#define MODULE_AUTHOR(x)
#define MODULE_DESCRIPTION(x)

/* common kernel/FFmpeg macros */
#define BUG()               ((void)0)
#define BUG_ON(x)           ((void)(x))
#define WARN_ON(x)          ((void)(x))
#define WARN_ON_ONCE(x)     ((void)(x))
#define BUILD_BUG_ON(x)     ((void)(x))
#define ARRAY_SIZE(x)       (sizeof(x)/sizeof((x)[0]))
#define container_of(ptr,type,member) ((type*)((char*)(ptr)-offsetof(type,member)))
#define min(a,b)            ((a)<(b)?(a):(b))
#define max(a,b)            ((a)>(b)?(a):(b))
#define clamp(v,lo,hi)      ((v)<(lo)?(lo):(v)>(hi)?(hi):(v))
#define DIV_ROUND_UP(n,d)   (((n)+(d)-1)/(d))
#define IS_ERR(x)           ((unsigned long)(x) > (unsigned long)(-4096))
#define PTR_ERR(x)          ((long)(x))
#define ERR_PTR(e)          ((void*)(long)(e))
#define NULL_CHECK(x)       ((x) != 0)
#define READ_ONCE(x)        (x)
#define WRITE_ONCE(x,v)     ((x)=(v))

/* kernel primitive types — NOT in standard headers, safe to define */
typedef unsigned char       u8;
typedef unsigned short      u16;
typedef unsigned int        u32;
typedef unsigned long long  u64;
typedef signed char         s8;
typedef short               s16;
typedef int                 s32;
typedef long long           s64;
typedef u8   __u8;
typedef u16  __u16;
typedef u32  __u32;
typedef u64  __u64;
typedef s8   __s8;
typedef s16  __s16;
typedef s32  __s32;
typedef s64  __s64;
/* uint/ulong/uchar guarded — system headers may already define them */
#ifndef __uint_defined
typedef unsigned int   uint;
#endif
#ifndef __ulong_defined
typedef unsigned long  ulong;
#endif
#ifndef __uchar_defined
typedef unsigned char  uchar;
#endif
"""

PREAMBLE = (("\n".join(_PROJECT_INCLUDES) + "\n\n" + _PREAMBLE_STATIC)
            if _PROJECT_INCLUDES else _PREAMBLE_STATIC)

# Regexes to detect fixable clang errors and extract the symbol name
_ERR_UNKNOWN_TYPE  = re.compile(r"error: unknown type name '(\w+)'")
_ERR_UNDECL_IDENT  = re.compile(r"error: use of undeclared identifier '(\w+)'")
_ERR_IMPLICIT_FUNC = re.compile(r"warning: implicit declaration of function '(\w+)'")
_ERR_INCOMPLETE    = re.compile(r"error: incomplete definition of type '(?:struct|union|enum) (\w+)'")
_ERR_COMBINE       = re.compile(r"error: cannot combine with previous '(?:type-name|storage class)' declaration specifier")
_ERR_MEMBER_ON_INT = re.compile(r"error: member reference (?:base )?type '(?:int|char)(?: \*+)?' is not (?:a structure or union|a pointer)")

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
    except Exception as e:
        return None, f"exception: {e}"
    finally:
        if src_path:
            src_path.unlink(missing_ok=True)
        if ir_path:
            ir_path.unlink(missing_ok=True)


def compile_to_ir(func_source: str, max_retries: int = 6) -> str | None:
    """
    Compile one C function string to LLVM IR.

    On failure, parses clang's stderr for unknown types and undeclared
    identifiers, injects forward declarations, and retries up to
    max_retries times. Handles the majority of Devign functions that
    use project-specific types (AVCodecContext, kmem_cache, etc.).

    Unknown types are stubbed as padded structs (not void*) so that
    pointer member access (avctx->field) gets past the type check.
    If clang then complains that a struct typedef is being used as a
    storage-class qualifier (e.g. "av_cold int func"), we detect the
    "cannot combine" error, identify the offending name from context,
    and replace the struct stub with a no-op macro (#define T).
    """
    preamble = PREAMBLE
    seen_stubs: set[str] = set()
    # track which names were added as struct stubs so we can demote them
    struct_stubs: set[str] = set()

    for attempt in range(max_retries):
        ir, stderr = _try_compile(preamble + "\n" + func_source)
        if ir is not None:
            return ir

        new_stubs: list[str] = []
        demote_to_macro: set[str] = set()
        int_stubs_upgraded = False

        lines = stderr.splitlines()
        for i, line in enumerate(lines):
            # Unknown type name  →  padded struct so member access works
            m = _ERR_UNKNOWN_TYPE.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs:
                    new_stubs.append(f"typedef struct {{ char _pad[512]; }} {t};")
                    seen_stubs.add(t)
                    struct_stubs.add(t)

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

            # "cannot combine with previous 'type-name'" means a name we
            # stubbed as a struct type is being used as a qualifier (e.g.
            # "static av_cold int func").
            # clang format:
            #   i+0: file:line:col: error: cannot combine ...
            #   i+1:   108 | static av_cold int func(...)
            #   i+2:       |                ^           ← caret at the NEW type
            # Only demote struct stubs that appear BEFORE the caret — the
            # offending qualifier precedes the token clang points at.
            if _ERR_COMBINE.search(line):
                src_line   = lines[i + 1] if i + 1 < len(lines) else ""
                caret_line = lines[i + 2] if i + 2 < len(lines) else ""
                pipe_pos   = caret_line.find("|")
                caret_pos  = caret_line.find("^")
                if pipe_pos >= 0 and caret_pos > pipe_pos:
                    # offset of the error token within the source content
                    error_offset = caret_pos - pipe_pos - 1
                    # source content starts right after the "| " separator
                    src_content = src_line[pipe_pos + 1:] if pipe_pos < len(src_line) else src_line
                    for t in list(struct_stubs):
                        m = re.search(r"\b" + re.escape(t) + r"\b", src_content)
                        if m and m.start() < error_offset:
                            demote_to_macro.add(t)
                else:
                    # fallback: demote any struct stub on the line
                    for t in list(struct_stubs):
                        if re.search(r"\b" + re.escape(t) + r"\b", src_line):
                            demote_to_macro.add(t)

            # "member reference ... type 'int' is not a pointer/struct" means
            # a name was stubbed as `static int t = 0` but is actually used
            # as a pointer to struct (via -> or .). Upgrade it to a padded
            # struct typedef so member access passes the type check.
            if _ERR_MEMBER_ON_INT.search(line):
                src_line = lines[i + 1] if i + 1 < len(lines) else ""
                for t in list(seen_stubs):
                    int_stub = f"static int {t} = 0;"
                    if int_stub in preamble and re.search(r"\b" + re.escape(t) + r"\b", src_line):
                        preamble = preamble.replace(int_stub,
                                                    f"typedef struct {{ char _pad[512]; }} {t};")
                        struct_stubs.add(t)
                        int_stubs_upgraded = True

        if demote_to_macro:
            # Rebuild preamble: replace struct stub with empty macro
            for t in demote_to_macro:
                old = f"typedef struct {{ char _pad[512]; }} {t};"
                preamble = preamble.replace(old, f"#define {t}")
                struct_stubs.discard(t)
            # Don't count this as a wasted attempt — loop again
            continue

        if int_stubs_upgraded:
            # preamble was modified in-place; retry without wasting attempt
            continue

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


def process_split(jsonl_path: Path, subset: int | None, workers: int,
                  seed: int = 42) -> list[dict]:
    with open(jsonl_path) as f:
        items = [json.loads(l) for l in f]

    if subset:
        # Random balanced sample so we get a mix of all projects (FFmpeg, QEMU,
        # Linux, LibTIFF) rather than just the first project in the file.
        rng = random.Random(seed)
        vuln  = [x for x in items if x["target"] == 1]
        fixed = [x for x in items if x["target"] == 0]
        rng.shuffle(vuln)
        rng.shuffle(fixed)
        items = vuln[:subset // 2] + fixed[:subset // 2]

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

def debug_one(jsonl_path: Path) -> None:
    """Print clang stderr for the first function that fails — for diagnosing attrition."""
    with open(jsonl_path) as f:
        for line in f:
            item = json.loads(line)
            ir = compile_to_ir(item["func"])
            if ir:
                print("  compiled OK")
            else:
                # Re-run manually to show per-attempt stderr
                preamble = PREAMBLE
                seen: set[str] = set()
                struct_stubs: set[str] = set()
                for attempt in range(6):
                    ir2, stderr = _try_compile(preamble + "\n" + item["func"])
                    if ir2:
                        print(f"  compiled OK on attempt {attempt+1}")
                        break
                    print(f"\n--- attempt {attempt+1} stderr ---\n{stderr[:3000]}")
                    new_stubs = []
                    demote: set[str] = set()
                    int_upgraded = False
                    src_lines = stderr.splitlines()
                    for i, err_line in enumerate(src_lines):
                        m = _ERR_UNKNOWN_TYPE.search(err_line)
                        if m:
                            t = m.group(1)
                            if t not in seen:
                                new_stubs.append(f"typedef struct {{ char _pad[512]; }} {t};")
                                seen.add(t); struct_stubs.add(t)
                        m = _ERR_UNDECL_IDENT.search(err_line)
                        if m:
                            t = m.group(1)
                            if t not in seen:
                                new_stubs.append(f"static int {t} = 0;")
                                seen.add(t)
                        m = _ERR_IMPLICIT_FUNC.search(err_line)
                        if m:
                            t = m.group(1)
                            if t not in seen:
                                new_stubs.append(f"extern void* {t}();")
                                seen.add(t)
                        m = _ERR_INCOMPLETE.search(err_line)
                        if m:
                            t = m.group(1)
                            stub = f"struct {t}"
                            if stub not in seen:
                                new_stubs.append(f"struct {t} {{}};")
                                seen.add(stub)
                        if _ERR_COMBINE.search(err_line):
                            src_line   = src_lines[i + 1] if i + 1 < len(src_lines) else ""
                            caret_line = src_lines[i + 2] if i + 2 < len(src_lines) else ""
                            pipe_pos   = caret_line.find("|")
                            caret_pos  = caret_line.find("^")
                            if pipe_pos >= 0 and caret_pos > pipe_pos:
                                error_offset = caret_pos - pipe_pos - 1
                                src_content  = src_line[pipe_pos + 1:] if pipe_pos < len(src_line) else src_line
                                for t in list(struct_stubs):
                                    m = re.search(r"\b" + re.escape(t) + r"\b", src_content)
                                    if m and m.start() < error_offset:
                                        demote.add(t)
                            else:
                                for t in list(struct_stubs):
                                    if re.search(r"\b" + re.escape(t) + r"\b", src_line):
                                        demote.add(t)
                        if _ERR_MEMBER_ON_INT.search(err_line):
                            src_line = src_lines[i + 1] if i + 1 < len(src_lines) else ""
                            for t in list(seen):
                                int_stub = f"static int {t} = 0;"
                                if int_stub in preamble and re.search(r"\b" + re.escape(t) + r"\b", src_line):
                                    preamble = preamble.replace(int_stub,
                                                                f"typedef struct {{ char _pad[512]; }} {t};")
                                    struct_stubs.add(t)
                                    int_upgraded = True
                                    print(f"  upgrading int stub → struct: {t}")
                    if demote:
                        print(f"  demoting to macro: {demote}")
                        for t in demote:
                            old = f"typedef struct {{ char _pad[512]; }} {t};"
                            preamble = preamble.replace(old, f"#define {t}")
                            struct_stubs.discard(t)
                        continue
                    if int_upgraded:
                        continue
                    if not new_stubs:
                        print("  no fixable errors found — giving up")
                        break
                    preamble += "\n" + "\n".join(new_stubs)
            break   # only debug first function


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset",  type=int, default=None,
                    help="Use only N examples per split (laptop test)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel compilation workers")
    ap.add_argument("--seed",    type=int, default=42,
                    help="Random seed for subset sampling (default: 42)")
    ap.add_argument("--skip-download", action="store_true",
                    help="Skip download if data/*.jsonl already exist")
    ap.add_argument("--debug", action="store_true",
                    help="Print clang stderr for the first failing function and exit")
    args = ap.parse_args()

    if args.debug:
        src = DATA / "train.jsonl"
        if not src.exists():
            print("Run without --debug first to download the dataset.")
            sys.exit(1)
        print(f"Debugging first function in {src} ...")
        debug_one(src)
        sys.exit(0)

    print("\n── Headers ──────────────────────────────────────────────")
    if _PROJECT_INCLUDES:
        for inc in _PROJECT_INCLUDES:
            print(f"  {inc}")
        print("  → project headers active; expect lower attrition")
    else:
        print("  No project headers found. To reduce attrition:")
        print("  Fedora:  sudo dnf install ffmpeg-free-devel libtiff-devel")
        print("  Ubuntu:  sudo apt install libavcodec-dev libtiff-dev")

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
        graphs = process_split(src, subset=args.subset, workers=args.workers,
                               seed=args.seed)
        with open(dst, "wb") as f:
            pickle.dump(graphs, f)
        print(f"  Saved {len(graphs)} graphs → {dst}")

    print("\nDone. Run train.py next.\n")


if __name__ == "__main__":
    main()
