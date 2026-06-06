"""Patch safety validator.

Enforces embedded safety rules via regex before triggering compilation,
then compiles the patched source with native clang to confirm it builds.
"""

import re
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

    patched = _apply_patch(source, patch, source_path.name)
    if patched is None:
        return ValidationResult(False, "patch_apply", "Unified diff failed to apply cleanly")

    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / source_path.name
        out.write_text(patched, encoding="utf-8")

        # Include the source file's parent directory so local headers resolve.
        # For projects with compile_commands.json the caller should supply
        # precise -I flags; this is the best-effort fallback.
        cmd = [
            "clang", "-c", "-x", "c", "-O0", "-Wall",
            f"-I{source_path.parent}",
            "-o", "/dev/null",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return ValidationResult(False, "compile", proc.stderr[:500])

    return ValidationResult(True, "ok", "Compiled successfully")


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
