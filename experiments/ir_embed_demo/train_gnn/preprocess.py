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
import shutil
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
# Preamble construction — project header auto-detection via pkg-config
#
# The static section covers standard C + common kernel macros + primitive
# typedefs.  If project dev headers are installed (FFmpeg, LibTIFF, GLib)
# we prepend them so that project-specific types are fully resolved by the
# real headers rather than our stub injector.  This drops attrition on
# Devign from ~95% to ~40-60%.
#
# Detection uses pkg-config — the standard tool C build systems use to
# locate headers regardless of where they were installed:
#   pkg-config --exists libavcodec   → is FFmpeg installed?
#   pkg-config --cflags-only-I libavcodec → -I flags for non-std paths
#
# Install on Fedora:   sudo dnf install ffmpeg-free-devel libtiff-devel
# Install on Ubuntu:   sudo apt install libavcodec-dev libtiff-dev
# Install on macOS:    brew install ffmpeg libtiff
# ---------------------------------------------------------------------------

# pkg-config package name → #include line for the main header.
_PKG_MAP: list[tuple[str, str]] = [
    ("libavcodec",  "#include <libavcodec/avcodec.h>"),
    ("libavutil",   "#include <libavutil/avutil.h>"),
    ("libavformat", "#include <libavformat/avformat.h>"),
    ("libavfilter", "#include <libavfilter/avfilter.h>"),
    ("libswscale",  "#include <libswscale/swscale.h>"),
    ("libtiff-4",   "#include <tiff.h>"),
    ("glib-2.0",    "#include <glib.h>"),
]


def _detect_project_headers() -> tuple[list[str], list[str]]:
    """
    Query pkg-config for each known project library.

    Returns:
        includes  — #include lines to prepend to the preamble
        cflags    — extra -I flags (empty on standard Linux; non-empty e.g.
                    on macOS Homebrew where headers live under /opt/homebrew)
    """
    if not shutil.which("pkg-config"):
        return [], []   # pkg-config absent — skip silently

    includes: list[str] = []
    cflags_seen: set[str] = set()
    cflags: list[str] = []

    for pkg, inc in _PKG_MAP:
        r = subprocess.run(["pkg-config", "--exists", pkg], capture_output=True)
        if r.returncode != 0:
            continue
        includes.append(inc)
        # --cflags-only-I returns nothing for /usr/include (already on path),
        # but returns e.g. -I/opt/homebrew/include for Homebrew installs.
        flags = subprocess.run(
            ["pkg-config", "--cflags-only-I", pkg],
            capture_output=True, text=True,
        ).stdout.split()
        for flag in flags:
            if flag not in cflags_seen:
                cflags_seen.add(flag)
                cflags.append(flag)

    return includes, cflags


# Resolved at import time — safe to use in worker processes.
# Project headers go BEFORE the __attribute__ suppressor so they compile
# with real attribute support; our macros then handle remaining extensions.
_PROJECT_INCLUDES, _PROJECT_CFLAGS = _detect_project_headers()

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
_ERR_NO_MEMBER     = re.compile(r"error: no member named '(\w+)' in '(?:struct |union )?(\w+)'")
_ERR_INCOMPAT_INT  = re.compile(r"error: assigning to '(\w+)' \(aka 'struct \1'\) from incompatible type '(?:int|long|unsigned|void \*)'")
_ERR_BINOP_INT     = re.compile(r"error: invalid operands to binary expression \('(\w+)' \(aka 'struct \1'\) and '(?:int|long|unsigned)'\)")
_ERR_NOT_FUNC      = re.compile(r"error: called object type 'int' is not a function or function pointer")
_ERR_SUBSCRIPT     = re.compile(r"error: subscripted value is not an array, pointer, or vector")
_ERR_NOT_CONST     = re.compile(r"error: expression is not an integer constant expression")

# C keywords that can follow an undeclared identifier without making it a type
_C_KEYWORDS = frozenset({
    "return", "goto", "sizeof", "if", "else", "while", "for",
    "do", "switch", "case", "break", "continue", "default",
})

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
             *_PROJECT_CFLAGS,              # -I flags for non-std install paths
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


