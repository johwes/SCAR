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

# Start a run
tkn pipeline start scar \
  --param repo-url=https://github.com/your-org/target-repo \
  --workspace name=shared-data,claimName=scar-pvc
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## Benchmarks

See [`benchmarks/juliet/`](benchmarks/juliet/README.md) for setup instructions for the NIST Juliet Test Suite (CWE-121, CWE-122, CWE-476).
