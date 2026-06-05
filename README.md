# SCAR — Static C Analysis & Repair

Autonomous CVE scanning and patch generation for C codebases, orchestrated via OpenShift Pipelines (Tekton).

## Architecture

```
C source (x86)
  └─> clang -emit-llvm → .bc              [Task: build-bitcode]
        ├─> IKOS → SARIF                   [Task: ikos-analyze]  ─┐
        └─> LLM vulnerability scan         [Task: llm-scan]      ─┤ (parallel)
                                                                   ↓
              Stage 1: Context Gen  ─┐                    [Task: repair-loop]
              Stage 2: Patch         ├────────────────────────────┘
              Stage 3: Triage + Validator
                    └─> accepted patches                  [Task: submit-results]
```

IKOS and the LLM scan run in parallel after bitcode compilation. The repair-loop
merges both result sets, deduplicates by file+line, and processes each finding
through the three-stage LLM repair pipeline.

## Components

| Module | Role |
|---|---|
| `scar/sarif_bridge.py` | Parses IKOS SARIF output into structured findings |
| `scar/context_gen.py` | Generates a security briefing per file (nano-analyzer Stage 1) |
| `scar/vuln_scan.py` | LLM-driven vulnerability discovery (nano-analyzer Stage 2) |
| `scar/scan_cmd.py` | Entry point for the `scar-llm-scan` Tekton task |
| `scar/patch_gen.py` | Synthesises a unified diff patch via LLM |
| `scar/triage.py` | Multi-round skeptical triage + Arbiter verdict (nano-analyzer Stage 3) |
| `scar/validator.py` | Enforces MISRA safety rules and verifies compilation |
| `scar/grep_tool.py` | Agentic grep — lets the LLM resolve `#define` constants across the repo |
| `scar/llm.py` | OpenAI-compatible client (works with LiteLLM, OpenAI, OpenRouter, vLLM) |

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

### LLM vulnerability scan (broad)

Inspired by [nano-analyzer](https://github.com/weareaisle/nano-analyzer), the LLM scan runs independently on every C file, hunting for bug classes IKOS cannot model: string function overflows, type confusion, logic errors, and protocol-level bugs. Findings are few-shot prompted, then deduplicated against IKOS results before entering the repair loop. Each accepted patch is tagged `[ikos]` or `[llm]` to indicate its origin.

## What SCAR does not find

- **String function overflows via IKOS** — IKOS does not model string lengths; `strcpy` into a fixed buffer won't be flagged by the static checker. The LLM scan covers this gap.
- **Race conditions / TOCTOU** (CWE-362) — outside both IKOS and the LLM scan's reliable coverage.
- **Cross-platform / non-x86 targets** — SCAR compiles and analyses x86 bitcode only.

## Test corpus

[`johwes/scar-test-c`](https://github.com/johwes/scar-test-c) — minimal C files, one vulnerability each, covering all active checkers:

| File | CWE | IKOS | LLM scan |
|---|---|---|---|
| `bof.c` | CWE-121 (`strcpy`) | not detected | detected |
| `oob_read.c` | CWE-125 | `boa` | detected |
| `divzero.c` | CWE-369 | `dbz` | detected |
| `nullderef.c` | CWE-476 | `nullity` | detected |
| `uninit.c` | CWE-457 | `uva` | detected |
| `signedoverflow.c` | CWE-190 | `sio` | detected |
| `doublefree.c` | CWE-415 | `dfa` | detected |

## Configuration

Create a Kubernetes secret with your OpenAI-compatible LLM endpoint:

```bash
oc create secret generic scar-llm-credentials \
  --from-literal=base_url="https://your-llm-endpoint/v1" \
  --from-literal=api_key="sk-your-api-key" \
  --from-literal=model="your-model-name"
```

The three keys (`base_url`, `api_key`, `model`) are required. Any OpenAI-compatible
endpoint works: LiteLLM proxy, OpenAI, OpenRouter, vLLM, etc.

## Running the Tekton Pipeline

```bash
# Apply all tasks and the pipeline
oc apply -f pipeline/tasks/
oc apply -f pipeline/pipeline.yaml

# Create a PVC for the shared workspace
oc apply -f pipeline/pvc.yaml

# Start a run
tkn pipeline start scar \
  --param repo-url=https://github.com/johwes/scar-test-c \
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

## Benchmarks

See [`benchmarks/juliet/`](benchmarks/juliet/README.md) for setup instructions for the NIST Juliet Test Suite (CWE-121, CWE-122, CWE-476).
