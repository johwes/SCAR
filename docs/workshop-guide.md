# SCAR Workshop & Challenge Guide

This document has two parts with different goals:

**Part 1 — Guided learning (everyone follows together).** You will run SCAR on
a known target, observe how the pipeline evolves across versions, and learn to
read the internal traces that explain every decision SCAR makes. By the end you
will have a working mental model of all three repair stages.

**Part 2 — The challenge (teams compete).** The guided portion ends. You have a
real target, a scoring system, and a set of extension tasks. How many accepted
patches can your team produce? Who wires a new scanner first? The leaderboard is
live.

---

## Prerequisites

Before the workshop starts, verify you have:

```bash
tkn version          # Tekton CLI
kubectl version      # cluster access
echo $LLM_BASE_URL   # LLM endpoint configured
echo $LLM_API_KEY
```

The following environment variables must be set for the pipeline to reach the
LLM. Your instructor will provide the values:

```
LLM_BASE_URL   — OpenAI-compatible endpoint base URL
LLM_API_KEY    — API key (may be "dummy" for local vLLM)
LLM_MODEL      — model name to use
```

All tasks write to a shared PVC. Apply it once per namespace if it does not
already exist:

```bash
kubectl apply -f pipeline/pvc.yaml
```

---

## Part 1 — Guided Learning

### Step 1: Run scar-v1 (LLM-only)

scar-v1 compiles the target to LLVM bitcode, then runs the LLM vulnerability
scanner. No static analysis. This is the baseline — an LLM reading source code
and identifying potential bugs without any formal analysis backing it up.

```bash
tkn pipeline start scar-v1 \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --param triage-rounds=3 \
  --param min-confidence=0.6 \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

While it runs, watch the `[llm-scan]` output. You will see each file being
scanned and how many findings the LLM reports per file.

When the run finishes, note:
- How many findings were accepted (the final `[scar] N patch(es) accepted` line)
- Which files produced findings
- The total token usage

Keep this number. You will compare it in Step 2.

---

### Step 2: Run scar-v2 (IKOS + LLM + cppcheck)

scar-v2 adds sound static analysis. IKOS performs whole-program abstract
interpretation across all linked bitcode modules; cppcheck runs a complementary
intra-procedural pass. All three scanners run in parallel after the bitcode is
built. The repair loop merges their findings and deduplicates overlapping
locations.

```bash
tkn pipeline start scar-v2 \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --param triage-rounds=3 \
  --param min-confidence=0.6 \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

When the run finishes, compare with Step 1:
- Did IKOS find bugs the LLM missed? Look for `[origin] = ikos` in the finding
  list printed at startup.
- Did cppcheck add anything? Look for `origin = cppcheck`.
- Did the LLM scan findings change? The same files are scanned — but the
  briefing now includes IKOS witness information, which changes how the LLM
  reasons about the code.

**Discussion point:** Sound static analysis is not about finding *more* bugs —
it is about finding bugs with a *proof*. IKOS's buffer-overflow checker (`boa`)
does not guess; it proves, via abstract interpretation, that a memory access can
exceed its allocation. That proof is what makes the finding actionable without
manual review.

---

### Step 3: Trace Inspection

Every finding SCAR processes writes a trace directory under `.scar/traces/` on
the shared PVC. Each trace contains the full prompts and responses for all three
LLM stages: context generation, patch synthesis, and triage. Reading a trace is
the fastest way to understand *why* a specific patch was accepted or rejected.

#### Launch the inspector pod

```bash
kubectl apply -f docs/scar-inspector-pod.yaml
kubectl wait --for=condition=Ready pod/scar-inspector --timeout=60s
kubectl exec -it scar-inspector -- bash
```

#### Inside the pod

```bash
# List all trace directories — one per finding processed
ls /workspace/source/.scar/traces/

# Each directory is named: <id>-<stem>-<line>-<origin>
# Example: 01-parse-46-ikos
cd /workspace/source/.scar/traces/

# List the stage files for one finding
ls 01-*/
```

Each trace directory contains up to four files:

