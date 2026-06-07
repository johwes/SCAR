"""libCRS bridge — intercepts OSS-CRS agent API calls and translates findings
to SCAR's pluggable findings schema (.scar/findings-<name>.json).

Inject into any OSS-CRS-compatible tool by prepending this directory to
PYTHONPATH. The tool calls libCRS.submit() as normal; we quietly redirect
the output into the shared Tekton PVC workspace instead of the real CRS sidecar.

Output path is taken from the SCAR_WORKSPACE environment variable (set by the
Tekton task to the workspace path) so the bridge works regardless of the
tool's working directory.
"""

import json
import os
import time
from pathlib import Path


def _scar_dir() -> Path:
    ws = os.environ.get("SCAR_WORKSPACE", ".")
    d = Path(ws) / ".scar"
    d.mkdir(parents=True, exist_ok=True)
    return d


def submit(data_type: str, file_path: str) -> None:
    """Intercept a bug-candidate or pov submission and write findings-*.json."""
    if data_type not in ("bug-candidate", "pov"):
        print(f"[libCRS] ignoring non-vulnerability submission: {data_type}")
        return

    print(f"[libCRS] intercepted {data_type} from {file_path}")
    try:
        oss_data = json.loads(Path(file_path).read_text())
    except Exception as exc:
        print(f"[libCRS] could not read payload {file_path}: {exc}")
        return

    findings = []
    for bug in oss_data.get("vulnerabilities", []):
        findings.append({
            "rule_id":   bug.get("cwe", "CWE-UNKNOWN"),
            "severity":  bug.get("severity", "high"),
            "file_path": bug.get("file", "unknown.c"),
            "line":      int(bug.get("line", 0)),
            "column":    0,
            "message":   bug.get("description", "Vulnerability found by OSS-CRS tool"),
        })

    out = _scar_dir() / f"findings-osscrs-{int(time.time())}.json"
    out.write_text(json.dumps(findings, indent=2))
    print(f"[libCRS] {len(findings)} finding(s) → {out}")


def register_submit_dir(data_type: str, path: str) -> None:
    """Stub — OSS-CRS agents call this to register output directories."""
    print(f"[libCRS] register_submit_dir({data_type}, {path})")
    Path(path).mkdir(parents=True, exist_ok=True)


def register_shared_dir(name: str, path: str) -> None:
    """Stub — OSS-CRS agents call this to share directories across ensemble."""
    print(f"[libCRS] register_shared_dir({name}, {path})")


def register_fetch_dir(data_type: str, path: str) -> None:
    """Stub — OSS-CRS agents call this to receive artifacts from the ensemble."""
    print(f"[libCRS] register_fetch_dir({data_type}, {path})")
    Path(path).mkdir(parents=True, exist_ok=True)


def download_build_output(name: str, dest: str) -> None:
    """Stub — OSS-CRS agents use this to retrieve compiled build artifacts."""
    print(f"[libCRS] download_build_output({name}, {dest}) — no-op in SCAR bridge")


def submit_build_output(name: str, path: str) -> None:
    """Stub — OSS-CRS agents use this to register a build artifact."""
    print(f"[libCRS] submit_build_output({name}, {path}) — no-op in SCAR bridge")


def __getattr__(name: str):
    """Catch-all for any libCRS API not explicitly stubbed above."""
    def _noop(*args, **kwargs):
        print(f"[libCRS] ignored unmapped call: {name}({args}, {kwargs})")
    return _noop
