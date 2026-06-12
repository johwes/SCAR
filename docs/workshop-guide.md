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

The instructor has already run scar-v1 and scar-v2 against a known target
(`scar-test-c`). Pre-generated traces are included in the repo. Extract them
on your laptop:

```bash
tar xf examples/trace-scar-v1-v2-scar-test-c.tar.xz
```

This gives you two directories you can inspect locally — no cluster needed.

---

### Step 1: What did scar-v1 find? (LLM-only)

```bash
ls trace-scarv1-scar-test-c/
```
```
01-doublefree-8-llm-scan
02-bof-6-llm-scan
```

2 findings, both from the LLM scanner. Each directory name encodes:
`<id>-<bug-type>-<line>-<origin>`

---

### Step 2: What did scar-v2 find? (IKOS + cppcheck + LLM)

```bash
ls trace-scarv2-scar-test-c/
```
```
01-divzero-5-ikos
02-doublefree-8-ikos
03-nullderef-7-ikos
04-oob_read-7-ikos
05-signedoverflow-7-ikos
06-uninit-6-ikos
07-bof-6-cppcheck
```

7 findings — 5 from IKOS, 1 from cppcheck, 1 from llm-scan. The LLM scanner
also ran in v2, but its double-free and bof findings were suppressed: IKOS had
already flagged those same locations, so the repair loop skipped the duplicates.

**Discussion point:** Sound static analysis is not about finding *more* bugs —
it is about finding bugs with a *proof*. IKOS's buffer-overflow checker does not
guess; it proves via abstract interpretation that a code path is reachable and
dangerous. That proof is what makes a finding actionable without manual review.

---

### Step 3: Reading a trace

Each trace directory is the complete paper trail for one finding — every prompt,
every model response, every triage round.

```bash
ls trace-scarv2-scar-test-c/02-doublefree-8-ikos/
```
```
1-context-briefing.md  2-patch-gen.md  3-triage-round-1.md
3-triage-round-2.md    3-triage-round-3.md  4-arbiter.md
```

| File | Contents |
|---|---|
| `1-context-briefing.md` | Security briefing generated for the patch model |
| `2-patch-gen.md` | The patch prompt and the raw diff the model produced |
| `2-patch-gen-structured.md` | Structured-output retry (only present if first attempt failed) |
| `3-triage-round-N.md` | One file per triage round — skeptical reviewer reasoning and verdict |
| `4-arbiter.md` | Final verdict: VALID or INVALID, confidence score, one-sentence reason |

The quickest way to understand an outcome is to read `4-arbiter.md` first:

```bash
tail -4 trace-scarv2-scar-test-c/02-doublefree-8-ikos/4-arbiter.md
```
```
VERDICT: VALID
CONFIDENCE: 10
REASON: The patch correctly mitigates the double-free vulnerability by nullifying
the pointer after the first deallocation, which is the standard and safe C idiom
to prevent undefined behavior on subsequent frees.
```

Then read backwards — `3-triage-round-*.md` to see how the reviewer challenged
the patch, `2-patch-gen.md` to see what was produced, `1-context-briefing.md`
to see what context the model was given.

---

### Step 4: The same bug, two origins

The double-free bug appears in both runs. Compare the finding message the patch
model received in each case.

**v1 — LLM scanner** (heuristic, from reading the source):
```
Rule:    Double free vulnerability
Message: Heap pointer `p` is deallocated twice without reinitialization. This
         corrupts heap metadata and can lead to arbitrary code execution or
         denial of service if triggered in a production context.
```

**v2 — IKOS** (proof, from abstract interpretation):
```
Rule:    free
Message: "double free, pointer '(int8_t*)p' points to dynamic memory allocated
         at 'main:5:21', which is already released"
```

The LLM inferred the bug. IKOS proved it — it can name the exact allocation
site (`main:5:21`) because it traced every possible execution path through the
program. Both produce an accepted patch at confidence 10, but IKOS's finding
carries a formal proof where the LLM carries a suspicion.

---

### Step 5: A bug only IKOS found — and a rejected patch

`01-divzero-5-ikos` exists in v2 but has no equivalent in v1. The LLM scanner
never flagged it. IKOS proved that `divide(int a, int b)` can be called with
`b = 0` and that no guard exists on that path.

Read the verdict:

```bash
tail -4 trace-scarv2-scar-test-c/01-divzero-5-ikos/4-arbiter.md
```
```
VERDICT: INVALID
CONFIDENCE: 10
REASON: The patch masks the division-by-zero error by returning a value that
collides with a valid mathematical result, breaking the API contract and enabling
silent failure propagation downstream.
```

The model proposed `if (b == 0) return 0;` — which is wrong because `0` is a
valid result for `divide(0, n)`. The triage reviewer caught it.

**Finding the bug is not the same as fixing it correctly.** The triage stage
exists precisely for this: a finding with a proof is still only a finding. The
patch has to hold up to adversarial scrutiny before SCAR will accept it.

**Discussion questions:**
- Why did the LLM miss divzero in v1 but IKOS found it in v2?
- What makes IKOS's double-free message more useful to the patch model than the LLM's?
- The divzero fix returned `0` — what would a correct fix look like? *(The function
  has no way to signal an error; fixing this properly requires changing the API.)*

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
