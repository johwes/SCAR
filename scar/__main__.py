"""CLI entry point: scar <sarif_path> <repo_dir> [--triage-rounds N]."""

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .sarif_bridge import IkosSarifBridge, Finding
from . import context_gen, patch_gen, triage, validator, llm

# Optional OSS-CRS integration — libCRS is injected via PYTHONPATH when SCAR
# runs inside an OSS-CRS environment or via SCAR's own Tekton pipeline (where
# the libCRS_bridge shim intercepts the calls). Falls back gracefully when
# neither is present (e.g. plain CLI usage on a dev machine).
try:
    import libCRS as _crs
except ImportError:
    _crs = None


def _process_file_group(
    findings: list[Finding],
    args: argparse.Namespace,
    scar_dir: Path,
    ikos_findings: list[Finding],
    aux_origins: dict[int, str],
    finding_ids: dict[int, int],
) -> tuple[list[dict], list[dict]]:
    """Process all findings for one source file sequentially.

    Findings run sequentially within each file group so that patch compounding
    can be added later without restructuring: future work will apply each
    accepted patch to a per-file scratchpad before the next finding is
    processed. Today the source on disk is not mutated — each finding reads the
    original file. Sequential order is preserved but compounding is not yet active.

    Returns (accepted, rejected) — both lists carry enough detail for the
    summarise step to explain why each finding was accepted or rejected.
    """
    accepted = []
    rejected = []
    whole_db = scar_dir / "whole_program.db"
    crs_patch_dir = scar_dir / "crs-patches"

    for finding in findings:
        source = finding.file_path
        stem = Path(source).stem
        fid = finding_ids[id(finding)]
        tag = f"[#{fid} {stem}:{finding.line}]"
        origin = "ikos" if finding in ikos_findings else aux_origins.get(id(finding), "llm")

        print(f"\n{tag} ── {finding.rule_id} @ {stem}:{finding.line} [{origin}] ──", flush=True)
        print(f"{tag}   {finding.message}", flush=True)

        trace_dir = scar_dir / "traces" / f"{fid:02d}-{stem}-{finding.line}-{origin}"
        trace_dir.mkdir(parents=True, exist_ok=True)

        print(f"{tag} [1/3] Generating security briefing...", flush=True)
        witness_db = whole_db if whole_db.exists() else scar_dir / f"{stem}.db"
        briefing = context_gen.generate(
            source, args.repo,
            witness_db=witness_db if witness_db.exists() else None,
            finding_line=finding.line,
            tag=tag,
            trace_dir=trace_dir,
        )
        print(f"{tag} [1/3] Briefing ready ({len(briefing)} chars)", flush=True)

        print(f"{tag} [2/3] Synthesising patch...", flush=True)
        patch = patch_gen.generate(finding, briefing, source, trace_dir=trace_dir)
        print(f"{tag} [2/3] Patch ready ({len(patch.splitlines())} lines)", flush=True)

        print(f"{tag} [3/3] Validating patch...", flush=True)
        val = validator.validate(patch, source, repo_root=args.repo)
        if not val.passed:
            reason = f"{val.stage}: {val.detail}"
            print(f"{tag} [skip] Validation failed ({reason})", flush=True)
            rejected.append({
                "finding": finding.__dict__,
                "patch": patch,
                "origin": origin,
                "rejected_at": "validation",
                "reason": reason,
            })
            continue
        print(f"{tag} [3/3] Validation passed — running triage ({args.triage_rounds} rounds)", flush=True)

        result = triage.run(finding, patch, source, args.repo, briefing=briefing, rounds=args.triage_rounds, tag=tag, trace_dir=trace_dir)
        print(f"{tag} [triage] verdict={result.verdict} confidence={result.confidence:.2f} chain={result.chain}", flush=True)

        if result.verdict == "VALID" and result.confidence >= args.min_confidence:
            entry = {"finding": finding.__dict__, "patch": patch, "triage": result.__dict__, "origin": origin}
            accepted.append(entry)
            print(f"{tag} [accept] {result.reason}", flush=True)

            if _crs is not None:
                crs_patch_dir.mkdir(parents=True, exist_ok=True)
                patch_file = crs_patch_dir / f"patch-{stem}-{finding.line}.diff"
                patch_file.write_text(patch)
                _crs.submit("patch", str(patch_file))
        else:
            print(f"{tag} [reject] {result.reason}", flush=True)
            rejected.append({
                "finding": finding.__dict__,
                "patch": patch,
                "origin": origin,
                "rejected_at": "triage",
                "triage": result.__dict__,
                "reason": result.reason,
            })

    return accepted, rejected


