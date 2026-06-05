# SCAR — Static C Analysis & Repair

Autonomous CVE scanning and patch generation for C codebases, orchestrated via OpenShift Pipelines (Tekton).

## Architecture

```
C source (x86)
  └─> clang -emit-llvm → .bc          [Task: build-bitcode]
        └─> IKOS → SARIF               [Task: ikos-analyze]
              └─> Stage 1: Context Gen  ─┐
                    └─> Stage 2: Patch   ├ [Task: repair-loop]
                          └─> Stage 3: Triage + Validator ─┘
                                └─> accepted patches        [Task: submit-results]
```

## Components

| Module | Role |
|---|---|
| `scar/sarif_bridge.py` | Parses IKOS SARIF output into structured findings |
| `scar/context_gen.py` | Generates a security briefing before patch synthesis (nano-analyzer Stage 1) |
| `scar/patch_gen.py` | Synthesises a unified diff patch via LLM |
| `scar/triage.py` | Multi-round skeptical triage + Arbiter verdict (nano-analyzer Stage 3) |
| `scar/validator.py` | Enforces MISRA safety rules and verifies compilation |
| `scar/grep_tool.py` | Agentic grep — lets the LLM resolve `#define` constants across the repo |
| `scar/llm.py` | OpenAI-compatible client (works with LiteLLM, OpenAI, OpenRouter, vLLM) |

## What SCAR finds

SCAR uses [NASA IKOS](https://github.com/NASA-SW-VnV/ikos) (abstract interpretation) as its primary scanner.
Unlike pattern-matching tools, IKOS *proves* a bug is definitely present before reporting it — no false positives for the checkers it runs.

Active checkers:

| Checker | CWE | Description |
|---|---|---|
| `boa` | CWE-121, CWE-125 | Buffer overflow / out-of-bounds array access |
| `dbz` | CWE-369 | Divide by zero |
| `nullity` | CWE-476 | Null pointer dereference |
| `uva` | CWE-457 | Read of uninitialized variable |
| `sio` | CWE-190 | Signed integer overflow (undefined behaviour in C) |
| `dfa` | CWE-415, CWE-416 | Double free and use-after-free |

cppcheck runs alongside IKOS as a supplementary pass (output stored as `.scar/cppcheck.xml`).

## What SCAR does not find

- **String function overflows** (CWE-121 via `strcpy`, `strcat`, `gets`, `sprintf`) — IKOS does not model string lengths, so an unchecked `strcpy` into a fixed buffer will not be flagged. Tools like Flawfinder or Clang's `-Wstringop-overflow` cover this gap.
- **Use-after-free / double-free** (CWE-416, CWE-415) — not in the active checker set. The patch generator explicitly bans `malloc`/`free` in generated patches but the scanner does not detect these patterns.
- **Integer overflow leading to allocation undersize** (CWE-190) — not modelled.
- **Race conditions / TOCTOU** (CWE-362) — outside IKOS's analysis model.
- **Format string vulnerabilities** (CWE-134) — not covered by the active checkers.
- **Cross-platform / non-x86 targets** — SCAR compiles and analyses x86 bitcode only; no cross-compilation support.

## Test corpus

[`johwes/scar-test-c`](https://github.com/johwes/scar-test-c) — four minimal C files, one vulnerability each, used to validate the pipeline end-to-end:

| File | CWE | IKOS checker |
|---|---|---|
| `bof.c` | CWE-121 (strcpy) | not detected — illustrates the string-function gap |
| `oob_read.c` | CWE-125 | `boa` |
| `divzero.c` | CWE-369 | `dbz` |
| `nullderef.c` | CWE-476 | `nullity` |
| `uninit.c` | CWE-457 | `uva` |
| `signedoverflow.c` | CWE-190 | `sio` |
| `doublefree.c` | CWE-415 | `dfa` |

## Configuration

Set three environment variables (or mount as a Kubernetes Secret named `scar-llm-credentials`):

```bash
export LLM_BASE_URL=http://localhost:4000/v1   # LiteLLM proxy or any OpenAI-compatible endpoint
export LLM_API_KEY=<key>
export LLM_MODEL=gpt-4o                        # any model name accepted by the endpoint
```

## Quick Start (CLI)

```bash
pip install -e .

scar results.sarif /path/to/repo \
  --triage-rounds 5 \
  --min-confidence 0.6 \
  --output patches.json
```

## Running the Tekton Pipeline

```bash
# Apply tasks and pipeline
kubectl apply -f pipeline/tasks/
kubectl apply -f pipeline/pipeline.yaml

# Create LLM credentials secret
kubectl create secret generic scar-llm-credentials \
  --from-literal=base_url="$LLM_BASE_URL" \
  --from-literal=api_key="$LLM_API_KEY" \
  --from-literal=model="$LLM_MODEL"

# Create a PVC for the shared workspace
kubectl apply -f pipeline/pvc.yaml

# Start a run
tkn pipeline start scar \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --workspace name=shared-data,claimName=scar-pvc \
  --showlog
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## Benchmarks

See [`benchmarks/juliet/`](benchmarks/juliet/README.md) for setup instructions for the NIST Juliet Test Suite (CWE-121, CWE-122, CWE-476).