| File | Contents |
|---|---|
| `1-context-gen.md` | Security briefing: what the LLM was told about the file's architecture |
| `2-patch-gen.md` | The system + user prompt sent to the patch model, and the raw diff it produced |
| `2-patch-gen-structured.md` | If the first patch failed validation, the structured-output retry attempt |
| `3-triage-N.md` | One file per triage round — the judge's reasoning and verdict |
| `4-arbiter.md` | Final verdict: VALID or INVALID, confidence score, reason |

#### Reading an accepted finding

```bash
# Look at the briefing — what context did the LLM get?
cat 01-*/1-context-gen.md | head -60

# Look at the patch — what did the model produce?
cat 01-*/2-patch-gen.md

# Look at the triage rounds — how confident was the judge?
cat 01-*/3-triage-*.md

# Final verdict
cat 01-*/4-arbiter.md
```

#### Reading a rejected finding

Find a trace directory where `4-arbiter.md` shows `INVALID` (or where
`4-arbiter.md` does not exist — meaning the patch failed validation before
triage even started):

```bash
# If 4-arbiter.md is absent, the patch failed the validator.
# Check the patch that was produced:
cat 02-*/2-patch-gen.md   # look at the raw diff

# If a structured retry happened:
cat 02-*/2-patch-gen-structured.md

# If 4-arbiter.md exists but shows INVALID:
cat 02-*/4-arbiter.md     # read the rejection reason
```

**Exercise:** Find one accepted and one rejected finding. For each, identify
the stage where the outcome was determined (context generation, patch synthesis,
validation, or triage) and write one sentence explaining why.

#### Exit the inspector pod

```bash
exit
kubectl delete pod scar-inspector
```

---

## Part 2 — Real Target: Scarnet

You have seen SCAR work on a synthetic test target. Now run it on scarnet — the
actual competition codebase. This is a C application with real, intentional
vulnerabilities of varying complexity.

```bash
tkn pipeline start scar-v2 \
  --param repo-url=<scarnet-repo-url> \
  --param triage-rounds=3 \
  --param min-confidence=0.6 \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

Your instructor will provide the scarnet repo URL.

While the pipeline runs, prepare for the debrief:
- How many findings does scar-v2 report?
- Which files are flagged?
- Which origins appear — ikos, cppcheck, llm-scan?

After the run, inspect the results:

```bash
# Accepted patches
cat /workspace/source/scar-results.json | python3 -m json.tool | head -80

# Rejected findings and why
cat /workspace/source/.scar/scar-rejected.json | python3 -m json.tool
```

---

## Part 3 — The Challenge

**The guided portion is over. From here, your team works independently.**

The scoring system is live. Every accepted patch your team produces earns
points. The leaderboard updates in real time on the competition dashboard.
Your instructor will provide the dashboard URL.

### Scoring

| Achievement | Points |
|---|---|
| Each accepted patch (VALID, confidence ≥ 0.6) | 10 pts |
| Each unique tool added to the pipeline | 15 pts |
| Accepted patch from a tool your team wired in | 25 pts |
| Highest triage confidence score across all teams | 10 pts bonus |
| Finding accepted that no other team found | 20 pts bonus |

### Starting point: Add Semgrep (guided)

Semgrep is a fast, pattern-based static analyzer with a large community C
ruleset. It is the lowest-friction scanner to add because it outputs JSON
natively and requires no new container image.

Your task: write a `scar-semgrep` Tekton task that:
1. Runs Semgrep against the scarnet source directory
2. Writes its findings to `.scar/findings-semgrep.json` in the format the
   repair loop expects
3. Runs in parallel with IKOS after `build-bitcode` in `pipeline-v3-extended.yaml`

**Findings JSON format** (every field is required):

```json
[
  {
    "rule_id":   "CWE-121",
    "severity":  "high",
    "file_path": "/workspace/source/parse.c",
    "line":      46,
    "column":    0,
    "message":   "description of the finding"
  }
]
```

**Hints:**
- Semgrep is installed in the `scar-agent` image. You do not need a new
  container.
- `semgrep --config=p/c --json $SRC` outputs a JSON object. The findings are
  under the `results` key. Each result has `path`, `start.line`, `check_id`,
  and `extra.message`.
- Look at `pipeline/tasks/cppcheck.yaml` for the pattern: one step runs the
  tool, a second step converts the output to the findings schema.
- Add your task to the `runAfter: [build-bitcode]` fan-out in
  `pipeline-v3-extended.yaml` and add it to the `repair-loop` runAfter list.

Test by applying your task and re-running the pipeline:

```bash
kubectl apply -f pipeline/tasks/semgrep.yaml
tkn pipeline start scar-v3 \
  --param repo-url=<scarnet-repo-url> \
  --param triage-rounds=3 \
  --param min-confidence=0.6 \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

