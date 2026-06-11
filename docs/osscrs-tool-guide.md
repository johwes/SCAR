# Building an OSS-CRS Tool for SCAR

This guide walks you through writing a scanner that plugs into the SCAR pipeline
via the OSS-CRS libCRS API. By the end you will have a working tool that:

1. Scans a C source tree for vulnerabilities
2. Reports findings through the libCRS bridge
3. Has those findings picked up automatically by the SCAR repair loop

No changes to SCAR's core code are needed at any step.

---

## How it fits together

```
your tool
  └─> import libCRS
  └─> libCRS.submit("bug-candidate", payload.json)
        └─> bridge intercepts the call
        └─> writes .scar/findings-osscrs-<ts>.json
              └─> repair-loop picks it up automatically
```

The bridge (`scar/libCRS_bridge/libCRS.py`) is a drop-in shim that intercepts
`libCRS` API calls and translates them to SCAR's pluggable findings schema.
Your tool never touches the filesystem directly — it just calls `libCRS.submit`.

---

## The libCRS API

Your tool only needs to call two things:

```python
import libCRS

# Optional — register where you will write bug-candidate payloads.
# The bridge creates the directory; you do not need to do it yourself.
libCRS.register_submit_dir("bug-candidate", "/tmp/my-tool-output")

# Submit a finding. file_path must point to a JSON file you write
# (see payload format below).
libCRS.submit("bug-candidate", "/tmp/my-tool-output/finding-001.json")
```

All other libCRS API calls (`register_shared_dir`, `register_fetch_dir`,
`download_build_output`, etc.) are stubbed — calling them is safe but does nothing.

---

## Bug-candidate payload format

The file you pass to `libCRS.submit` must be a JSON object with a `vulnerabilities`
array. Each entry needs these fields:

```json
{
  "vulnerabilities": [
    {
      "cwe":         "CWE-121",
      "severity":    "high",
      "file":        "/tmp/osscrs-sandbox/src/input.c",
      "line":        12,
      "description": "strcpy into fixed-size buffer without length check"
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `cwe` | yes | CWE identifier, e.g. `"CWE-121"` |
| `severity` | no | `"critical"`, `"high"`, `"medium"`, `"low"` — defaults to `"high"` |
| `file` | yes | Absolute path to the file **inside the sandbox** (`$SANDBOX_SRC/...`) |
| `line` | yes | 1-based line number |
| `description` | no | Human-readable message passed to the LLM as context |

> **Tip:** If your tool scans a file and finds nothing, skip the `libCRS.submit()` call
> entirely. Submitting an empty `{"vulnerabilities": []}` payload writes a redundant
> `findings-osscrs-*.json` file to `.scar/`. The repair loop handles it safely, but
> suppressing empty submissions keeps the workspace clean and avoids unnecessary noise
> in large pipeline runs.

> **Note on file paths:** Your tool runs against a sandbox copy of the source at
> `$SANDBOX_SRC`. Use the sandbox path in `file` — the bridge automatically remaps
> it back to the real PVC path before writing the findings file.

---

## A minimal working scanner

Save this as `my_scanner.py`. It greps for dangerous C functions and reports each
match as a bug-candidate.

```python
#!/usr/bin/env python3
"""Minimal OSS-CRS tool — regex scan for dangerous C functions."""

import json
import os
import re
import tempfile
from pathlib import Path

import libCRS

DANGEROUS = {
    r"\bstrcpy\s*\(":  ("CWE-121", "strcpy into destination without bounds check"),
    r"\bgets\s*\(":    ("CWE-121", "gets reads unbounded input"),
    r"\bsprintf\s*\(": ("CWE-134", "sprintf with potentially unsanitised format string"),
    r"\bstrcat\s*\(":  ("CWE-121", "strcat appends without bounds check"),
}

sandbox = Path(os.environ.get("SANDBOX_SRC", "/tmp/osscrs-sandbox"))
output_dir = Path(tempfile.mkdtemp(prefix="my-scanner-"))

libCRS.register_submit_dir("bug-candidate", str(output_dir))

for c_file in sorted(sandbox.rglob("*.c")):
    for lineno, line in enumerate(c_file.read_text(errors="replace").splitlines(), 1):
        for pattern, (cwe, description) in DANGEROUS.items():
            if re.search(pattern, line):
                payload = output_dir / f"finding-{c_file.stem}-{lineno}.json"
                payload.write_text(json.dumps({
                    "vulnerabilities": [{
                        "cwe":         cwe,
                        "severity":    "high",
                        "file":        str(c_file),
                        "line":        lineno,
                        "description": description,
                    }]
                }))
                libCRS.submit("bug-candidate", str(payload))
                print(f"[my-scanner] {cwe} @ {c_file.name}:{lineno}")
```

---

## Testing locally (no Tekton needed)

You can run and debug your tool entirely on your laptop before touching the pipeline.

```bash
# 1. Clone SCAR so you have the bridge shim
git clone https://github.com/johwes/SCAR /tmp/scar

