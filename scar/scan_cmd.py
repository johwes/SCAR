"""Entry point for the scar-llm-scan Tekton task.

Scans all C files in a repo directory using the LLM vulnerability scanner
(nano-analyzer Stage 2) and writes results to .scar/findings-llm-scan.json.
"""

import json
import sys
from pathlib import Path

from . import context_gen, vuln_scan, llm


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

    all_c_files = sorted(scan_root.rglob("*.c"))

    def _is_app_code(f: Path) -> bool:
        """True for C files that are application code, not fuzz/test scaffolding."""
        return (
            "fuzz" not in f.name.lower()
            and not any(p in ("test", "tests", "fuzzer", "fuzzers") for p in f.parts)
        )

    ccdb_path = repo / ".scar" / "compile_commands.json"
    if ccdb_path.exists():
        try:
            entries = json.loads(ccdb_path.read_text())
            ccdb_files = {
                str((Path(e["directory"]) / e["file"]).resolve())
                for e in entries
                if e.get("file") and e.get("directory")
            }
            # Files in the CCDB (captures fuzz harnesses bear may have picked up).
            ccdb_filtered = [f for f in all_c_files
                             if str(f.resolve()) in ccdb_files and _is_app_code(f)]
            # Files NOT in the CCDB that are still application code.
            # OSS-Fuzz build scripts compile only the fuzz harness, not the server
            # binary — so main.c has no CCDB entry even though it should be scanned.
            non_ccdb = [f for f in all_c_files
                        if str(f.resolve()) not in ccdb_files and _is_app_code(f)]
            c_files = sorted(set(ccdb_filtered + non_ccdb))
            extra_note = f", +{len(non_ccdb)} outside CCDB" if non_ccdb else ""
            print(f"[llm-scan] {len(c_files)} C file(s) to scan "
                  f"({len(ccdb_filtered)} from CCDB{extra_note})", flush=True)
        except Exception:
            c_files = [f for f in all_c_files if _is_app_code(f)]
            print(f"[llm-scan] {len(c_files)} C file(s) to scan "
                  f"(compile_commands.json unreadable — scanning all)", flush=True)
    else:
        c_files = [f for f in all_c_files if _is_app_code(f)]
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

    usage = llm.get_usage()
    if usage["total_tokens"]:
        print(
            f"[llm-scan] tokens: {usage['prompt_tokens']:,} prompt + "
            f"{usage['completion_tokens']:,} completion = "
            f"{usage['total_tokens']:,} total",
            flush=True,
        )
    token_file = repo / ".scar" / "token-usage-llm-scan.json"
    token_file.write_text(json.dumps(usage))


if __name__ == "__main__":
    main()