def _build_struct_stub(t: str, members: list[str],
                       fn_members: list[str] | None = None,
                       ptr_members: list[str] | None = None) -> str:
    """Padded struct stub with int, function-pointer, and void* pointer members."""
    fn_members  = fn_members  or []
    ptr_members = ptr_members or []
    decls  = "".join(f" int {m};"        for m in members
                     if m not in fn_members and m not in ptr_members)
    decls += "".join(f" void* (*{m})();" for m in fn_members)
    decls += "".join(f" void *{m};"      for m in ptr_members)
    if decls:
        return f"typedef struct {t} {{{decls} char _pad[512]; }} {t};"
    return f"typedef struct {t} {{ char _pad[512]; }} {t};"


def compile_to_ir(func_source: str, max_retries: int = 12,
                  _failure_capture: list | None = None) -> str | None:
    """
    Compile one C function string to LLVM IR.

    On failure, parses clang's stderr for unknown types and undeclared
    identifiers, injects forward declarations, and retries up to
    max_retries times. Handles the majority of Devign functions that
    use project-specific types (AVCodecContext, kmem_cache, etc.).

    Unknown types are stubbed as padded structs (not void*) so that
    pointer member access (avctx->field) gets past the type check.
    When clang then reports "no member named 'foo' in 'T'", we inject
    'foo' as an int field into the stub and retry — with -ferror-limit=0
    all missing members are reported at once so one retry typically suffices.
    If clang complains that a struct typedef is being used as a
    storage-class qualifier (e.g. "av_cold int func"), we detect the
    "cannot combine" error, identify the offending name from context,
    and replace the struct stub with a no-op macro (#define T).
    Member injections, demotions, and int-stub upgrades are free retries
    (do not count against max_retries); only new-stub additions are charged.
    """
    preamble = PREAMBLE
    seen_stubs: set[str] = set()
    struct_stubs: set[str] = set()
    struct_members: dict[str, list[str]] = {}
    struct_fn_members: dict[str, list[str]] = {}   # members that are fn ptrs, not ints
    struct_ptr_members: dict[str, list[str]] = {}  # members that are void *, not ints

    paid_attempts = 0   # only new_stubs additions count against the limit
    _last_stderr = ""
    for _ in range(max_retries):
        ir, stderr = _try_compile(preamble + "\n" + func_source)
        if ir is not None:
            return ir
        _last_stderr = stderr

        new_stubs: list[str] = []
        demote_to_macro: set[str] = set()
        demote_to_int_type: set[str] = set()
        demote_to_const: set[str] = set()
        fn_member_upgrades: set[str] = set()
        ptr_member_upgrades: dict[str, list[str]] = {}
        int_stubs_upgraded = False
        new_member_injections: dict[str, list[str]] = {}

        lines = stderr.splitlines()

        for i, line in enumerate(lines):
            # Unknown type name  →  padded struct so member access works
            m = _ERR_UNKNOWN_TYPE.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs:
                    new_stubs.append(_build_struct_stub(t, []))
                    seen_stubs.add(t)
                    struct_stubs.add(t)
                    struct_members[t] = []

            # Undeclared identifier — is_type_context heuristic:
            #   A type name can only appear at statement start or after {, ;, (, ,
            #   Combined with before_char look-behind to skip variables after *.
            m = _ERR_UNDECL_IDENT.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs:
                    src_l = lines[i + 1] if i + 1 < len(lines) else ""
                    car_l = lines[i + 2] if i + 2 < len(lines) else ""
                    pp = car_l.find("|"); cp = car_l.find("^")
                    stub_as_type = False; skip_stub = False
                    if pp >= 0 and cp > pp:
                        off    = cp - pp - 1
                        src_c  = src_l[pp + 1:] if pp < len(src_l) else ""
                        prefix = src_c[:off].rstrip()
                        bef    = prefix[-1] if prefix else ""
                        aft    = src_c[off + len(t):].lstrip()
                        is_type_context = (not prefix.strip() or
                                           prefix.strip()[-1] in "{};(,")
                        if bef == "*":
                            skip_stub = True   # var after pointer star, not a type
                        elif aft.startswith("*"):
                            if is_type_context:
                                stub_as_type = True
                        elif aft and aft[0].isalpha():
                            fw = re.split(r"\W", aft)[0]
                            if fw not in _C_KEYWORDS and is_type_context:
                                stub_as_type = True

                    if not skip_stub:
                        if stub_as_type:
                            new_stubs.append(_build_struct_stub(t, []))
                            struct_stubs.add(t)
                            struct_members[t] = []
                        else:
                            new_stubs.append(f"static int {t} = 0;")
                        seen_stubs.add(t)

            # Implicit function declaration  →  extern void* fn();
            m = _ERR_IMPLICIT_FUNC.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs:
                    new_stubs.append(f"extern void* {t}();")
                    seen_stubs.add(t)

            # Incomplete struct/union — use a padded typedef so that subsequent
            # "no member named" errors can inject members via member injection.
            # The typedef also creates a type alias matching the tag name, which
            # lets code that uses "T *ptr" (without "struct") compile too.
            m = _ERR_INCOMPLETE.search(line)
            if m:
                t = m.group(1)
                if t not in seen_stubs and t not in struct_stubs:
                    new_stubs.append(_build_struct_stub(t, []))
                    seen_stubs.add(t)
                    struct_stubs.add(t)
                    struct_members[t] = []

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
            # with -> or . (it's a struct variable, not a type). Upgrade it:
            # create a separate _struct_T typedef and keep T as a variable so
            # that "t.field" and "t->field" remain valid C expressions.
            if _ERR_MEMBER_ON_INT.search(line):
                src_line = lines[i + 1] if i + 1 < len(lines) else ""
                car_line = lines[i + 2] if i + 2 < len(lines) else ""
                for t in list(seen_stubs):
                    int_stub = f"static int {t} = 0;"
                    # Only upgrade if t is the BASE of a member access (t. or t->),
                    # not merely present on the same line as another member access.
                    if (int_stub in preamble and
                            re.search(r"\b" + re.escape(t) + r"\s*(?:\.|->)", src_line)):
                        struct_name  = f"_struct_{t}"
                        typedef_stub = _build_struct_stub(struct_name, [])
                        is_ptr       = re.search(r"\b" + re.escape(t) + r"\s*->", src_line)
                        var_decl     = (f"static {struct_name} *{t} = 0;" if is_ptr
                                        else f"static {struct_name} {t};")
                        preamble = preamble.replace(int_stub, typedef_stub + "\n" + var_decl)
                        struct_stubs.add(struct_name)
                        struct_members[struct_name] = []
                        int_stubs_upgraded = True
                # Also handle: caret points at a struct MEMBER that is int but
                # used as struct or pointer (e.g. avctx->priv_data->field).
                pp = car_line.find("|"); cp = car_line.find("^")
                if pp >= 0 and cp > pp:
                    off = cp - pp - 1
                    src = src_line[pp + 1:] if pp < len(src_line) else ""
                    mid = re.match(r"\w+", src[off:]) if off < len(src) else None
                    if mid:
                        mem = mid.group(0)
                        for type_name, type_members in struct_members.items():
                            if (mem in type_members and
                                    mem not in struct_fn_members.get(type_name, []) and
                                    mem not in struct_ptr_members.get(type_name, [])):
                                ptr_member_upgrades.setdefault(type_name, []).append(mem)

            # "subscripted value is not an array" — a member or var used with [i].
            # Upgrade: top-level int stub → void * var; struct member → void * field.
            if _ERR_SUBSCRIPT.search(line):
                src_line = lines[i + 1] if i + 1 < len(lines) else ""
                car_line = lines[i + 2] if i + 2 < len(lines) else ""
                pp = car_line.find("|"); cp = car_line.find("^")
                if pp >= 0 and cp > pp:
                    off = cp - pp - 1
                    src = src_line[pp + 1:] if pp < len(src_line) else ""
                    mid = re.match(r"\w+", src[off:]) if off < len(src) else None
                    if mid:
                        mem = mid.group(0)
                        int_stub_v = f"static int {mem} = 0;"
                        if int_stub_v in preamble:
                            preamble = preamble.replace(int_stub_v,
                                                        f"static void *{mem} = 0;")
                            int_stubs_upgraded = True
                        else:
                            for type_name, type_members in struct_members.items():
                                if (mem in type_members and
                                        mem not in struct_fn_members.get(type_name, []) and
                                        mem not in struct_ptr_members.get(type_name, [])):
                                    ptr_member_upgrades.setdefault(type_name, []).append(mem)

            # "expression is not an integer constant expression" — a static int
            # stub was used in a switch case label. C requires case values to be
            # compile-time constants; demote the stub to a #define so it works.
            if _ERR_NOT_CONST.search(line):
                src_line = lines[i + 1] if i + 1 < len(lines) else ""
                car_line = lines[i + 2] if i + 2 < len(lines) else ""
                pp = car_line.find("|"); cp = car_line.find("^")
                if pp >= 0 and cp > pp:
                    off = cp - pp - 1
                    src = src_line[pp + 1:] if pp < len(src_line) else ""
                    mid = re.match(r"\w+", src[off:]) if off < len(src) else None
                    if mid:
                        sym = mid.group(0)
                        if f"static int {sym} = 0;" in preamble:
                            demote_to_const.add(sym)

            # "no member named 'foo' in 'T'" — if T is one of our padded
            # struct stubs, inject 'foo' as an int member and rebuild the stub.
            # This handles FFmpeg/QEMU functions that access struct fields:
            #   avctx->width, avctx->bit_rate, etc.
            # With -ferror-limit=0 clang reports all missing members in one
            # pass, so one retry usually suffices per function.
            m = _ERR_NO_MEMBER.search(line)
            if m:
                member_name, type_name = m.group(1), m.group(2)
                if type_name in struct_stubs:
                    existing = struct_members.get(type_name, [])
                    pending  = new_member_injections.get(type_name, [])
                    if member_name not in existing and member_name not in pending:
                        new_member_injections.setdefault(type_name, []).append(member_name)

            # Struct stub used in integer context (OSStatus-style scalar aliases):
            # demote the struct typedef to a plain int typedef so arithmetic works.
            m = _ERR_INCOMPAT_INT.search(line) or _ERR_BINOP_INT.search(line)
            if m:
                t = m.group(1)
                if t in struct_stubs:
                    demote_to_int_type.add(t)

            # Int member used as function call: upgrade that member to a fn pointer.
            if _ERR_NOT_FUNC.search(line):
                src_l  = lines[i + 1] if i + 1 < len(lines) else ""
                car_l  = lines[i + 2] if i + 2 < len(lines) else ""
                cp     = car_l.find("^")
                if cp > 0:
                    # Extract the identifier immediately before the '('
                    m_fn = re.search(r"(\w+)$", src_l[:cp].rstrip(" ("))
                    if m_fn:
                        fn_member_upgrades.add(m_fn.group(1))

        if demote_to_macro:
            for t in demote_to_macro:
                old = _build_struct_stub(t, struct_members.get(t, []),
                                         struct_fn_members.get(t, []),
                                         struct_ptr_members.get(t, []))
                preamble = preamble.replace(old, f"#define {t}", 1)
                struct_stubs.discard(t)
            continue

        if demote_to_int_type:
            for t in demote_to_int_type:
                old = _build_struct_stub(t, struct_members.get(t, []),
                                         struct_fn_members.get(t, []),
                                         struct_ptr_members.get(t, []))
                preamble = preamble.replace(old, f"typedef int {t};", 1)
                struct_stubs.discard(t)
            continue

        if demote_to_const:
            for sym in demote_to_const:
                preamble = preamble.replace(f"static int {sym} = 0;",
                                            f"#define {sym} 0", 1)
            continue

        if fn_member_upgrades:
            for fn_mem in fn_member_upgrades:
                for type_name, members in struct_members.items():
                    if (fn_mem in members and
                            fn_mem not in struct_fn_members.get(type_name, [])):
                        old_stub = _build_struct_stub(type_name, members,
                                                      struct_fn_members.get(type_name, []),
                                                      struct_ptr_members.get(type_name, []))
                        struct_fn_members.setdefault(type_name, []).append(fn_mem)
                        new_stub = _build_struct_stub(type_name, members,
                                                      struct_fn_members[type_name],
                                                      struct_ptr_members.get(type_name, []))
                        preamble = preamble.replace(old_stub, new_stub, 1)
            continue

        if ptr_member_upgrades:
            for type_name, ptr_mems in ptr_member_upgrades.items():
                old_stub = _build_struct_stub(type_name,
                                              struct_members.get(type_name, []),
                                              struct_fn_members.get(type_name, []),
                                              struct_ptr_members.get(type_name, []))
                for pm in ptr_mems:
                    if pm not in struct_ptr_members.get(type_name, []):
                        struct_ptr_members.setdefault(type_name, []).append(pm)
                new_stub = _build_struct_stub(type_name,
                                              struct_members[type_name],
                                              struct_fn_members.get(type_name, []),
                                              struct_ptr_members[type_name])
                preamble = preamble.replace(old_stub, new_stub, 1)
            continue

        if new_member_injections:
            for type_name, new_members in new_member_injections.items():
                old_stub = _build_struct_stub(type_name, struct_members.get(type_name, []),
                                              struct_fn_members.get(type_name, []),
                                              struct_ptr_members.get(type_name, []))
                struct_members.setdefault(type_name, []).extend(new_members)
                new_stub = _build_struct_stub(type_name, struct_members[type_name],
                                              struct_fn_members.get(type_name, []),
                                              struct_ptr_members.get(type_name, []))
                preamble = preamble.replace(old_stub, new_stub, 1)
            continue

        if int_stubs_upgraded:
            continue

        if not new_stubs:
            if _failure_capture is not None:
                _failure_capture.append(_last_stderr)
            return None
        paid_attempts += 1
        if paid_attempts >= max_retries:
            if _failure_capture is not None:
                _failure_capture.append(_last_stderr)
            return None
        preamble += "\n" + "\n".join(new_stubs)

    if _failure_capture is not None:
        _failure_capture.append(_last_stderr)
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
# Attrition analysis — sample N functions and report final failure categories
# ---------------------------------------------------------------------------