Success: you should see `[scar] N finding(s) from findings-semgrep.json` in
the repair-loop output.

---

### Advanced track: Wire the fuzzing harness

Scarnet ships with a libFuzzer harness. It is not connected to the pipeline.
Your task: make it produce accepted patches.

The fuzzer harness is in the scarnet repository. The pipeline has a stub task
at `pipeline/tasks/scar-stub-fuzzer.yaml` that currently writes an empty
findings file. Replace the stub with a real task that:

1. Compiles the target with ASan + fuzzer instrumentation
2. Runs the harness for a bounded time window (e.g. 120 seconds)
3. Collects crash inputs
4. Runs each crash through an ASan build to get a stack trace
5. Converts each unique crash to a finding entry in `.scar/findings-fuzzer.json`

The crash-to-finding conversion is the key step. An ASan crash trace tells you
the file and line of the memory error — that becomes `file_path` and `line` in
the findings schema. The error type (heap-buffer-overflow, stack-buffer-overflow,
etc.) maps to a CWE ID.

**There is no template for this step.** That is intentional — figuring out the
conversion is part of the challenge. The cppcheck converter in
`pipeline/tasks/cppcheck.yaml` and the IKOS SARIF bridge in
`scar/sarif_bridge.py` are both reference implementations of the same pattern.

---

### Open sandbox

Teams that complete both tracks can go further:

- **Tune Semgrep rules**: which community rulesets fire on scarnet? Can you
  write a custom rule that finds something the community rules miss?
- **Add Clang Static Analyzer**: `clang --analyze` is already in the IKOS
  container. The output is `.plist` format. Can you wire it in?
- **Improve triage confidence**: edit `scar/triage.py` prompts to reduce
  false-positive rejections. More accepted patches means more points.
- **Prompt engineering**: the patch generation prompts are in
  `scar/patch_gen.py`. Can you improve patch quality for a specific class of
  vulnerability?

---

## Reference

### Useful tkn commands

```bash
# List recent pipeline runs and their status
tkn pipelinerun list

# Stream logs from a specific run
tkn pipelinerun logs <run-name> --follow

# Re-run the last pipeline run with the same parameters
tkn pipeline start scar-v3 --last --showlog

# Describe a failed task to see the error
tkn taskrun describe <taskrun-name>
```

### Findings origins in the repair-loop output

```
origin = ikos       — IKOS abstract interpreter (sound, proof-backed)
origin = cppcheck   — cppcheck intra-procedural analysis
origin = llm-scan   — LLM vulnerability scanner (heuristic)
origin = semgrep    — Semgrep pattern rules (once you wire it in)
origin = fuzzer     — libFuzzer crash (once you wire it in)
```

### Where to look when something goes wrong

| Symptom | Where to look |
|---|---|
| No findings from a tool | Check `.scar/findings-<tool>.json` exists and is non-empty |
| Patch fails validation | `2-patch-gen.md` in the trace — look at the raw diff |
| Patch rejected by triage | `4-arbiter.md` in the trace — read the reason |
| IKOS finds nothing | Check `.scar/sarif/` — are `.sarif` files present and non-empty? |
| Pipeline times out | Add `--pipeline-timeout 3h` to the tkn command |
