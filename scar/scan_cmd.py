"""Entry point for the scar-llm-scan Tekton task.

Scans all C files in a repo directory using the LLM vulnerability scanner
(nano-analyzer Stage 2) and writes results to .scar/findings-llm-scan.json.
"""

import json
import sys
from pathlib import Path

from . import context_gen, vuln_scan


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python3 -m scar.scan_cmd <repo_dir> [<source_dir>]", file=sys.stderr)
        sys.exit(1)

    repo = Path(sys.argv[1])
    # Optional second arg scopes the scan to a subdirectory (e.g. source-dir=multifile).
    # Findings still land in repo/.scar/ so the repair loop finds them regardless.
    scan_root = Path(sys.argv[2]) if len(sys.argv) > 2 else repo
    out_path = repo / ".scar" / "findings-llm-scan.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    c_files = sorted(scan_root.rglob("*.c"))
    print(f"[llm-scan] {len(c_files)} C file(s) to scan", flush=True)

    findings = []
    for c_file in c_files:
        print(f"  [llm-scan] {c_file.name}", flush=True)
        briefing = context_gen.generate(str(c_file), str(repo))
        file_findings = vuln_scan.scan(str(c_file), briefing, str(repo))
        print(f"    → {len(file_findings)} finding(s)", flush=True)
        for lf in file_findings:
            findings.append({
                "rule_id": lf.title,
                "severity": lf.severity,
                "file_path": lf.file_path,
                "line": lf.line,
                "column": 0,
                "message": lf.description,
                "function": lf.function,
            })

    out_path.write_text(json.dumps(findings, indent=2))
    print(f"[llm-scan] {len(findings)} finding(s) written → {out_path}", flush=True)


if __name__ == "__main__":
    main()
