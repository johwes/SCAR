"""Entry point for the scar-llm-scan Tekton task.

Scans all C files in a repo directory using the LLM vulnerability scanner
(nano-analyzer Stage 2) and writes results to .scar/findings-llm-scan.json.

Files are scanned concurrently up to --max-workers (default 4). Each file
is fully independent — no shared mutable state between workers — so
parallelism here is straightforward I/O concurrency.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import context_gen, vuln_scan, llm


def _scan_one(c_file: Path, repo: Path) -> list[dict]:
    """Scan a single C file and return normalised finding dicts."""
    tag = f"[{c_file.stem}]"
    print(f"{tag} [llm-scan] starting", flush=True)
    briefing = context_gen.generate(str(c_file), str(repo), tag=tag)
    file_findings = vuln_scan.scan(str(c_file), briefing, str(repo))
    print(f"{tag} [llm-scan] → {len(file_findings)} finding(s)", flush=True)
    return [
        {
            "rule_id":  lf.title,
            "severity": lf.severity,
            "file_path": lf.file_path,
            "line":     lf.line,
            "column":   0,
            "message":  lf.description,
            "function": lf.function,
        }
        for lf in file_findings
    ]


def main() -> None:
    parser = argparse.ArgumentParser(prog="scar.scan_cmd")
    parser.add_argument("repo", help="Repository root directory")
    parser.add_argument("scan_root", nargs="?",
                        help="Subdirectory to scan (default: repo root)")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="Max concurrent file scanners (default: 4)")
    args = parser.parse_args()

    repo      = Path(args.repo)
    scan_root = Path(args.scan_root) if args.scan_root else repo
    out_path  = repo / ".scar" / "findings-llm-scan.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_c_files = sorted(scan_root.rglob("*.c"))

    _THIRD_PARTY = frozenset({
        "contrib", "third_party", "thirdparty", "external",
        "extern", "vendor", "deps", "dependencies",
    })

    def _is_app_code(f: Path) -> bool:
        """True for first-party application code (not fuzz scaffolding or third-party)."""
        parts = {p.lower() for p in f.parts}
        return (
            "fuzz" not in f.name.lower()
            and not any(p in ("test", "tests", "fuzzer", "fuzzers") for p in f.parts)
            and not (parts & _THIRD_PARTY)
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
            # Directories that appear in the CCDB — used to include non-CCDB
            # files that live alongside built files (e.g. a main.c added to
            # the repo after the build was captured). Files in directories
            # with no CCDB entries (contrib/, platform-specific subtrees) are
            # excluded so we don't waste tokens on unrelated code.
            ccdb_dirs = {
                str((Path(e["directory"]) / e["file"]).resolve().parent)
                for e in entries
                if e.get("file") and e.get("directory")
            }
            ccdb_filtered = [f for f in all_c_files
                             if str(f.resolve()) in ccdb_files and _is_app_code(f)]
            non_ccdb = [f for f in all_c_files
                        if str(f.resolve()) not in ccdb_files
                        and _is_app_code(f)
                        and str(f.parent.resolve()) in ccdb_dirs]
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

    n_workers = min(args.max_workers, len(c_files)) if c_files else 1
    print(f"[llm-scan] {n_workers} concurrent worker(s)", flush=True)

    findings: list[dict] = []

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_scan_one, f, repo): f for f in c_files}
        for future in as_completed(futures):
            c_file = futures[future]
            try:
                findings.extend(future.result())
            except Exception as exc:
                print(f"  [llm-scan] error scanning {c_file.name}: {exc}", flush=True)

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
