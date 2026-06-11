# Extending the SCAR Pipeline

This guide gives you the mechanical knowledge to add a new analysis task to the
SCAR pipeline — a fuzzer, a second static analyzer, a custom pattern scanner, or
anything else that produces vulnerability findings.

It covers the workspace layout, the findings schema, a minimal task skeleton, and
the exact edits required to `pipeline-v2-full.yaml` or `pipeline-v3-extended.yaml`. It does not tell you what to build.

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
`pipeline-v2-full.yaml` or `pipeline-v3-extended.yaml` — you work in the task YAML. The Brain does not need to change
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
| Add a new pipeline parameter or workspace | `pipeline/pipeline-v3-extended.yaml` |

---

## Pipeline variants

Three pipeline definitions ship with SCAR, each building on the previous:

| File | Name | What runs |
|---|---|---|
| `pipeline/pipeline-v1-llm-only.yaml` | `scar-v1` | clone → build-bitcode → llm-scan → repair-loop → submit |
| `pipeline/pipeline-v2-full.yaml` | `scar-v2` | + IKOS static analysis + OSS-CRS scan in parallel |
| `pipeline/pipeline-v3-extended.yaml` | `scar-v3` | + two pre-wired stub slots for you to fill in |

**If you are here to add a new tool, start with v3.** The two stub tasks
(`pipeline/tasks/scar-stub-fuzzer.yaml` and `pipeline/tasks/scar-stub-custom-scan.yaml`)
are already wired into the pipeline and write empty findings files. Replace the
body of either stub with your tool logic, `oc apply` the task YAML, and run
`scar-v3` — no pipeline YAML edits required.

---

## How the pipeline is structured

```
clone
  └─> build-bitcode
          ├─> ikos-analyze       ─┐
          ├─> llm-scan            ├─> repair-loop ─> submit-results
          ├─> osscrs-scan         │
          ├─> fuzzer-stub        ─┤  ← replace with your fuzzer
          └─> custom-scan-stub   ─┘  ← replace with your analyser
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

## Inline Python in a Tekton task step

Tekton task steps have their `script:` field parsed as YAML before the script
runs. This creates a non-obvious constraint when you want to call Python inline.

### The problem: heredocs break YAML

A YAML **literal block scalar** (`script: |`) collects every line that is
indented at least as much as the first content line. Once a line appears at a
*lesser* indentation the YAML parser considers the block finished — and tries to
parse whatever follows as YAML.

A bash heredoc violates this rule: the body and the closing delimiter must sit
at column 0, which ends the YAML block scalar before the YAML parser finishes
reading the script. The result is a parse error:

```
error converting YAML to JSON: yaml: line N: could not find expected ':'
```

This is what a broken heredoc looks like inside a task YAML:

```yaml
    steps:
      - name: run
        script: |
          #!/bin/bash
          python3 - <<'EOF'    # ← this line is fine (12-space indentation)
import json                    # ← column 0: YAML ends the block here
print("hello")                 # ← YAML now tries to parse this as a mapping key
EOF
```

### The fix: `python3 -c "..."` with matching indentation

Embed the Python inside a double-quoted bash string. The trick is that **every
Python line in the YAML file must sit at the same indentation as the rest of the
bash script** (8 spaces in the SCAR tasks). YAML strips that shared indentation
before handing the script to the step runner, so Python receives the code at
0 indentation — exactly where module-level statements must be.

```yaml
    steps:
      - name: run
        image: quay.io/jwesterl/scar-agent:latest
        script: |
          #!/bin/bash
          set -euo pipefail
          WS=$(workspaces.source.path)
          RESULTS="$WS/.scar/scar-results.json"

          python3 -c "
          import json, sys
          from pathlib import Path
          for p in json.load(open(sys.argv[1])):
              print(p['finding']['rule_id'])
          " "$RESULTS"
```

After YAML strips the 8-space block indentation, the step runner sees:

```bash
#!/bin/bash
set -euo pipefail
WS=$(workspaces.source.path)
RESULTS="$WS/.scar/scar-results.json"

python3 -c "
import json, sys
from pathlib import Path
for p in json.load(open(sys.argv[1])):
    print(p['finding']['rule_id'])
