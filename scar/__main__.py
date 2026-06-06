"""CLI entry point: scar <sarif_path> <repo_dir> [--triage-rounds N]."""

import argparse
import json
import sys
from pathlib import Path

from .sarif_bridge import IkosSarifBridge, Finding
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

    # ── IKOS findings ────────────────────────────────────────────────────────
    bridge = IkosSarifBridge(args.sarif, args.repo)
    ikos_findings = bridge.parse()
    print(f"[scar] {len(ikos_findings)} finding(s) from IKOS", flush=True)

    # Build a per-file line index for ±3-line sliding-window deduplication.
    # LLMs and static analyzers often flag the same bug at slightly different
    # line numbers (e.g. the memcpy sink vs. the tainted assignment above it).
    # Normalise all IKOS paths to resolved absolute form so that comparisons
    # against LLM-scan paths (which may use rglob-absolute or SARIF-relative
    # roots) never silently miss due to path string mismatches.
    ikos_map: dict[str, list[int]] = {}
    for f in ikos_findings:
        norm = str(Path(f.file_path).resolve())
        ikos_map.setdefault(norm, []).append(f.line)

    def _near_ikos(file_path: str, line: int, radius: int = 3) -> bool:
        norm = str(Path(file_path).resolve())
        return any(abs(line - l) <= radius for l in ikos_map.get(norm, []))

    # ── Auxiliary findings (any task writing .scar/findings-<name>.json) ────
    # Convention: any analyzer Tekton task drops a findings-<name>.json file
    # in .scar/ using the schema {rule_id, severity, file_path, line, message}.
    # The repair loop discovers and deduplicates all of them automatically —
    # no changes needed here when a new tool is added to the pipeline.
    aux_findings: list[Finding] = []
    scar_dir = Path(args.repo) / ".scar"
    for findings_file in sorted(scar_dir.glob("findings-*.json")):
        try:
            raw = json.loads(findings_file.read_text())
            before = len(aux_findings)
            for item in raw:
                if _near_ikos(item["file_path"], item["line"]):
                    continue  # IKOS already covers this location (within ±3 lines)
                aux_findings.append(Finding(
                    rule_id=item["rule_id"],
                    severity=item["severity"],
                    file_path=item["file_path"],
                    line=item["line"],
                    column=item.get("column", 0),
                    message=item["message"],
                ))
            added = len(aux_findings) - before
            print(f"[scar] {added} finding(s) from {findings_file.name}", flush=True)
        except Exception as exc:
            print(f"[scar] warning: could not load {findings_file.name}: {exc}", flush=True)

    if not list(scar_dir.glob("findings-*.json")):
        print(f"[scar] no auxiliary findings files found in .scar/", flush=True)

    llm_findings = aux_findings

    all_findings = ikos_findings + llm_findings
    print(f"[scar] {len(all_findings)} total finding(s) to process", flush=True)

    # ── Repair loop ──────────────────────────────────────────────────────────
    accepted = []

    for finding in all_findings:
        source = finding.file_path
        origin = "ikos" if finding in ikos_findings else "llm"
        print(f"\n[scar] ── {finding.rule_id} @ {source}:{finding.line} [{origin}] ──", flush=True)
        print(f"  {finding.message}", flush=True)

        print(f"  [1/3] Generating security briefing...", flush=True)
        # Locate the IKOS witness db for this file (named after the .c basename)
        stem = Path(source).stem
        witness_db = Path(args.repo) / ".scar" / f"{stem}.db"
        briefing = context_gen.generate(
            source, args.repo,
            witness_db=witness_db if witness_db.exists() else None,
            finding_line=finding.line,
        )
        print(f"  [1/3] Briefing ready ({len(briefing)} chars)", flush=True)

        print(f"  [2/3] Synthesising patch...", flush=True)
        patch = patch_gen.generate(finding, briefing, source)
        print(f"  [2/3] Patch ready ({len(patch.splitlines())} lines)", flush=True)

        print(f"  [3/3] Validating patch...", flush=True)
        val = validator.validate(patch, source, repo_root=args.repo)
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
