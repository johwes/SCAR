# Extending the SCAR Pipeline

This guide gives you the mechanical knowledge to add a new analysis task to the
SCAR pipeline — a fuzzer, a second static analyzer, a custom pattern scanner, or
anything else that produces vulnerability findings.

It covers the workspace layout, the findings schema, a minimal task skeleton, and
the exact edits required to `pipeline.yaml`. It does not tell you what to build.

---

## Why the code lives in two places

You will notice that SCAR has logic in two very different locations:

- **`scar/` Python package** — the repair loop, patch generation, triage, the
  LLM client, the SARIF bridge, the validator
- **`pipeline/tasks/*.yaml`** — Tekton task definitions that contain inline bash
  and Python scripts

This is intentional, and understanding the split will help you decide where your
own work belongs.

### The Brain: `scar/`

The core analysis and repair logic is platform-agnostic. It has no knowledge of
Tekton, Kubernetes, or OpenShift. You can run it on a laptop:

```bash
scar results.sarif /path/to/repo --triage-rounds 5
```

Because it is a normal Python package it can be unit-tested with `pytest`,
imported by other tools, and versioned independently of the pipeline. When you
want to change *how* SCAR analyses or repairs code — the prompts, the triage
logic, the safety rules — you change the Python package and rebuild the
container image.

### The Glue: `pipeline/tasks/`

The Tekton task YAML files are responsible for platform plumbing: mounting the
shared PVC workspace (`$(workspaces.source.path)`), passing pipeline parameters
between steps, and coordinating parallel tasks. The inline scripts in these
files orchestrate external binaries (`llvm-link`, `ikos`, `ikos-report`, `bear`)
that live in the container image and have no place in a general-purpose Python
library.

When you want to *wire a new tool into the pipeline* — point it at the right
workspace paths, make it write a `findings-<name>.json`, add it to
`pipeline.yaml` — you work in the task YAML. The Brain does not need to change
at all.

### The honest exception

The `emit-llvm` step in `build-bitcode.yaml` is a pragmatic exception. It
contains real domain logic — CCDB parsing, build system detection, clang
invocation — that is not Tekton-specific and could live in `scar/build_cmd.py`.
It ended up inline in the YAML early in the project's life and has not been
moved. If you notice this and find it jarring, you are asking the right question.
Real systems have these exceptions; recognising them is part of the job.

### Where your work belongs

| I want to… | Work here |
|---|---|
| Change how patches are generated or validated | `scar/patch_gen.py`, `scar/validator.py` |
| Change how the LLM prompt is structured | `scar/context_gen.py`, `scar/triage.py` |
| Add a new static analyser or fuzzer | New Tekton task YAML + findings schema |
| Change how findings from different tools are merged | `scar/__main__.py` |
| Add a new pipeline parameter or workspace | `pipeline/pipeline.yaml` |

---

## How the pipeline is structured

```
clone
  └─> build-bitcode
          ├─> ikos-analyze   ─┐
          ├─> llm-scan        ├─> repair-loop ─> submit-results
          ├─> osscrs-scan    ─┘
          └─> [your task]   ─┘  ← plug in here
```

All analysis tasks run **in parallel** after `build-bitcode`. The `repair-loop`
waits for all of them before it starts. Your task contributes findings by writing
a file to the shared workspace in the format described below.

---

## The shared workspace

Every task mounts the same PVC at `$(workspaces.source.path)`. In shell scripts
inside task steps this is accessed as:

```bash
WS=$(workspaces.source.path)   # e.g. /workspace/source
SRC="$WS/$(params.source-dir)" # the C source root (usually the repo root)
```

### What is available at each stage

**After `clone`**
```
$WS/                          ← repo root (all source files)
$WS/src/                      ← source files (project-dependent layout)
```

**After `build-bitcode`** — your task can read all of these
```
$WS/.scar/compile_commands.json   ← per-file compiler flags (CCDB)
$WS/.scar/bitcode/                ← LLVM bitcode directory
$WS/.scar/bitcode/src/handler.bc  ← one .bc per compiled source file
$WS/.scar/bitcode/src/parse.bc
$WS/.scar/bitcode/src/session.bc
$WS/.scar/bitcode/src/util.bc
$WS/.scar/oss-out/                ← OSS-Fuzz build outputs (object files, binaries)
```

**After `ikos-analyze`** (parallel — do not depend on this in your task)
```
$WS/.scar/whole_program.bc        ← linked whole-program bitcode
$WS/.scar/whole_program.db        ← IKOS SQLite result database
$WS/.scar/results.sarif           ← merged IKOS SARIF
$WS/.scar/cppcheck.xml            ← cppcheck XML output
```

**What your task must write**
```
$WS/.scar/findings-<name>.json    ← your findings (see schema below)
```

