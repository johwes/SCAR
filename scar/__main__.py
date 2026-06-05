"""CLI entry point: scar <sarif_path> <repo_dir> [--triage-rounds N]."""

import argparse
import json
import sys
from pathlib import Path

from .sarif_bridge import IkosSarifBridge
from . import context_gen, patch_gen, triage, validator


def main() -> None:
    parser = argparse.ArgumentParser(prog="scar", description="SCAR — Static C Analysis & Repair")
    parser.add_argument("--version", action="version", version="scar 0.1.0")
    parser.add_argument("sarif", help="Path to IKOS SARIF output file")
    parser.add_argument("repo", help="Repository root directory")
    parser.add_argument("--triage-rounds", type=int, default=5)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--output", default="scar-results.json")
    args = parser.parse_args()

    bridge = IkosSarifBridge(args.sarif, args.repo)
    findings = bridge.parse()
    print(f"[scar] {len(findings)} findings from IKOS")

    accepted = []

    for finding in findings:
        source = finding.file_path
        print(f"\n[scar] ── {finding.rule_id} @ {source}:{finding.line} ──")
        print(f"  {finding.message}")

        print(f"  [1/3] Generating security briefing...")
        briefing = context_gen.generate(source, args.repo)
        print(f"  [1/3] Briefing ready ({len(briefing)} chars)")

        print(f"  [2/3] Synthesising patch...")
        patch = patch_gen.generate(finding, briefing, source)
        print(f"  [2/3] Patch ready ({len(patch.splitlines())} lines)")

        print(f"  [3/3] Validating patch...")
        val = validator.validate(patch, source)
        if not val.passed:
            print(f"  [skip] Validation failed ({val.stage}): {val.detail}")
            continue
        print(f"  [3/3] Validation passed — running triage ({args.triage_rounds} rounds)")

        result = triage.run(finding, patch, source, args.repo, rounds=args.triage_rounds)
        print(f"  [triage] verdict={result.verdict} confidence={result.confidence:.2f} chain={result.chain}")

        if result.verdict == "VALID" and result.confidence >= args.min_confidence:
            accepted.append({"finding": finding.__dict__, "patch": patch, "triage": result.__dict__})
            print(f"  [accept] {result.reason}")
        else:
            print(f"  [reject] {result.reason}")

    Path(args.output).write_text(json.dumps(accepted, indent=2))
    print(f"[scar] {len(accepted)} patches accepted → {args.output}")
    sys.exit(0 if accepted else 1)


if __name__ == "__main__":
    main()
