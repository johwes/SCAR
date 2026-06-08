#!/usr/bin/env python3
"""Validate a SCAR findings JSON file against the pluggable findings schema.

Usage:
    python3 scar/validate_schema.py findings-myscanner.json [findings-other.json ...]

Exits 0 if all files are valid, 1 if any schema error is found.
Safe to use in a pre-commit hook or CI step.
"""
import sys
import json
from pathlib import Path

REQUIRED = {"rule_id", "severity", "file_path", "line", "message"}
VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


def check_file(path: str) -> bool:
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        print(f"[-] {p.name}: invalid JSON — {e}")
        return False

    if not isinstance(data, list):
        print(f"[-] {p.name}: top-level structure must be a JSON array, got {type(data).__name__}")
        return False

    ok = True
    for idx, entry in enumerate(data):
        missing = REQUIRED - entry.keys()
        if missing:
            print(f"[-] {p.name} entry {idx}: missing required keys: {sorted(missing)}")
            ok = False
            continue

        if not isinstance(entry["line"], int):
            print(
                f"[-] {p.name} entry {idx}: 'line' must be an integer,"
                f" got {type(entry['line']).__name__} ({entry['line']!r})"
            )
            ok = False

        sev = entry["severity"]
        if sev not in VALID_SEVERITIES:
            print(
                f"[-] {p.name} entry {idx}: 'severity' is {sev!r},"
                f" must be one of {sorted(VALID_SEVERITIES)}"
            )
            ok = False

    if ok:
        print(f"[+] {p.name}: {len(data)} finding(s) — schema valid")
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scar/validate_schema.py <findings.json> [...]")
        sys.exit(1)

    all_ok = all(check_file(f) for f in sys.argv[1:])
    sys.exit(0 if all_ok else 1)
