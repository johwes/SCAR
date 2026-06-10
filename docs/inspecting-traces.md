# Inspecting SCAR Pipeline Traces

After a pipeline run, the `.scar/traces/` directory on the workspace PVC
contains one folder per finding processed by the repair loop. Each folder holds
one Markdown file per LLM call, showing the exact prompt sent and the raw
response received. Reading these files lets you follow the full reasoning chain
— from source code analysis through patch synthesis to skeptical triage.

---

## What is stored

```
.scar/
  traces/
    01-util-22-ikos/
      1-context-briefing.md    # Stage 1: security briefing + any grep results + IKOS witness trace
      2-patch-gen.md           # Stage 2: patch synthesis
      3-triage-round-1.md      # Stage 3: skeptical reviewer round 1
      3-triage-round-2.md      #          round 2 (if reached)
      3-triage-round-3.md      #          round 3 (if reached)
      4-arbiter.md             #          final verdict
    02-parse-46-llm-scan/
      ...
    06-handler-77-llm-scan/
      ...
```

Directory names follow the pattern `<id>-<file>-<line>-<origin>`.

**Partial traces are normal.** If a patch failed validation, only
`1-context-briefing.md` and `2-patch-gen.md` exist. If triage returned
`INVALID` on round 1, only that round's file and `4-arbiter.md` exist (early
exit saves tokens). The absence of later files tells you exactly where the
pipeline stopped and why.

Each file contains:

- The system prompt (the role the LLM was asked to play)
- The user prompt (source context, finding, prior reasoning, etc.)
- The raw LLM response
- Extra sections where applicable: **Grep Results** (code lookups the LLM
  requested), **IKOS Witness Trace** (the static analyser's execution evidence)

---

## Creating an inspector pod

The workspace PVC is only accessible from within the OpenShift cluster. You
need a pod that mounts the PVC so you can `exec` into it.

**Step 1 — find your PVC name.**

A pipeline run binds the workspace to a PVC. List PVCs in your namespace:

```bash
oc get pvc
```

Or extract the name directly from the PipelineRun:

```bash
oc get pipelinerun <your-run-name> -o jsonpath='{.spec.workspaces[*].persistentVolumeClaim.claimName}'
```

**Step 2 — edit and apply the pod manifest.**

A ready-made manifest is at `docs/scar-inspector-pod.yaml`. Edit the one line
that needs changing:

```bash
# In your editor: replace <YOUR-PVC-NAME> with the actual name
vim docs/scar-inspector-pod.yaml
oc apply -f docs/scar-inspector-pod.yaml
```

Or inline with sed:

```bash
sed 's/<YOUR-PVC-NAME>/my-actual-pvc-name/' docs/scar-inspector-pod.yaml | oc apply -f -
```

**Step 3 — wait for the pod to be ready, then connect.**

```bash
oc wait --for=condition=Ready pod/scar-inspector --timeout=60s
oc exec -it scar-inspector -- bash
```

> **Note on RWO PVCs:** Most pipeline PVCs use `ReadWriteOnce` access mode,
> which means only one pod can mount them at a time. Wait for your pipeline
> run to finish before creating the inspector pod, or you may see the pod
> stuck in `Pending`.

---

## Useful commands inside the pod

```bash
# List all finding trace directories
ls /workspace/source/.scar/traces/

# Read a specific trace — e.g. finding #2, context briefing
cat /workspace/source/.scar/traces/02-parse-46-llm-scan/1-context-briefing.md

# See all final verdicts at a glance
grep -h "VERDICT:" /workspace/source/.scar/traces/*/4-arbiter.md

# See which findings triggered early exit (INVALID round 1)
ls /workspace/source/.scar/traces/*/3-triage-round-2.md 2>/dev/null \
  || echo "all findings exited on round 1"

# See every grep directive the LLM emitted during context generation
grep "GREP:" /workspace/source/.scar/traces/*/1-context-briefing.md

# Compare the arbiter reasoning across all findings
for d in /workspace/source/.scar/traces/*/; do
  echo "=== $d ==="
  cat "$d/4-arbiter.md" 2>/dev/null || echo "(no arbiter — failed validation)"
done

# Read all triage rounds for one finding in order
cat /workspace/source/.scar/traces/06-handler-77-llm-scan/3-triage-round-*.md

# Pretty-print the accepted patches JSON
python3 -m json.tool /workspace/source/.scar/scar-results.json

# See which tools contributed accepted patches
python3 -c "
import json
data = json.load(open('/workspace/source/.scar/scar-results.json'))
for p in data:
    print(p['origin'], p['finding']['rule_id'], p['finding']['file_path'])
"
```

---

## Copying traces to your laptop

If you prefer to read the files locally (e.g. with a Markdown viewer):

```bash
oc cp scar-inspector:/workspace/source/.scar/traces ./scar-traces
```

---

## Cleanup

Delete the pod when you are done — it is safe to delete at any time since the
PVC is mounted read-only and no data is modified.

```bash
oc delete pod scar-inspector
```

---

## Do I need a special tool container?

No. The standard UBI 9 image (`registry.access.redhat.com/ubi9`) used in the
pod manifest has everything required: `bash`, `grep`, `find`, `cat`,
`python3`. The trace files are plain Markdown — no special tooling needed.

Building a dedicated inspector image would only be worth it if you wanted to
add a terminal Markdown renderer (e.g. `glow`) or a JSON query tool (e.g.
`jq`). For a workshop, `cat` and `grep` are sufficient and keep the setup
simple.
