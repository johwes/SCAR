"""Patch safety validator.

Enforces embedded safety rules via regex before triggering compilation,
then compiles the patched source with native clang to confirm it builds.
"""

import json
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_HEAP_ALLOC = re.compile(r"\b(malloc|free|realloc|calloc|alloca)\s*\(")
_BANNED_STR = re.compile(r"\b(strcpy|strcat|sprintf|vsprintf|gets)\s*\(")
_UNBOUNDED_LOOP = re.compile(r"\bwhile\s*\(\s*(1|true)\s*\)")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_c_comments(code: str) -> str:
    code = _BLOCK_COMMENT.sub("", code)
    return _LINE_COMMENT.sub("", code)


@dataclass
class ValidationResult:
    passed: bool
    stage: str      # "safety" | "patch_apply" | "compile" | "ok"
    detail: str


def validate(
    patch: str,
    source_path: str | Path,
    repo_root: str | Path | None = None,
    tag: str = "[validator]",
) -> ValidationResult:
    result = _check_safety_rules(patch)
    if not result.passed:
        return result
    return _check_compilation(patch, Path(source_path), Path(repo_root) if repo_root else None, tag=tag)


def _check_safety_rules(patch: str) -> ValidationResult:
    lines = patch.splitlines()
    added_lines = "\n".join(
        line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")
    )
    removed_lines = "\n".join(
        line[1:] for line in lines if line.startswith("-") and not line.startswith("---")
    )
    # Strip comments before applying safety regexes so that explanatory
    # remarks mentioning banned function names don't trigger false rejections.
    added_code   = _strip_c_comments(added_lines)
    removed_code = _strip_c_comments(removed_lines)

    # Net-count check: a patch that preserves an existing malloc (removes it in
    # one hunk, adds it back in the same hunk with a surrounding guard) is fine.
    # Only reject if the patch increases the number of heap-allocation calls.
    added_alloc = len(_HEAP_ALLOC.findall(added_code))
    removed_alloc = len(_HEAP_ALLOC.findall(removed_code))
    if added_alloc > removed_alloc:
        return ValidationResult(False, "safety", "Patch introduces dynamic heap allocation")
    if _BANNED_STR.search(added_code):
        return ValidationResult(False, "safety", "Patch introduces MISRA-banned string function")
    if _UNBOUNDED_LOOP.search(added_code):
        return ValidationResult(False, "safety", "Patch introduces unbounded while(1) loop")
    return ValidationResult(True, "safety", "All safety rules passed")


def _check_compilation(patch: str, source_path: Path, repo_root: Path | None = None, tag: str = "[validator]") -> ValidationResult:
    source = source_path.read_text(encoding="utf-8", errors="replace")

    patched = _apply_patch(source, patch, source_path.name, tag=tag)
    if patched is None:
        return ValidationResult(False, "patch_apply", "Unified diff failed to apply cleanly")

    flags, build_cwd = _compile_flags_and_cwd(source_path, repo_root)

    # Write the patched file as a sibling of the original so that relative
    # include paths (-I. / -I../include / #include "local.h") resolve correctly.
    patched_sibling = source_path.with_name(f".scar_tmp_{source_path.name}")
    try:
        patched_sibling.write_text(patched, encoding="utf-8")
        cmd = ["clang", "-c", "-x", "c", "-O0", "-Wall"] + flags + ["-o", "/dev/null", str(patched_sibling)]
        # Run from the directory recorded in compile_commands.json so that
        # relative -I paths (e.g. -I../include) resolve against the original
        # build root, not the source file's parent directory.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=build_cwd)
        if proc.returncode != 0:
            return ValidationResult(False, "compile", proc.stderr[:500])
    finally:
        if patched_sibling.exists():
            patched_sibling.unlink()

    return ValidationResult(True, "ok", "Compiled successfully")