def _categorize_stderr(stderr: str) -> str:
    """Return a short label for the first unfixed error type in a final stderr."""
    for line in stderr.splitlines():
        if ": error:" not in line:
            continue
        if "no member named" in line:
            m = re.search(r"no member named '(\w+)' in '(?:struct )?(\w+)'", line)
            if m:
                return f"no-member-in:{m.group(2)}"
            return "no-member-named"
        if "unknown type name" in line:
            m = re.search(r"unknown type name '(\w+)'", line)
            return f"unknown-type:{m.group(1)}" if m else "unknown-type"
        if "incomplete definition" in line:
            return "incomplete-struct"
        if "not a structure or union" in line:
            return "member-on-non-struct"
        if "subscripted value is not an array" in line:
            return "subscript-on-non-array"
        if "is not a pointer" in line:
            return "member-ref-not-pointer"
        if "not an integer constant expression" in line:
            return "case-label-not-const"
        if "not a function or function pointer" in line:
            return "call-on-non-func"
        if "incompatible type" in line:
            return "type-mismatch"
        if "invalid operands" in line:
            return "invalid-operands"
        if "use of undeclared identifier" in line:
            m = re.search(r"use of undeclared identifier '(\w+)'", line)
            return f"undeclared:{m.group(1)}" if m else "undeclared-id"
        if "cannot combine" in line:
            return "cannot-combine-specifier"
        if "implicit declaration" in line:
            return "implicit-func-decl"
        m = re.search(r"error: (.{0,50})", line)
        if m:
            return m.group(1).strip()[:40]
    return "timeout-or-exception" if not stderr.strip() else "unknown"