def main() -> None:
    parser = argparse.ArgumentParser(prog="scar", description="SCAR — Static C Analysis & Repair")
    parser.add_argument("--version", action="version", version="scar 0.1.0")
    parser.add_argument("sarif", help="Path to IKOS SARIF output file")
    parser.add_argument("repo", help="Repository root directory")
    parser.add_argument("--triage-rounds", type=int, default=3)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--max-workers", type=int, default=4,
                        help="Max concurrent file-group workers in the repair loop (default: 4)")
    parser.add_argument("--output", default="scar-results.json")
    args = parser.parse_args()

    # ── OSS-CRS registration ─────────────────────────────────────────────────
    scar_dir = Path(args.repo) / ".scar"
    scar_dir.mkdir(parents=True, exist_ok=True)
    crs_patch_dir = scar_dir / "crs-patches"

    if _crs is not None:
        _crs.register_submit_dir("patch", str(crs_patch_dir))
        _crs.register_fetch_dir("bug-candidate", str(scar_dir))

    # ── IKOS findings ────────────────────────────────────────────────────────
    bridge = IkosSarifBridge(args.sarif, args.repo)
    if not bridge.sarif_path.exists():
        ikos_findings = []
        print(f"[scar] IKOS SARIF not found — skipping IKOS findings", flush=True)
    else:
        ikos_findings = bridge.parse()
        print(f"[scar] {len(ikos_findings)} finding(s) from IKOS", flush=True)

    # Build a per-file line index for ±3-line sliding-window deduplication.
    ikos_map: dict[str, list[int]] = {}
    for f in ikos_findings:
        norm = str(Path(f.file_path).resolve())
        ikos_map.setdefault(norm, []).append(f.line)

    def _near_ikos(file_path: str, line: int, radius: int = 3) -> bool:
        norm = str(Path(file_path).resolve())
        return any(abs(line - l) <= radius for l in ikos_map.get(norm, []))

    # ── Auxiliary findings (any task writing .scar/findings-<name>.json) ────
    aux_findings: list[Finding] = []
    aux_origins: dict[int, str] = {}  # id(finding) -> tool name extracted from filename
    for findings_file in sorted(scar_dir.glob("findings-*.json")):
        # Extract the tool name from "findings-<tool>.json" so it survives into
        # the accepted-patch record and is counted correctly by tool_diversity.
        tool_name = findings_file.stem[len("findings-"):]  # e.g. "fuzzer", "llm-scan"
        try:
            raw = json.loads(findings_file.read_text())
            before = len(aux_findings)
            for item in raw:
                if _near_ikos(item["file_path"], item["line"]):
                    continue  # IKOS already covers this location (within ±3 lines)
                f = Finding(
                    rule_id=item["rule_id"],
                    severity=item["severity"],
                    file_path=item["file_path"],
                    line=item["line"],
                    column=item.get("column", 0),
                    message=item["message"],
                )
                aux_findings.append(f)
                aux_origins[id(f)] = tool_name
            added = len(aux_findings) - before
            print(f"[scar] {added} finding(s) from {findings_file.name}", flush=True)
        except Exception as exc:
            print(f"[scar] warning: could not load {findings_file.name}: {exc}", flush=True)

    if not list(scar_dir.glob("findings-*.json")):
        print(f"[scar] no auxiliary findings files found in .scar/", flush=True)

    all_findings = ikos_findings + aux_findings

    # Assign stable sequential IDs before grouping so every log line for a
    # finding carries the same ID regardless of which worker processes it.
    finding_ids: dict[int, int] = {id(f): n for n, f in enumerate(all_findings, start=1)}

    # Print the finding index up front so IDs can be cross-referenced later.
    print(f"[scar] {len(all_findings)} finding(s) to process:", flush=True)
    for finding in all_findings:
        fid = finding_ids[id(finding)]
        origin = "ikos" if finding in ikos_findings else aux_origins.get(id(finding), "llm")
        print(
            f"  #{fid:<3} {finding.rule_id:<12} "
            f"{Path(finding.file_path).name}:{finding.line:<6} [{origin}]",
            flush=True,
        )

    # ── Parallel repair loop ─────────────────────────────────────────────────
    # Group findings by resolved file path. All findings for the same file run
    # sequentially in one worker (patch-compounding order); different files run
    # concurrently across workers. Max workers is capped at --max-workers (default 4)
    # so the loop stays well-behaved on large projects with many source files.
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for finding in all_findings:
        key = str(Path(finding.file_path).resolve())
        by_file[key].append(finding)

    n_workers = min(args.max_workers, len(by_file)) if by_file else 1
    print(
        f"[scar] {len(by_file)} file group(s) → {n_workers} concurrent worker(s)",
        flush=True,
    )

    accepted: list[dict] = []
    rejected: list[dict] = []

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(
                _process_file_group,
                findings, args, scar_dir, ikos_findings, aux_origins, finding_ids,
            ): path
            for path, findings in by_file.items()
        }
        for future in as_completed(futures):
            try:
                a, r = future.result()
                accepted.extend(a)
                rejected.extend(r)
            except Exception as exc:
                print(f"[scar] worker error for {futures[future]}: {exc}", flush=True)

    Path(args.output).write_text(json.dumps(accepted, indent=2))
    rejected_file = scar_dir / "scar-rejected.json"
    rejected_file.write_text(json.dumps(rejected, indent=2))
    print(f"\n[scar] {len(accepted)} patch(es) accepted → {args.output}", flush=True)
    print(f"[scar] {len(rejected)} finding(s) rejected → {rejected_file}", flush=True)

    usage = llm.get_usage()
    # Merge token counts from parallel tasks that ran in separate containers.
    for partial in sorted(scar_dir.glob("token-usage-*.json")):
        try:
            p = json.loads(partial.read_text())
            usage["prompt_tokens"]     += p.get("prompt_tokens", 0)
            usage["completion_tokens"] += p.get("completion_tokens", 0)
            usage["total_tokens"]      += p.get("total_tokens", 0)
            print(f"[scar] merged tokens from {partial.name}", flush=True)
        except Exception as exc:
            print(f"[scar] warning: could not merge {partial.name}: {exc}", flush=True)

    if usage["total_tokens"]:
        print(
            f"[scar] tokens (all tasks): {usage['prompt_tokens']:,} prompt + "
            f"{usage['completion_tokens']:,} completion = "
            f"{usage['total_tokens']:,} total",
            flush=True,
        )

    token_file = scar_dir / "token-usage.json"
    token_file.write_text(json.dumps(usage))

    sys.exit(0 if accepted else 1)


if __name__ == "__main__":
    main()