" "$RESULTS"
```

Python receives the code with 0-space module-level indentation, which is valid.

### Indentation rules to remember

| What you write in the YAML file | What bash/Python sees |
|---|---|
| 8-space line (bash module level) | 0 spaces — bash top level |
| 8-space Python line (inside `-c "..."`) | 0 spaces — Python module level |
| 12-space Python line (inside a `for`/`if`) | 4 spaces — Python block body |
| 16-space Python line (nested block) | 8 spaces — Python nested body |

### Rules for the Python code inside `python3 -c "..."`

- **Use single quotes** for all Python string literals — the outer bash string
  uses double quotes, so a `"` inside would terminate it prematurely.
- **Pass file paths as arguments** (`sys.argv[1]`) rather than embedding
  `$SHELL_VAR` inside the Python string. Bash expands `$VAR` inside double
  quotes, but injecting a path directly can cause issues if the path contains
  spaces or special characters.
- **No `f-strings` with `{}`** — bash does not expand them, but curly braces
  inside a double-quoted string can confuse readers. Use `str(x)` and
  concatenation, or `'{:.2f}'.format(x)`, instead.

### Validating before applying

Always run a YAML lint pass before `oc apply`:

```bash
python3 -c "import yaml; yaml.safe_load(open('pipeline/tasks/my-task.yaml').read()); print('OK')"
```

This catches block-scalar truncation before the task hits the cluster.

---

## Wiring your task into the pipeline

### Option A — Use the v3 stub slots (recommended)

The v3 pipeline ships with two stub tasks already wired in. Replace the body of
`pipeline/tasks/scar-stub-fuzzer.yaml` or `pipeline/tasks/scar-stub-custom-scan.yaml`
with your tool logic, then apply just the task:

```bash
oc apply -f pipeline/tasks/scar-stub-fuzzer.yaml   # or scar-stub-custom-scan.yaml
```

No pipeline YAML changes needed — the stub is already in `repair-loop`'s
`runAfter` list.

### Option B — Wire a brand-new task from scratch

If you need a third slot, or are working against a different pipeline variant,
make two edits to the pipeline YAML:

**1. Add your task to the `tasks` list** at the same level as `ikos-analyze`:

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

**2. Add your task to `repair-loop`'s `runAfter`:**

```yaml
    - name: repair-loop
      ...
      runAfter: [ikos-analyze, llm-scan, osscrs-scan, fuzzer-stub, custom-scan-stub, <name>]
```

---

## Local development workflow

Iterating via commit → push → `tkn pipeline start` → wait is slow. Two utilities
let you validate your tool locally before touching the cluster.

### 1. Mock the Tekton workspace

`tests/mock_pipeline.sh` recreates the PVC directory layout on your laptop and runs
your scanner against a local copy of the source:

```bash
# Basic: check that your tool writes a valid findings file
tests/mock_pipeline.sh /path/to/scarnet

# End-to-end: also run the SCAR repair loop against your findings
LLM_BASE_URL=... LLM_API_KEY=... LLM_PATCH_MODEL=... LLM_REVIEW_MODEL=... \
  tests/mock_pipeline.sh /path/to/scarnet --run-repair
```

Edit the `Executing student tool` section near the middle of the script to call
your scanner. The script verifies that a `findings-*.json` file was produced,
validates its schema, and (with `--run-repair`) drives the full repair loop so you
can see accepted patches before pushing anything to the cluster.

### 2. Validate the findings schema

```bash
python3 scar/validate_schema.py .scar/findings-myscanner.json
```

This catches the most common integration mistakes before the repair loop silently
drops your findings:

| Problem | Example |
|---|---|
| Missing required field | `"filepath"` instead of `"file_path"` |
| Wrong type for `line` | `"46"` (string) instead of `46` (integer) |
| Invalid `severity` | `"warning"` — must be `critical`, `high`, `medium`, `low`, or `info` |

Pass multiple files at once and the exit code is 1 if any file fails, so it is safe
to use in a pre-commit hook or local CI step:

```bash
python3 scar/validate_schema.py .scar/findings-*.json
```

---

## Applying your changes to the cluster

```bash
# Apply your task definition (always needed)
oc apply -f pipeline/tasks/scar-<name>.yaml

# Apply the pipeline only if you edited pipeline YAML (Option B)
oc apply -f pipeline/pipeline-v3-extended.yaml

# Run
tkn pipeline start scar-v3 \
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
