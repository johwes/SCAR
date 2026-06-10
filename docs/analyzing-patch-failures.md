# Analyzing Patch Failures with LLM Traces

SCAR writes a complete trace of every LLM interaction to `.scar/traces/` during
each pipeline run. This document shows how to use those traces to diagnose why
patches fail, using a real audit performed against a production scarnet run
(score=62, 12/15 findings accepted).

---

## The methodology

After a run, identify which findings failed before reaching triage — these are
the patches the validator rejected, and they represent the most actionable
failures because the LLM understood the bug but couldn't produce a valid fix.

**Step 1 — find the rejected findings** (no arbiter file means validation failed):

```bash
# In the traces directory
ls */4-arbiter.md | sed 's|/4-arbiter.md||' | sort > has_arbiter.txt
ls -d */ | tr -d '/' | sort > all_findings.txt
comm -23 all_findings.txt has_arbiter.txt
```

**Step 2 — read the generated diff:**

```bash
grep -A 60 "^## Response" <finding-dir>/2-patch-gen.md
```

**Step 3 — check what the LLM was actually shown:**

```bash
grep -n "strcpy\|malloc\|sprintf\|memcpy" <finding-dir>/1-context-briefing.md
```

This tells you whether the briefing contained the right context, and how many
occurrences of the vulnerable pattern were visible to the model.

**Step 4 — read the briefing** to verify the extracted function context was correct:

```bash
grep -A 80 "^## User" <finding-dir>/1-context-briefing.md | head -90
```

---

## Three failure modes found

### Failure 1 — Hallucinated second hunk (`02-parse-46`)

**Finding:** Unbounded buffer copy into fixed-size key field, `parse.c:46`

**Generated patch (abbreviated):**
```diff
--- a/src/parse.c
+++ b/src/parse.c
@@ -44,2 +44,2 @@
     if (!tok) return -1;
-    strcpy(out->key, tok);
+    strncpy(out->key, tok, sizeof(out->key) - 1); out->key[sizeof(out->key) - 1] = '\0';
@@ -55,2 +55,2 @@
     if (!tok) return -1;
-    strcpy(out->key, tok);
+    strncpy(out->key, tok, sizeof(out->key) - 1); out->key[sizeof(out->key) - 1] = '\0';
```

**What the briefing showed:** One `strcpy(out->key, tok)` occurrence (line 33
of the briefing file). No second occurrence existed in the code.

**Root cause:** The patch system prompt instructed "Fix ALL occurrences in a
single multi-hunk patch." The LLM invented a second hunk at line 55 because
the instruction implied there should be more occurrences to find.

**Fix applied:** Before synthesis, `patch_gen` now counts exact matches for
the vulnerable line in the full source and injects the count into the prompt:

> "The vulnerable line appears exactly once in this file (line 46).
> Generate exactly 1 hunk."

This removes the ambiguity that triggered the hallucination.

---

### Failure 2 — Variable redeclaration across hunks (`04-parse-68`)

**Finding:** Integer wrap-around via atol cast to size_t, `parse.c:68`

**Generated patch (abbreviated):**
```diff
@@ -76,2 +76,4 @@ int parse_cmd(...)
-        out->frag_id = atoi(tok);
+        long val = strtol(tok, NULL, 10);
+        if (val < 0) return -1;
+        out->frag_id = (size_t)val;
@@ -79,2 +79,4 @@
-        out->frag_offset = (size_t)atol(tok);
+        long val = strtol(tok, NULL, 10);   ← redeclaration
+        if (val < 0) return -1;
+        out->frag_offset = (size_t)val;
... (repeated twice more)
```

**What went wrong:** The fix is semantically correct — replacing `atoi`/`atol`
with `strtol` plus a bounds check is the right approach, and there were 4 real
occurrences. But all 4 hunks are in the same function scope, and each declares
`long val`. In C you cannot declare the same variable name multiple times in
the same scope. The recompile step in the validator catches this immediately.

**Root cause:** The LLM applied the fix pattern independently to each
occurrence without considering that they share a function scope.

**Fix applied:** Added to the patch system prompt:

> "When fixing multiple occurrences in the same function, do not re-declare
> the same local variable in each hunk — declare it once before the affected
> block, or use distinct variable names per hunk to avoid C redeclaration
> errors."

---

### Failure 3 — Wrong diff path and malformed hunk count (`12-main-35`)

**Finding:** Format string vulnerability in `scar_log`, `main.c:35`

**Generated patch:**
```diff
--- a/source/main.c      ← wrong: should be src/main.c
+++ b/source/main.c
@@ -33,7 +33,7 @@ ...   ← claims 7 lines, only 4 follow
         size_t len = strlen(line);
-        scar_log(line);
+        scar_log("%s", line);
 
```

**What went wrong:** Two issues:

1. **Wrong path** — the diff header says `source/main.c` but the file is at
   `src/main.c`. The standard `patch` command fails to locate the file.
   The Python fallback ignores the path, so this alone wouldn't fail.

2. **Malformed hunk count** — `@@ -33,7 +33,7 @@` claims 7 lines but only 4
   lines of content follow. `patch` waits for 3 more lines that never arrive.

3. **Underlying signature mismatch** — even if the patch applied, compiling
   `scar_log("%s", line)` would fail if `scar_log` is declared as a
   single-argument function. The function definition is in `util.c:8` (a
   separate finding, #13) — cross-file context SCAR doesn't have at this stage.

**Action taken:** Left as-is. The path and hunk count issues are LLM
generation errors that the validator correctly catches. The signature mismatch
is a fundamental cross-file dependency that requires architectural changes
beyond the current scope. Finding #13 (`util.c:8`) was separately accepted and
fixes the root cause in the function definition.

---

## What the audit showed

Across 15 findings processed, 12 reached triage and all 12 were accepted
(100% triage acceptance rate). All 3 rejections happened at the validator
before triage ran. The failures were exclusively in **patch synthesis**, not in
context quality or triage reasoning:

| Finding | Failure | Root cause |
|---|---|---|
| 02-parse-46 | Hallucinated hunk | "Fix all occurrences" with no count anchor |
| 04-parse-68 | C redeclaration | Same variable declared in each hunk |
| 12-main-35 | Wrong path + hunk count | LLM diff formatting error |

The context briefings were accurate in all three cases — the LLM correctly
identified the vulnerability. The problem was translating that understanding
into a syntactically and structurally valid unified diff.

---

## Repeating this analysis

After any pipeline run, copy the traces to your local machine:

```bash
oc cp scar-inspector:/workspace/source/.scar/traces ./scar-traces
```

Then run the methodology above. The key signal is always in `2-patch-gen.md`
for validator failures and `4-arbiter.md` for triage failures — both are plain
Markdown files readable with `cat`.

For triage failures, compare the round files (`3-triage-round-*.md`) to see
how the reasoning chain evolved. An arbiter that returns INVALID after 3 VALID
rounds is worth reading — the reasoning usually identifies a genuine patch flaw
that context_gen or patch_gen missed.
