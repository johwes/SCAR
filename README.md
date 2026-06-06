# SCAR — Static C Analysis & Repair

Autonomous CVE scanning and patch generation for C codebases, orchestrated via OpenShift Pipelines (Tekton).

## Architecture

```
C source (x86)
  └─> build system detection → compile_commands.json  [Task: build-bitcode]
  └─> clang -emit-llvm → .bc                          [Task: build-bitcode]
        ├─> IKOS → SARIF + output.db          [Task: ikos-analyze]  ─┐
        └─> LLM vulnerability scan            [Task: llm-scan]      ─┤ (parallel)
                                                                      ↓
              Stage 1: Context Gen + IKOS witness trace  ─┐  [Task: repair-loop]
              Stage 2: Patch Gen                          ─┤
              Stage 3: Validate + Triage                  ┘
                    └─> accepted patches              [Task: submit-results]
```

`ikos-analyze` and `llm-scan` run in parallel after bitcode compilation. The repair
loop merges all finding sources, deduplicates with a ±3-line sliding window, and
drives each finding through the three-stage LLM repair pipeline.

## Components

| Module | Role |
|---|---|
| `scar/sarif_bridge.py` | Parses IKOS SARIF output into structured findings |
| `scar/ikos_witness.py` | Queries IKOS `output.db` (SQLite) for counterexample witness traces |
| `scar/context_gen.py` | Security briefing per file, enriched with grep results and IKOS witness traces |
| `scar/vuln_scan.py` | LLM-driven vulnerability discovery (nano-analyzer Stage 2) |
| `scar/scan_cmd.py` | Entry point for the `scar-llm-scan` Tekton task |
| `scar/patch_gen.py` | Synthesises a unified diff patch via LLM |
| `scar/triage.py` | Multi-round skeptical triage + Arbiter verdict (nano-analyzer Stage 3) |
| `scar/validator.py` | Enforces MISRA safety rules and verifies compilation via `compile_commands.json` |
| `scar/grep_tool.py` | Agentic grep — lets the LLM resolve `#define` constants across the repo |
| `scar/llm.py` | OpenAI-compatible client (LiteLLM, OpenAI, OpenRouter, vLLM) |

## Two scanning approaches

### IKOS static analysis (sound)

