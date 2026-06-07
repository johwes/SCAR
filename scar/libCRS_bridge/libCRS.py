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
    """Intercept a libCRS submission and route it based on data_type.

    bug-candidate / pov:
        Translate the OSS-CRS vulnerability payload to SCAR's findings schema
        and drop it in .scar/ so the repair-loop picks it up automatically.

    patch:
        SCAR itself calls this after accepting a patch. In Tekton mode the
        patch file is already on the shared PVC; we just log the call.
        In a real OSS-CRS environment the framework replaces this shim and
        handles the upload to the CRS ensemble.
    """
    if data_type == "patch":
        print(f"[libCRS] patch submitted: {file_path}")
        return

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
    """Register a directory to receive artifacts synced by the CRS ensemble.

    In a real OSS-CRS environment the framework monitors this directory and
    populates it with artifacts submitted by other tools. In Tekton/bridge
    mode we just ensure the directory exists — parallel tasks already write
    findings-*.json directly into .scar/, so SCAR finds them via its normal
    glob without any additional sync step.
    """
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