def attrition_sample(jsonl_path: Path, n: int, seed: int = 42) -> None:
    """Compile N random functions and report final failure categories."""
    import collections
    rng = random.Random(seed)
    with open(jsonl_path) as f:
        items = [json.loads(l) for l in f]
    rng.shuffle(items)
    items = items[:n]

    cats: collections.Counter = collections.Counter()
    ok = 0
    print(f"  Sampling {n} functions from {jsonl_path.name} (sequential, ~1-3 min) ...")
    for item in items:
        cap: list = []
        ir = compile_to_ir(item["func"], _failure_capture=cap)
        if ir is not None:
            ok += 1
        else:
            cats[_categorize_stderr(cap[0] if cap else "")] += 1

    fail = n - ok
    print(f"\n── Attrition sample ({n} functions) ────────────────────")
    print(f"  Compiled OK : {ok}  ({ok/n*100:.0f}%)")
    print(f"  Failed      : {fail}  ({fail/n*100:.0f}%)")
    if cats:
        print(f"\n  Final failure breakdown (top 15):")
        total_shown = 0
        for cat, cnt in cats.most_common(15):
            pct = cnt / fail * 100
            print(f"    {cnt:4d}  {pct:4.0f}%  {cat}")
            total_shown += cnt
        if total_shown < fail:
            print(f"    {fail-total_shown:4d}  {(fail-total_shown)/fail*100:4.0f}%  (other)")


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
                preamble = PREAMBLE
                seen: set[str] = set()
                struct_stubs: set[str] = set()
                struct_members_d: dict[str, list[str]] = {}
                struct_fn_members_d: dict[str, list[str]] = {}
                struct_ptr_members_d: dict[str, list[str]] = {}
                for attempt in range(8):
                    ir2, stderr = _try_compile(preamble + "\n" + item["func"])
                    if ir2:
                        print(f"  compiled OK on attempt {attempt+1}")
                        break
                    print(f"\n--- attempt {attempt+1} stderr ---\n{stderr[:3000]}")
                    new_stubs = []
                    demote: set[str] = set()
                    demote_int: set[str] = set()
                    demote_const_d: set[str] = set()
                    fn_upgrades: set[str] = set()
                    ptr_upgrades_d: dict[str, list[str]] = {}
                    int_upgraded = False
                    new_members_d: dict[str, list[str]] = {}
                    src_lines = stderr.splitlines()

                    for i, err_line in enumerate(src_lines):
                        m = _ERR_UNKNOWN_TYPE.search(err_line)
                        if m:
                            t = m.group(1)
                            if t not in seen:
                                new_stubs.append(_build_struct_stub(t, []))
                                seen.add(t); struct_stubs.add(t); struct_members_d[t] = []
                        m = _ERR_UNDECL_IDENT.search(err_line)
                        if m:
                            t = m.group(1)
                            if t not in seen:
                                sl  = src_lines[i + 1] if i + 1 < len(src_lines) else ""
                                cl  = src_lines[i + 2] if i + 2 < len(src_lines) else ""
                                pp2 = cl.find("|"); cp2 = cl.find("^")
                                stub_as_type2 = False; skip2 = False
                                if pp2 >= 0 and cp2 > pp2:
                                    off2    = cp2 - pp2 - 1
                                    sc2     = sl[pp2 + 1:] if pp2 < len(sl) else ""
                                    prefix2 = sc2[:off2].rstrip()
                                    bef2    = prefix2[-1] if prefix2 else ""
                                    aft2    = sc2[off2 + len(t):].lstrip()
                                    is_type_ctx = (not prefix2.strip() or
                                                   prefix2.strip()[-1] in "{};(,")
                                    if bef2 == "*":
                                        skip2 = True
                                    elif aft2.startswith("*"):
                                        if is_type_ctx:
                                            stub_as_type2 = True
                                    elif aft2 and aft2[0].isalpha():
                                        fw2 = re.split(r"\W", aft2)[0]
                                        if fw2 not in _C_KEYWORDS and is_type_ctx:
                                            stub_as_type2 = True
                                if not skip2:
                                    if stub_as_type2:
                                        new_stubs.append(_build_struct_stub(t, []))
                                        struct_stubs.add(t); struct_members_d[t] = []
                                    else:
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
                            if t not in seen and t not in struct_stubs:
                                new_stubs.append(_build_struct_stub(t, []))
                                seen.add(t)
                                struct_stubs.add(t); struct_members_d[t] = []
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
                            car_line = src_lines[i + 2] if i + 2 < len(src_lines) else ""
                            for t in list(seen):
                                int_stub = f"static int {t} = 0;"
                                if (int_stub in preamble and
                                        re.search(r"\b" + re.escape(t) + r"\s*(?:\.|->)", src_line)):
                                    sn       = f"_struct_{t}"
                                    td_stub  = _build_struct_stub(sn, [])
                                    is_ptr   = re.search(r"\b" + re.escape(t) + r"\s*->", src_line)
                                    var_decl = (f"static {sn} *{t} = 0;" if is_ptr
                                                else f"static {sn} {t};")
                                    preamble = preamble.replace(int_stub, td_stub + "\n" + var_decl)
                                    struct_stubs.add(sn)
                                    struct_members_d[sn] = []
                                    int_upgraded = True
                                    print(f"  upgrading int stub → struct var: {t} (type {sn})")
                            pp_d = car_line.find("|"); cp_d = car_line.find("^")
                            if pp_d >= 0 and cp_d > pp_d:
                                off_d = cp_d - pp_d - 1
                                src_d = src_line[pp_d + 1:] if pp_d < len(src_line) else ""
                                mid_d = re.match(r"\w+", src_d[off_d:]) if off_d < len(src_d) else None
                                if mid_d:
                                    mem_d = mid_d.group(0)
                                    for tn, tm in struct_members_d.items():
                                        if (mem_d in tm and
                                                mem_d not in struct_fn_members_d.get(tn, []) and
                                                mem_d not in struct_ptr_members_d.get(tn, [])):
                                            print(f"  upgrading struct member → void*: {tn}.{mem_d}")
                                            ptr_upgrades_d.setdefault(tn, []).append(mem_d)
                        if _ERR_SUBSCRIPT.search(err_line):
                            src_line = src_lines[i + 1] if i + 1 < len(src_lines) else ""
                            car_line = src_lines[i + 2] if i + 2 < len(src_lines) else ""
                            pp_s = car_line.find("|"); cp_s = car_line.find("^")
                            if pp_s >= 0 and cp_s > pp_s:
                                off_s = cp_s - pp_s - 1
                                src_s = src_line[pp_s + 1:] if pp_s < len(src_line) else ""
                                mid_s = re.match(r"\w+", src_s[off_s:]) if off_s < len(src_s) else None
                                if mid_s:
                                    mem_s = mid_s.group(0)
                                    int_stub_s = f"static int {mem_s} = 0;"
                                    if int_stub_s in preamble:
                                        preamble = preamble.replace(int_stub_s,
                                                                    f"static void *{mem_s} = 0;")
                                        print(f"  upgrading int stub → void*: {mem_s}")
                                        int_upgraded = True
                                    else:
                                        for tn, tm in struct_members_d.items():
                                            if (mem_s in tm and
                                                    mem_s not in struct_fn_members_d.get(tn, []) and
                                                    mem_s not in struct_ptr_members_d.get(tn, [])):
                                                print(f"  upgrading struct member → void*: {tn}.{mem_s}")
                                                ptr_upgrades_d.setdefault(tn, []).append(mem_s)
                        m = _ERR_NO_MEMBER.search(err_line)
                        if m:
                            member_name, type_name = m.group(1), m.group(2)
                            if type_name in struct_stubs:
                                existing = struct_members_d.get(type_name, [])
                                pending  = new_members_d.get(type_name, [])
                                if member_name not in existing and member_name not in pending:
                                    new_members_d.setdefault(type_name, []).append(member_name)
                        md = _ERR_INCOMPAT_INT.search(err_line) or _ERR_BINOP_INT.search(err_line)
                        if md:
                            t = md.group(1)
                            if t in struct_stubs:
                                demote_int.add(t)
                        if _ERR_NOT_FUNC.search(err_line):
                            src_line = src_lines[i + 1] if i + 1 < len(src_lines) else ""
                            car_line = src_lines[i + 2] if i + 2 < len(src_lines) else ""
                            cp2 = car_line.find("^")
                            if cp2 > 0:
                                m_fn = re.search(r"(\w+)$", src_line[:cp2].rstrip(" ("))
                                if m_fn:
                                    fn_upgrades.add(m_fn.group(1))
                        if _ERR_NOT_CONST.search(err_line):
                            src_line = src_lines[i + 1] if i + 1 < len(src_lines) else ""
                            car_line = src_lines[i + 2] if i + 2 < len(src_lines) else ""
                            pp_c = car_line.find("|"); cp_c = car_line.find("^")
                            if pp_c >= 0 and cp_c > pp_c:
                                off_c = cp_c - pp_c - 1
                                src_c = src_line[pp_c + 1:] if pp_c < len(src_line) else ""
                                mid_c = re.match(r"\w+", src_c[off_c:]) if off_c < len(src_c) else None
                                if mid_c:
                                    sym_c = mid_c.group(0)
                                    if f"static int {sym_c} = 0;" in preamble:
                                        print(f"  demoting to const macro: {sym_c}")
                                        demote_const_d.add(sym_c)

                    if demote:
                        print(f"  demoting to macro: {demote}")
                        for t in demote:
                            old = _build_struct_stub(t, struct_members_d.get(t, []),
                                                     struct_fn_members_d.get(t, []),
                                                     struct_ptr_members_d.get(t, []))
                            preamble = preamble.replace(old, f"#define {t}", 1)
                            struct_stubs.discard(t)
                        continue
                    if demote_int:
                        print(f"  demoting struct→int typedef: {demote_int}")
                        for t in demote_int:
                            old = _build_struct_stub(t, struct_members_d.get(t, []),
                                                     struct_fn_members_d.get(t, []),
                                                     struct_ptr_members_d.get(t, []))
                            preamble = preamble.replace(old, f"typedef int {t};", 1)
                            struct_stubs.discard(t)
                        continue
                    if demote_const_d:
                        for sym in demote_const_d:
                            preamble = preamble.replace(f"static int {sym} = 0;",
                                                        f"#define {sym} 0", 1)
                        continue
                    if fn_upgrades:
                        print(f"  upgrading int members → fn ptrs: {fn_upgrades}")
                        for fn_mem in fn_upgrades:
                            for type_name, members in struct_members_d.items():
                                if (fn_mem in members and
                                        fn_mem not in struct_fn_members_d.get(type_name, [])):
                                    old_stub = _build_struct_stub(
                                        type_name, members,
                                        struct_fn_members_d.get(type_name, []),
                                        struct_ptr_members_d.get(type_name, []))
                                    struct_fn_members_d.setdefault(type_name, []).append(fn_mem)
                                    new_stub = _build_struct_stub(
                                        type_name, members,
                                        struct_fn_members_d[type_name],
                                        struct_ptr_members_d.get(type_name, []))
                                    preamble = preamble.replace(old_stub, new_stub, 1)
                        continue
                    if ptr_upgrades_d:
                        print(f"  upgrading int members → void*: { {k: v for k, v in ptr_upgrades_d.items()} }")
                        for type_name, ptr_mems in ptr_upgrades_d.items():
                            old_stub = _build_struct_stub(type_name,
                                                          struct_members_d.get(type_name, []),
                                                          struct_fn_members_d.get(type_name, []),
                                                          struct_ptr_members_d.get(type_name, []))
                            for pm in ptr_mems:
                                if pm not in struct_ptr_members_d.get(type_name, []):
                                    struct_ptr_members_d.setdefault(type_name, []).append(pm)
                            new_stub = _build_struct_stub(type_name,
                                                          struct_members_d[type_name],
                                                          struct_fn_members_d.get(type_name, []),
                                                          struct_ptr_members_d[type_name])
                            preamble = preamble.replace(old_stub, new_stub, 1)
                        continue
                    if new_members_d:
                        print(f"  injecting members: { {k: v for k, v in new_members_d.items()} }")
                        for type_name, new_m in new_members_d.items():
                            old_stub = _build_struct_stub(type_name,
                                                          struct_members_d.get(type_name, []),
                                                          struct_fn_members_d.get(type_name, []),
                                                          struct_ptr_members_d.get(type_name, []))
                            struct_members_d.setdefault(type_name, []).extend(new_m)
                            new_stub = _build_struct_stub(type_name,
                                                          struct_members_d[type_name],
                                                          struct_fn_members_d.get(type_name, []),
                                                          struct_ptr_members_d.get(type_name, []))
                            preamble = preamble.replace(old_stub, new_stub, 1)
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
    ap.add_argument("--attrition-sample", type=int, default=None, metavar="N",
                    help="Sample N functions and print final failure breakdown")
    args = ap.parse_args()

    if args.debug:
        src = DATA / "train.jsonl"
        if not src.exists():
            print("Run without --debug first to download the dataset.")
            sys.exit(1)
        print(f"Debugging first function in {src} ...")
        debug_one(src)
        sys.exit(0)

    if args.attrition_sample:
        src = DATA / "train.jsonl"
        if not src.exists():
            print("Run without --attrition-sample first to download the dataset.")
            sys.exit(1)
        attrition_sample(src, args.attrition_sample, seed=args.seed)
        sys.exit(0)

    print("\n── Headers ──────────────────────────────────────────────")
    if not shutil.which("pkg-config"):
        print("  pkg-config not found — install it to enable project header detection")
    elif _PROJECT_INCLUDES:
        found_pkgs  = [pkg for pkg, _ in _PKG_MAP
                       if any(pkg in inc for inc in _PROJECT_INCLUDES)]
        missing_pkgs = [pkg for pkg, _ in _PKG_MAP
                        if not any(pkg in inc for inc in _PROJECT_INCLUDES)]
        for inc in _PROJECT_INCLUDES:
            print(f"  ✓ {inc}")
        if _PROJECT_CFLAGS:
            print(f"  cflags: {' '.join(_PROJECT_CFLAGS)}")
        if missing_pkgs:
            print(f"  ✗ not found: {', '.join(missing_pkgs)}")
            print("    Fedora:  sudo dnf install ffmpeg-free-devel libtiff-devel")
            print("    Ubuntu:  sudo apt install libavcodec-dev libtiff-dev")
        print("  → project headers active; expect lower attrition")
    else:
        print("  No project headers found via pkg-config. To reduce attrition:")
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