# 2. Set up a fake workspace and sandbox
export SCAR_WORKSPACE=/tmp/scar-test-ws
export SCAR_SRC=/tmp/scar-test-ws/src          # source-dir inside workspace
export SANDBOX_SRC=/tmp/osscrs-sandbox
export PYTHONPATH=/tmp/scar/scar/libCRS_bridge

mkdir -p "$SCAR_WORKSPACE/.scar" "$SCAR_SRC" "$SANDBOX_SRC"

# 3. Copy some C source into the sandbox (simulating what osscrs-scan does)
cp /path/to/your/test/source/*.c "$SANDBOX_SRC/"
cp /path/to/your/test/source/*.c "$SCAR_SRC/"   # persistent copy for repair loop

# 4. Run your scanner
python3 my_scanner.py

# 5. Inspect the findings written by the bridge
cat "$SCAR_WORKSPACE"/.scar/findings-osscrs-*.json
```

A successful local run produces one or more `findings-osscrs-<timestamp>.json` files
in `$SCAR_WORKSPACE/.scar/`. These have exactly the same format the repair loop reads.

---

## Integrating into the pipeline

### Choose your pattern

| | Pattern A | Pattern B |
|---|---|---|
| **When to use** | Your tool is pip-installable or has light deps | Your tool ships its own container image |
| **How it works** | Install into the scar-agent image | Bridge is injected via PYTHONPATH at runtime |
| **Changes to SCAR** | Build a new image, push it | None — use the upstream image unchanged |

### Pattern A — tool bundled in scar-agent image

Copy `containers/scar/Dockerfile.osscrs-tool.example` and add your install steps:

```dockerfile
FROM quay.io/jwesterl/scar-agent:latest

# Install your tool
RUN pip3 install --no-cache-dir your-tool==1.0.0
# or: COPY my_scanner.py /opt/my-scanner/my_scanner.py
```

Build and push:

```bash
podman build -f containers/scar/Dockerfile.my-scanner \
  -t quay.io/<your-registry>/scar-my-scanner:latest containers/scar/
podman push quay.io/<your-registry>/scar-my-scanner:latest
```

Run the pipeline:

```bash
tkn pipeline start scar \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --param tool-image=quay.io/<your-registry>/scar-my-scanner:latest \
  --param tool-cmd="python3 /opt/my-scanner/my_scanner.py" \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

### Pattern B — unmodified upstream image

If your tool already has a published container image, you do not need to build anything.
The `inject-bridge` step copies the libCRS shim from scar-agent into the shared PVC
and sets `PYTHONPATH` so `import libCRS` resolves inside your tool's container.

Your tool must already call `libCRS.submit("bug-candidate", ...)` — or you wrap it
in a small Python driver script that does.

```bash
tkn pipeline start scar \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --param tool-image=ghcr.io/your-org/your-scanner:latest \
  --param tool-cmd="python3 /opt/driver.py --src \$SANDBOX_SRC" \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

---

## What to look for in the logs

**inject-bridge step:**
```
[inject-bridge] bridge copied to /workspace/source/.scar/libCRS_bridge
```

**run-scanner step (your tool):**
```
[libCRS] register_submit_dir(bug-candidate, /tmp/my-scanner-abc123)
[libCRS] intercepted bug-candidate from /tmp/my-scanner-abc123/finding-input-12.json
[libCRS] 1 finding(s) normalized → /workspace/source/.scar/findings-osscrs-1717743891000000000.json
```

**repair-loop step:**
```
[scar] 3 finding(s) from findings-osscrs-1234567890.json
```

If `findings-osscrs-*.json` appears in the repair-loop logs, your tool is wired up correctly.

---

## Checklist

- [ ] `import libCRS` works locally with `PYTHONPATH` set
- [ ] Local run produces `findings-osscrs-*.json` in `$SCAR_WORKSPACE/.scar/`
- [ ] JSON findings have the correct schema (cwe, file, line fields present)
- [ ] File paths in findings use `$SANDBOX_SRC` prefix (bridge remaps them)
- [ ] Pipeline run shows findings picked up by the repair-loop step

---

## Common mistakes

**`import libCRS` fails** — `PYTHONPATH` is not set to the bridge directory.
In Tekton the `run-scanner` step sets this automatically. Locally, export it manually.

**Findings not picked up by repair-loop** — Check that `file` in the payload uses
the sandbox path (`$SANDBOX_SRC/...`), not a path from your development machine.
The bridge remaps sandbox paths; absolute paths outside the sandbox are used as-is.

**All findings deduplicated away** — SCAR deduplicates findings within ±3 lines
of an existing IKOS or LLM finding. This is expected behaviour — it means IKOS or
the LLM scan already found the same bug. Try a bug class neither covers (logic errors,
protocol issues, API misuse).
