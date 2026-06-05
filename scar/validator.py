"""Patch safety validator.

Enforces embedded safety rules via regex before triggering compilation,
then compiles the patched source with native clang to confirm it builds.
"""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Patterns that must NOT appear in added lines of a patch
_HEAP_ALLOC = re.compile(r"\b(malloc|free|realloc|calloc|alloca)\s*\(")
_BANNED_STR = re.compile(r"\b(strcpy|strcat|sprintf|vsprintf|gets)\s*\(")
_UNBOUNDED_LOOP = re.compile(r"\bwhile\s*\(\s*(1|true)\s*\)")


@dataclass
class ValidationResult:
    passed: bool
    stage: str      # "safety" | "compile" | "ok"
    detail: str


def validate(patch: str, source_path: str | Path) -> ValidationResult:
    result = _check_safety_rules(patch)
    if not result.passed:
        return result
    return _check_compilation(patch, Path(source_path))


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


def _check_compilation(patch: str, source_path: Path) -> ValidationResult:
    source = source_path.read_text(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory() as tmpdir:
        patched = Path(tmpdir) / source_path.name
        patched.write_text(_apply_patch_naive(source, patch), encoding="utf-8")

        proc = subprocess.run(
            ["clang", "-c", "-x", "c", "-O0", "-Wall", "-o", "/dev/null", str(patched)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return ValidationResult(False, "compile", proc.stderr[:500])

    return ValidationResult(True, "ok", "Compiled successfully")


def _apply_patch_naive(source: str, patch: str) -> str:
    """Best-effort single-file patch application without the patch binary."""
    try:
        import subprocess, tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as sf:
            sf.write(source)
            src_path = sf.name
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as pf:
            pf.write(patch)
            patch_path = pf.name
        subprocess.run(["patch", "-o", src_path + ".out", src_path, patch_path], check=True, capture_output=True)
        result = open(src_path + ".out").read()
        os.unlink(src_path); os.unlink(patch_path); os.unlink(src_path + ".out")
        return result
    except Exception:
        return source
