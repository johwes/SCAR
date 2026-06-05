"""CLI entry point: scar <sarif_path> <repo_dir> [--triage-rounds N]."""

import argparse
import json
import sys
from pathlib import Path

from .sarif_bridge import IkosSarifBridge, Finding
from . import context_gen, patch_gen, triage, validator, vuln_scan


def main() -> None:
    parser = argparse.ArgumentParser(prog="scar", description="SCAR — Static C Analysis & Repair")
    parser.add_argument("--version", action="version", version="scar 0.1.0")
    parser.add_argument("sarif", help="Path to IKOS SARIF output file")
    parser.add_argument("repo", help="Repository root directory")
    parser.add_argument("--triage-rounds", type=int, default=5)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--output", default="scar-results.json")
    args = parser.parse_args()

    # ── Stage 0: IKOS findings ──────────────────────────────────────────────
    bridge = IkosSarifBridge(args.sarif, args.repo)
    ikos_findings = bridge.parse()
    print(f"[scar] {len(ikos_findings)} findings from IKOS", flush=True)

    ikos_locations = {(f.file_path, f.line) for f in ikos_findings}

    # ── Stage 2: LLM vulnerability scan (nano-analyzer style) ───────────────
    c_files = sorted(Path(args.repo).rglob("*.c"))
    print(f"[scar] LLM vulnerability scan on {len(c_files)} C file(s)...", flush=True)

    llm_findings: list[Finding] = []
    briefing_cache: dict[str, str] = {}

    for c_file in c_files:
        print(f"  [llm-scan] {c_file.name}", flush=True)
        briefing = context_gen.generate(str(c_file), args.repo)
        briefing_cache[str(c_file)] = briefing

        for lf in vuln_scan.scan(str(c_file), briefing, args.repo):
            if (lf.file_path, lf.line) in ikos_locations:
                continue  # IKOS already has this location covered
            llm_findings.append(Finding(
                rule_id=lf.title,
                severity=lf.severity,
                file_path=lf.file_path,
                line=lf.line,
                column=0,
                message=lf.description,
            ))

    print(f"[scar] {len(llm_findings)} additional finding(s) from LLM scan", flush=True)

    all_findings = ikos_findings + llm_findings
    print(f"[scar] {len(all_findings)} total finding(s) to process", flush=True)

    # ── Repair loop ──────────────────────────────────────────────────────────
    accepted = []

    for finding in all_findings:
        source = finding.file_path
        origin = "ikos" if (source, finding.line) in ikos_locations else "llm"
        print(f"\n[scar] ── {finding.rule_id} @ {source}:{finding.line} [{origin}] ──", flush=True)
        print(f"  {finding.message}", flush=True)

        print(f"  [1/3] Generating security briefing...", flush=True)
        briefing = briefing_cache.get(source) or context_gen.generate(source, args.repo)
        print(f"  [1/3] Briefing ready ({len(briefing)} chars)", flush=True)

        print(f"  [2/3] Synthesising patch...", flush=True)
        patch = patch_gen.generate(finding, briefing, source)
        print(f"  [2/3] Patch ready ({len(patch.splitlines())} lines)", flush=True)

        print(f"  [3/3] Validating patch...", flush=True)
        val = validator.validate(patch, source)
        if not val.passed:
            print(f"  [skip] Validation failed ({val.stage}): {val.detail}", flush=True)
            continue
        print(f"  [3/3] Validation passed — running triage ({args.triage_rounds} rounds)", flush=True)

        result = triage.run(finding, patch, source, args.repo, rounds=args.triage_rounds)
        print(f"  [triage] verdict={result.verdict} confidence={result.confidence:.2f} chain={result.chain}", flush=True)

        if result.verdict == "VALID" and result.confidence >= args.min_confidence:
            accepted.append({"finding": finding.__dict__, "patch": patch, "triage": result.__dict__, "origin": origin})
            print(f"  [accept] {result.reason}", flush=True)
        else:
            print(f"  [reject] {result.reason}", flush=True)

    Path(args.output).write_text(json.dumps(accepted, indent=2))
    print(f"\n[scar] {len(accepted)} patch(es) accepted → {args.output}", flush=True)
    sys.exit(0 if accepted else 1)


if __name__ == "__main__":
    main()
