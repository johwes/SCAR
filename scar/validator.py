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


@dataclass
class ValidationResult:
    passed: bool
    stage: str      # "safety" | "patch_apply" | "compile" | "ok"
    detail: str


def validate(patch: str, source_path: str | Path, repo_root: str | Path | None = None) -> ValidationResult:
    result = _check_safety_rules(patch)
    if not result.passed:
        return result
    return _check_compilation(patch, Path(source_path), Path(repo_root) if repo_root else None)


def _check_safety_rules(patch: str) -> ValidationResult:
    added_lines = "\n".join(
        line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    if _HEAP_ALLOC.search(added_lines):
        return ValidationResult(False, "safety", "Patch introduces dynamic heap allocation")
    if _BANNED_STR.search(added_lines):
        return ValidationResult(False, "safety", "Patch introduces MISRA-banned string function")
    if _UNBOUNDED_LOOP.search(added_lines):
        return ValidationResult(False, "safety", "Patch introduces unbounded while(1) loop")
    return ValidationResult(True, "safety", "All safety rules passed")


def _check_compilation(patch: str, source_path: Path, repo_root: Path | None = None) -> ValidationResult:
    source = source_path.read_text(encoding="utf-8", errors="replace")

    patched = _apply_patch(source, patch, source_path.name)
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
        for inc_name in ("include", "inc"):
            inc_dir = Path(repo_root) / inc_name
            if inc_dir.is_dir():
                fallback_flags.append(f"-I{inc_dir}")
    return fallback_flags, fallback_cwd


def _apply_patch(source: str, patch: str, filename: str) -> str | None:
    """Apply a unified diff using the patch binary in strict batch mode.

    Returns the patched text, or None if the patch does not apply cleanly.
    --batch suppresses all interactive prompts so the process never hangs
    in a headless Tekton container waiting for stdin.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src_file = Path(tmp) / filename
            patch_file = Path(tmp) / "diff.patch"
            out_file = Path(tmp) / f"{filename}.patched"

            src_file.write_text(source, encoding="utf-8")
            patch_file.write_text(patch, encoding="utf-8")

            proc = subprocess.run(
                ["patch", "--batch", "-s", "-o", str(out_file), str(src_file), str(patch_file)],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                return None
            return out_file.read_text(encoding="utf-8")
    except Exception:
        return None