def _compile_flags_and_cwd(source_path: Path, repo_root: Path | None) -> tuple[list[str], Path]:
    """Return (compiler_flags, working_directory) for source_path.

    Flags and cwd are sourced from the matching compile_commands.json entry
    when available. The directory field is the build root against which all
    relative -I paths in the command were originally evaluated.
    """
    fallback_cwd = source_path.parent
    if repo_root:
        ccdb_path = repo_root / ".scar" / "compile_commands.json"
        if ccdb_path.exists():
            try:
                entries = json.loads(ccdb_path.read_text())
                resolved = source_path.resolve()
                for entry in entries:
                    if entry.get("file") and entry.get("directory") and \
                            (Path(entry["directory"]) / entry["file"]).resolve() == resolved:
                        build_cwd = Path(entry.get("directory", fallback_cwd))
                        # LLVM spec allows "command" (string) or "arguments" (list).
                        # bear 3.x writes "arguments"; bear 2.x writes "command".
                        if "arguments" in entry:
                            parts = entry["arguments"]
                        else:
                            parts = shlex.split(entry.get("command", ""))
                        flags: list[str] = []
                        i = 0
                        while i < len(parts):
                            p = parts[i]
                            if p in ("-I", "-D", "-isystem", "-iquote") and (i + 1) < len(parts):
                                flags += [p, parts[i + 1]]
                                i += 2
                            elif p.startswith(("-I", "-D", "-isystem", "-iquote", "-std=")):
                                flags.append(p)
                                i += 1
                            else:
                                i += 1
                        return flags, build_cwd
            except Exception:
                pass
    fallback_flags = [f"-I{fallback_cwd}"]
    if repo_root:
        # Always add the repo root itself — many C projects place public headers
        # directly there (e.g. zlib.h at the root, not under include/).
        fallback_flags.append(f"-I{repo_root}")
        for inc_name in ("include", "inc"):
            inc_dir = Path(repo_root) / inc_name
            if inc_dir.is_dir():
                fallback_flags.append(f"-I{inc_dir}")
    return fallback_flags, fallback_cwd


def _apply_patch(source: str, patch: str, filename: str, tag: str = "[validator]") -> str | None:
    """Apply a unified diff, falling back to a context-free Python applier.

    Pass 1: standard patch binary with --fuzz=3 -l (tolerates minor context
    drift and whitespace differences).
    Pass 2: Python-based applier that ignores context lines entirely and
    locates the change by hunk line number with a ±15-line search window.
    This handles LLM-generated patches whose context lines are hallucinated
    but whose +/- change lines and hunk header positions are correct.
    """
    # ── Pass 1: patch binary ──────────────────────────────────────────────
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src_file = Path(tmp) / filename
            patch_file = Path(tmp) / "diff.patch"
            out_file = Path(tmp) / f"{filename}.patched"

            src_file.write_text(source, encoding="utf-8")
            patch_file.write_text(patch, encoding="utf-8")

            proc = subprocess.run(
                [
                    "patch", "--batch", "-s",
                    "--fuzz=3",
                    "-l",
                    "-o", str(out_file), str(src_file), str(patch_file),
                ],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                return out_file.read_text(encoding="utf-8")
            print(f"{tag} [validator] patch binary failed, trying Python applier", flush=True)
    except Exception:
        pass

    # ── Pass 2: context-free Python applier ───────────────────────────────
    return _apply_patch_python(source, patch, tag=tag)


def _apply_patch_python(source: str, patch: str, tag: str = "[validator]") -> str | None:
    """Context-free unified diff applier.

    Parses each hunk, ignores context lines, and locates the block of
    removed lines near the hunk's stated start position using a ±15-line
    search window. Replaces that block with the added lines.
    """
    import re as _re
    src_lines = source.splitlines(keepends=True)
    result = list(src_lines)
    offset = 0  # cumulative line shift from previously applied hunks

    hunk_re = _re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    i = 0
    patch_lines = patch.splitlines(keepends=True)
    while i < len(patch_lines):
        m = hunk_re.match(patch_lines[i])
        if not m:
            i += 1
            continue

        old_start = int(m.group(1)) - 1  # 0-indexed
        i += 1

        removed, added = [], []
        while i < len(patch_lines) and not hunk_re.match(patch_lines[i]):
            line = patch_lines[i]
            if line.startswith("-") and not line.startswith("---"):
                removed.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:])
            # context lines (space-prefixed) are intentionally ignored
            i += 1

        if not removed and not added:
            continue

        # Locate the removed block near old_start (adjusted by prior hunks)
        target = old_start + offset
        found_at = None

        if removed:
            # Strip trailing whitespace for comparison to handle CRLF/LF drift
            needle = [l.rstrip() for l in removed]
            search_start = max(0, target - 15)
            search_end = min(len(result), target + 15)
            for pos in range(search_start, search_end):
                if pos + len(removed) > len(result):
                    break
                window = [result[pos + j].rstrip() for j in range(len(removed))]
                if window == needle:
                    found_at = pos
                    break
            if found_at is None:
                print(
                    f"{tag} [validator] Python applier: could not locate removed block "
                    f"near line {old_start + 1} — aborting",
                    flush=True,
                )
                return None
            result[found_at:found_at + len(removed)] = added
            offset += len(added) - len(removed)
        else:
            # Pure insertion — no lines to locate, insert at target position
            found_at = min(target, len(result))
            result[found_at:found_at] = added
            offset += len(added)

    return "".join(result)
