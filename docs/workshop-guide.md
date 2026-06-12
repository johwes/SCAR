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

## Instructor Setup (run before the workshop)

Load the pre-generated traces onto `scar-workshop-pvc` so students can inspect
them during Part 1 without waiting for pipeline runs:

```bash
# From the root of the SCAR repo clone
./scripts/setup-workshop-pvc.sh
```

This creates `scar-workshop-pvc`, starts a temporary loader pod, extracts the
trace archive from `examples/`, and deletes the pod. Takes about 2 minutes.
Requires ReadWriteMany storage in the namespace (CephFS or NFS).

Then apply the workshop inspector pod so it is ready when students reach Step 3:

```bash
oc apply -f docs/scar-workshop-inspector-pod.yaml
```

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

> **Note for students:** Steps 1 and 2 are instructor-led demonstrations.
> Both pipelines have already been run and the traces are pre-loaded on
> `scar-workshop-pvc`. Follow along on the projected screen, then proceed
> to Step 3 for hands-on trace inspection.

---

### Step 1: scar-v1 — LLM-only *(instructor demo)*

scar-v1 compiles the target to LLVM bitcode, then runs the LLM vulnerability
scanner. No static analysis. This is the baseline — an LLM reading source code
and identifying potential bugs without any formal analysis backing it up.

```bash
# Instructor runs — you do not need to run this
tkn pipeline start scar-v1 \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --param triage-rounds=3 \
  --param min-confidence=0.6 \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

While it runs, note:
- How many findings the LLM reports per file (`[llm-scan]` output)
- How many findings are accepted (the final `[scar] N patch(es) accepted` line)
- Which files produced findings
- The total token usage

Keep this number — you will compare it against scar-v2.

**scar-v1 result on scar-test-c:** 2 findings (LLM scan only), both accepted.

---

### Step 2: scar-v2 — IKOS + cppcheck + LLM *(instructor demo)*

scar-v2 adds sound static analysis. IKOS performs whole-program abstract
interpretation across all linked bitcode modules; cppcheck runs a complementary
intra-procedural pass. All three scanners run in parallel after the bitcode is
built. The repair loop merges their findings and deduplicates overlapping
locations.

```bash
# Instructor runs — you do not need to run this
tkn pipeline start scar-v2 \
  --param repo-url=https://github.com/johwes/scar-test-c \
  --param triage-rounds=3 \
  --param min-confidence=0.6 \
  --workspace name=shared-data,claimName=scar-pvc \
  --pipeline-timeout 3h \
  --showlog
```

Compare with Step 1:
- Did IKOS find bugs the LLM missed? Look for `[origin] = ikos` in the finding
  list printed at startup.
- Did cppcheck add anything? Look for `origin = cppcheck`.
- Did the LLM scan findings change? The same files are scanned — but the
  briefing now includes IKOS witness information, which changes how the LLM
  reasons about the code.

**scar-v2 result on scar-test-c:** 7 findings — 5 from IKOS, 1 from cppcheck,
1 from llm-scan. All 7 accepted.

**Discussion point:** Sound static analysis is not about finding *more* bugs —
it is about finding bugs with a *proof*. IKOS's buffer-overflow checker (`boa`)
does not guess; it proves, via abstract interpretation, that a memory access can
exceed its allocation. That proof is what makes the finding actionable without
manual review.

---

### Step 3: Trace Inspection *(hands-on)*

Every finding SCAR processes writes a trace directory for each finding it
processes. Each trace contains the full prompts and responses for all three
LLM stages: context generation, patch synthesis, and triage. Reading a trace is
the fastest way to understand *why* a specific patch was accepted or rejected.

The pre-generated traces for both runs are already loaded on `scar-workshop-pvc`:

```
/workshop/trace-scarv1-scar-test-c/   — 2 findings (LLM-only)
/workshop/trace-scarv2-scar-test-c/   — 7 findings (IKOS + cppcheck + LLM)
```

#### Launch the workshop inspector pod

```bash
oc apply -f docs/scar-workshop-inspector-pod.yaml
oc wait --for=condition=Ready pod/scar-workshop-inspector --timeout=60s
oc exec -it scar-workshop-inspector -- bash
```

#### Inside the pod

```bash
# Compare what each pipeline version found
ls /workshop/trace-scarv1-scar-test-c/
ls /workshop/trace-scarv2-scar-test-c/

# Each directory is named: <id>-<bug-type>-<line>-<origin>
# Origin tells you which tool found it: ikos, cppcheck, or llm-scan

# Inspect traces for one finding — e.g. the IKOS divide-by-zero
cd /workshop/trace-scarv2-scar-test-c/01-divzero-5-ikos/
ls
```

Each trace directory contains up to four files:

| File | Contents |
|---|---|
| `1-context-briefing.md` | Security briefing: what the LLM was told about the file's architecture |
| `2-patch-gen.md` | The system + user prompt sent to the patch model, and the raw diff it produced |
| `2-patch-gen-structured.md` | If the first patch failed validation, the structured-output retry attempt |
| `3-triage-round-N.md` | One file per triage round — the judge's reasoning and verdict |
| `4-arbiter.md` | Final verdict: VALID or INVALID, confidence score, reason |

#### Reading an accepted finding

```bash
cd /workshop/trace-scarv2-scar-test-c/

# Look at the briefing — what context did the LLM get?
cat 01-divzero-5-ikos/1-context-briefing.md | head -60

# Look at the patch — what did the model produce?
cat 01-divzero-5-ikos/2-patch-gen.md

# Look at the triage rounds — how confident was the judge?
cat 01-divzero-5-ikos/3-triage-round-*.md

# Final verdict
cat 01-divzero-5-ikos/4-arbiter.md
```

#### Comparing v1 and v2 for the same bug

The double-free bug appears in both runs — found by llm-scan in v1, by IKOS in v2.
Compare how the briefing and patch differ between the two origins:

```bash
# v1 — found by llm-scan
cat /workshop/trace-scarv1-scar-test-c/01-doublefree-8-llm-scan/1-context-briefing.md | head -40
cat /workshop/trace-scarv1-scar-test-c/01-doublefree-8-llm-scan/2-patch-gen.md

# v2 — found by IKOS (includes witness trace in briefing)
cat /workshop/trace-scarv2-scar-test-c/02-doublefree-8-ikos/1-context-briefing.md | head -40
cat /workshop/trace-scarv2-scar-test-c/02-doublefree-8-ikos/2-patch-gen.md
```

Notice how the IKOS briefing includes a concrete witness trace — IKOS proved the
double-free path is reachable, which changes what context the patch model receives.

**Exercise:** Find one finding that IKOS found in v2 that was not in v1. Read its
`1-context-briefing.md` and `4-arbiter.md`. Write one sentence explaining why IKOS
could find it but the LLM scan alone could not.

#### Exit the inspector pod

```bash
exit
oc delete pod scar-workshop-inspector
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

```
score = (accepted patches × 3) + (unique CWEs × 2) + (tool diversity × 1)
```

| Dimension | What it measures | Multiplier |
|---|---|---|
| Accepted patches | Patches with verdict VALID and confidence ≥ 0.6 | × 3 |
| Unique CWEs | Distinct CWE identifiers across all accepted patches | × 2 |
| Tool diversity | Number of distinct analysis tools that contributed an accepted patch | × 1 |

Tiebreaker: fastest wall-clock pipeline run time.

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
