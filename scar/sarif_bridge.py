"""IKOS SARIF bridge.

Parses the SARIF JSON produced by IKOS and yields structured bug candidates
for downstream LLM processing.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Finding:
    rule_id: str
    severity: str
    file_path: str
    line: int
    column: int
    message: str


class IkosSarifBridge:
    def __init__(self, sarif_path: str | Path, repo_root: str | Path):
        self.sarif_path = Path(sarif_path)
        self.repo_root = Path(repo_root)

    def parse(self, min_level: str = "error") -> list[Finding]:
        """Parse SARIF findings, keeping only those at or above min_level.

        IKOS levels: error (definite unsafe) > warning (over-approximation) > note.
        Default is "error" — only mathematically proven bugs, no false-positive
        warnings from abstract interpretation widening.
        """
        _rank = {"error": 2, "warning": 1, "note": 0, "none": 0}
        threshold = _rank.get(min_level, 2)

        if not self.sarif_path.exists():
            raise FileNotFoundError(f"SARIF report not found: {self.sarif_path}")

        with open(self.sarif_path) as fh:
            data = json.load(fh)

        findings: list[Finding] = []
        for run in data.get("runs", []):
            for result in run.get("results", []):
                level = result.get("level", "warning")
                if _rank.get(level, 0) >= threshold:
                    for finding in self._extract(result):
                        findings.append(finding)
        return findings

    def _extract(self, result: dict) -> list[Finding]:
        rule_id = result.get("ruleId", "unknown")
        level = result.get("level", "warning")
        message = result.get("message", {}).get("text", "")
        findings = []

        for location in result.get("locations", []):
            phys = location.get("physicalLocation", {})
            uri = phys.get("artifactLocation", {}).get("uri", "")
            region = phys.get("region", {})
            findings.append(Finding(
                rule_id=rule_id,
                severity=level,
                file_path=self._resolve(uri),
                line=region.get("startLine", 0),
                column=region.get("startColumn", 0),
                message=message,
            ))
        return findings

    def _resolve(self, uri: str) -> str:
        if uri.startswith("file://"):
            uri = uri[7:]
        if os.path.isabs(uri):
            return uri
        candidate = self.repo_root / uri
        if candidate.exists():
            return str(candidate)
        # IKOS reports paths relative to its working directory (workspace parent)
        candidate = self.repo_root.parent / uri
        if candidate.exists():
            return str(candidate)
        return str(self.repo_root / uri)