[NASA IKOS](https://github.com/NASA-SW-VnV/ikos) uses abstract interpretation to *prove* a bug is definitely present — no false positives for the checkers it runs.

| Checker | CWE | Description |
|---|---|---|
| `boa` | CWE-121, CWE-125 | Buffer overflow / out-of-bounds array access |
| `dbz` | CWE-369 | Divide by zero |
| `nullity` | CWE-476 | Null pointer dereference |
| `uva` | CWE-457 | Read of uninitialized variable |
| `sio` | CWE-190 | Signed integer overflow (undefined behaviour in C) |
| `dfa` | CWE-415, CWE-416 | Double free and use-after-free |

cppcheck runs alongside IKOS as a supplementary pass (output stored as `.scar/cppcheck.xml`).

IKOS also writes `output.db` — a SQLite database containing the abstract interval
state at each flagged statement. SCAR reads this via `ikos_witness.py` and injects
the counterexample trace (checker, status, call context) into the context generation
prompt, giving the LLM proven execution data rather than requiring it to re-derive
the path from source alone.

### LLM vulnerability scan (broad)

Inspired by [nano-analyzer](https://github.com/weareaisle/nano-analyzer), the LLM scan
runs independently on every C file, hunting for bug classes IKOS cannot model: string
function overflows, type confusion, logic errors, and protocol-level bugs. Findings are
few-shot prompted, then deduplicated against IKOS results before entering the repair loop.
Each accepted patch is tagged `[ikos]` or `[llm]` to indicate its origin.

## Build system support

`build-bitcode` automatically detects the project's build system and generates
`compile_commands.json` so the validator uses exact per-file compiler flags:

| Detected file | Action |
|---|---|
| `CMakeLists.txt` | `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` (no full build needed) |
| `Makefile` | `bear -- make` intercepts compiler calls |
| `build.sh` | OSS-Fuzz compatible — runs with `bear` and standard env vars (`$CC`, `$CFLAGS`, `$SRC`, `$WORK`, `$OUT`) |
| none | Falls back to `-I <source_parent>` heuristic |

This enables SCAR to scan any project in the [OSS-Fuzz](https://github.com/google/oss-fuzz)
corpus (1,000+ open-source C projects) without per-project configuration.

## Pluggable findings convention

Any Tekton task can contribute findings to the repair loop by writing a file matching
`.scar/findings-<name>.json` using this schema:

```json
[
  {
    "rule_id": "CWE-121",
    "severity": "high",
    "file_path": "/workspace/source/src/input.c",
    "line": 12,
    "column": 0,
    "message": "strcpy into fixed-size buffer without length check"
  }
]
```

The repair loop discovers all `findings-*.json` files automatically — no Python changes
needed when a new analyzer or fuzzer is added to the pipeline. To add a new tool:

1. Write a Tekton Task that produces `.scar/findings-<name>.json`
2. Add it to `pipeline.yaml` with `runAfter: [build-bitcode]`
3. Add it to the `runAfter` list on `repair-loop`

## What SCAR does not find

- **String function overflows via IKOS** — IKOS does not model string lengths; `strcpy` into a fixed buffer won't be flagged by the static checker. The LLM scan covers this gap.
- **Race conditions / TOCTOU** (CWE-362) — outside both IKOS and the LLM scan's reliable coverage.
- **Cross-platform / non-x86 targets** — SCAR compiles and analyses x86 bitcode only.

## Test corpus

[`johwes/scar-test-c`](https://github.com/johwes/scar-test-c) — two test targets:

### Single-file (root of repo)

Minimal C files, one vulnerability each, covering all active checkers:

| File | CWE | IKOS | LLM scan |
|---|---|---|---|
| `bof.c` | CWE-121 (`strcpy`) | not detected | detected |
| `oob_read.c` | CWE-125 | `boa` | detected |
| `divzero.c` | CWE-369 | `dbz` | detected |
| `nullderef.c` | CWE-476 | `nullity` | detected |
| `uninit.c` | CWE-457 | `uva` | detected |
| `signedoverflow.c` | CWE-190 | `sio` | detected |
| `doublefree.c` | CWE-415 | `dfa` | detected |

### Multi-file (`multifile/`)

Three source files sharing a common header (`include/common.h`), built via an
OSS-Fuzz `build.sh`. Tests the full build system detection and `compile_commands.json`
pipeline — without the `-Iinclude` flag captured by bear, all three patches would fail
compilation.

| File | CWE | IKOS | LLM scan |
|---|---|---|---|
| `src/input.c` | CWE-121 (`strcpy`) | not detected | detected |
| `src/process.c` | CWE-476 | `nullity` | detected |
| `src/output.c` | CWE-369 | `dbz` | detected |

## Configuration

Create a Kubernetes secret with your OpenAI-compatible LLM endpoint:

```bash
oc create secret generic scar-llm-credentials \
  --from-literal=base_url="https://your-llm-endpoint/v1" \
  --from-literal=api_key="sk-your-api-key" \
  --from-literal=model="your-model-name"
```

Any OpenAI-compatible endpoint works: LiteLLM proxy, OpenAI, OpenRouter, vLLM, etc.

## Running the Tekton Pipeline

```bash
# Apply all tasks and the pipeline
oc apply -f pipeline/tasks/
oc apply -f pipeline/pipeline.yaml

# Create a PVC for the shared workspace
oc apply -f pipeline/pvc.yaml

# Build and push container images
podman build -t quay.io/jwesterl/scar-ikos:latest containers/ikos/
podman push quay.io/jwesterl/scar-ikos:latest

podman build -t quay.io/jwesterl/scar-agent:latest containers/scar/
podman push quay.io/jwesterl/scar-agent:latest

# Run against the single-file test corpus
tkn pipeline start scar \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --workspace name=shared-data,claimName=scar-pvc \
  --showlog

# Run against the multi-file OSS-Fuzz example
tkn pipeline start scar \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --param source-dir=multifile \
  --workspace name=shared-data,claimName=scar-pvc \
  --showlog
```

## Quick Start (CLI)

```bash
pip install -e .

scar results.sarif /path/to/repo \
  --triage-rounds 5 \
  --min-confidence 0.6 \
  --output patches.json
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## Improvements roadmap

See [`IMPROVEMENTS.md`](IMPROVEMENTS.md) for a prioritised list of enhancements from
low-hanging fruit (IKOS witness traces, macro expansion) through medium effort (caller
context injection, RAG over accepted patches, parallel repair loop) to higher complexity
(program slicing, cross-file data flow, reachability filtering).

## Benchmarks

See [`benchmarks/juliet/`](benchmarks/juliet/README.md) for setup instructions for the NIST Juliet Test Suite (CWE-121, CWE-122, CWE-476).