The `repair-loop` discovers every file matching `.scar/findings-*.json`
automatically. The `<name>` can be anything — choose something descriptive
(`findings-afl.json`, `findings-myanalyzer.json`).

---

## The findings schema

Your task must write a JSON array to `$WS/.scar/findings-<name>.json`.
Each entry describes one vulnerability:

```json
[
  {
    "rule_id":   "CWE-121",
    "severity":  "high",
    "file_path": "/workspace/source/src/parse.c",
    "line":      46,
    "column":    0,
    "message":   "Human-readable description passed to the LLM as context"
  }
]
```

| Field | Required | Notes |
|---|---|---|
| `rule_id` | yes | CWE identifier or tool-specific rule name |
| `severity` | yes | `critical`, `high`, `medium`, or `low` |
| `file_path` | yes | Absolute path — use `$WS/...` prefix, not `/tmp/` |
| `line` | yes | 1-based line number |
| `column` | no | Defaults to 0 if omitted |
| `message` | yes | Shown to the LLM — be specific about what is wrong |

**Important:** `file_path` must point into the persistent workspace (`$WS/...`),
not into a temporary directory your tool created. The `repair-loop` runs in a
separate container that has no access to `/tmp` from your step.

If your tool finds nothing, write an empty array `[]` or skip writing the file
entirely. The repair-loop handles both safely.

---

## Minimal task skeleton

Copy this into `pipeline/tasks/scar-<name>.yaml` and fill in the blanks:

```yaml
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: scar-<name>
spec:
  description: >
    One-sentence description of what this task does.
  params:
    - name: source-dir
      description: Path within the workspace containing C source files
      default: "."
  workspaces:
    - name: source
      description: Shared workspace containing the cloned C repository
  steps:
    - name: run
      image: <your-container-image>
      script: |
        #!/bin/bash
        set -euo pipefail
        WS=$(workspaces.source.path)
        SRC="$WS/$(params.source-dir)"
        FINDINGS="$WS/.scar/findings-<name>.json"

        mkdir -p "$WS/.scar"

        # ── your analysis logic here ──────────────────────────────────────
        # Read source from:  $SRC
        # Read bitcode from: $WS/.scar/bitcode/
        # Read CCDB from:    $WS/.scar/compile_commands.json
        # ─────────────────────────────────────────────────────────────────

        # Write findings (empty array if nothing found)
        echo '[]' > "$FINDINGS"
```

A task can have multiple steps. Steps within a task run sequentially and share
the same filesystem — useful if you need a build step before an analysis step.

---

## Wiring your task into the pipeline

Two edits to `pipeline/pipeline.yaml`:

### 1. Add your task to the `tasks` list

Add it at the same level as `ikos-analyze`, `llm-scan`, and `osscrs-scan`:

```yaml
    - name: <name>
      taskRef:
        name: scar-<name>
      runAfter: [build-bitcode]          # runs in parallel with the other analyzers
      workspaces:
        - name: source
          workspace: shared-data
      params:
        - name: source-dir
          value: $(params.source-dir)    # omit if your task has no source-dir param
```

### 2. Add your task to `repair-loop`'s `runAfter`

Find the `repair-loop` task entry and extend its `runAfter` list:

```yaml
    - name: repair-loop
      ...
      runAfter: [ikos-analyze, llm-scan, osscrs-scan, <name>]  # ← add here
```

This ensures the repair loop waits for your findings before it starts processing.

---

## Applying your changes to the cluster

```bash
# Apply the new task definition
oc apply -f pipeline/tasks/scar-<name>.yaml

# Apply the updated pipeline
oc apply -f pipeline/pipeline.yaml

# Run
tkn pipeline start scar \
  --param repo-url=<target-repo> \
  --workspace name=shared-data,claimName=scar-pvc \
  --showlog
```

You do **not** need to rebuild any container image unless your task uses a custom
image that you have built yourself.

---

## What the repair loop does with your findings

For each finding in your `findings-<name>.json`:

1. Deduplicates against findings from other tools (±3-line sliding window)
2. Generates a security briefing from the source file context
3. Asks the LLM to synthesise a patch
4. Validates the patch compiles and passes safety rules
5. Runs multi-round skeptical triage to confirm the patch is correct
6. Writes accepted patches to `.scar/patches/`

The `message` field you write is injected directly into the LLM prompt. A precise,
specific message produces better patches than a generic one.

---

## Using the OSS-CRS libCRS API instead

If your tool already speaks the OSS-CRS `libCRS` API (calls `libCRS.submit(...)`),
you do not need to write `findings-<name>.json` yourself — the libCRS bridge handles
that. See [`docs/osscrs-tool-guide.md`](osscrs-tool-guide.md) for that path.

The Tekton-native approach described in this document is simpler when you are
writing a tool from scratch that has no need for OSS-CRS compatibility.
