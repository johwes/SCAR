# How SCAR Was Built — Engineering History

SCAR is not a research prototype that appeared fully formed. It was built
iteratively, each stage driven by a concrete problem the previous stage
couldn't solve. This document traces that evolution so students can
understand not just what SCAR does, but why it is the way it is.

---

## Stage 1 — Getting the tools to build (longer than expected)

The first commits have nothing to do with vulnerability scanning. They are
twenty-odd commits wrestling a single tool — NASA IKOS — into a reproducible
container image.

IKOS is a sound static analyser built on LLVM's abstract interpretation
infrastructure. "Sound" means it proves bugs are definitely present rather
than guessing. That guarantee is valuable, but it comes with a cost: IKOS
has complex build dependencies (LLVM 14, TBB, GMP, SQLite, Python bindings)
and does not ship pre-built binaries.

The build sequence failed in different ways on different base images:

- CentOS Stream 9 — missing TBB headers at the path IKOS's cmake expected
- Ubuntu (latest at the time) — IKOS Python wrapper installed silently into a
  path not on PYTHONPATH; the binary ran but `import ikos` failed at analysis time
- Ubuntu 22.04 — missing `python3-setuptools`, causing the Python venv
  creation to fail without a useful error message

Each failure required reading IKOS's upstream CI configuration to understand
what the developers actually tested against. The final solution pinned to
Ubuntu 22.04 and replicated the upstream CI's dependency list exactly.

**The lesson:** reproducibility is not free. Before a single vulnerability
was found, two weeks of engineering went into making the analysis environment
deterministic. This is normal for systems that depend on complex external tools.

---

## Stage 2 — A pipeline that runs end to end

With IKOS building, the next problem was orchestration. The pipeline needed
to clone a repository, compile it to LLVM bitcode, run analysis, and collect
results — all in a Kubernetes environment using Tekton.

The git-clone step alone went through three iterations:

1. Bundle a custom task using `alpine/git` — simple but doesn't handle
   private repositories or SSH keys the way OpenShift Pipelines expects.
2. Use the cluster `git-clone` ClusterTask via a resolver — broke because
   the task's parameter names are `ALL_CAPS` (a Red Hat convention), not the
   lowercase names the initial YAML assumed.
3. Reference the task correctly with `resolver: cluster` pointing at the
   `openshift-pipelines` namespace.

The build-bitcode step had a subtler issue: `llvm-link` was being called with
a shell glob (`*.bc`) that the script expanded before calling the command,
but in the Tekton container environment the glob sometimes resolved to nothing.
Switching to `find` with `mapfile` made the file collection robust.

At this point SCAR could run IKOS on a repository. But it only ran five of
the seven available checkers. Adding `uva` (uninitialized variables), `sio`
(signed integer overflow), and `dfa` (double-free / use-after-free) came
later, once the infrastructure was stable enough to iterate on the analysis
configuration without fighting the container build.

---

## Stage 3 — Adding the LLM as a second scanner

IKOS is sound but narrow. It proves the bugs its checkers model and ignores
everything else. A `printf(user_data)` format string vulnerability, for
example, is invisible to IKOS because the checker would need to model the
semantics of variadic C library functions — which abstract interpretation
does not do.

The LLM scan was added as a second, independent analysis pass running in
parallel with IKOS. The key design decision was to run them *in parallel*
rather than sequentially: IKOS's analysis time is bounded by the bitcode
size and the number of checkers, while the LLM scan time is bounded by the
number of source files. Neither depends on the other's output at this stage,
so there is no reason to serialize them.

The parallel structure also meant that adding a third scanner later (the
OSS-CRS external tool) required only a new Tekton task and a `runAfter` entry
— the pipeline topology already supported it.

The LLM scan was modelled on the
[nano-analyzer](https://github.com/weareaisle/nano-analyzer) architecture:
a three-stage loop of context generation → patch generation → skeptical
triage. The skeptical triage was the key insight from nano-analyzer — instead
of asking the LLM "is this patch correct?" (which it almost always answers
yes), you ask it "find reasons this patch is wrong." A different posture
produces a different, more useful answer.

---

## Stage 4 — Multi-file projects and build system detection

The first version of `build-bitcode` called `clang -emit-llvm` directly on
each `.c` file it found. This works for toy programs. It fails for any real
project because real projects have non-trivial include paths, preprocessor
definitions, and build configurations that clang needs to know about.

The fix was `compile_commands.json` — a standard format where the build
system records the exact compiler invocation used for each file. SCAR now
runs one of three tools to produce this database:

- `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` for CMake projects
- `bear -- make` for Makefile projects (intercepts compiler calls)
- OSS-Fuzz's `build.sh` convention for fuzzing-focused projects

The emit-llvm step then reads the database and replays each invocation with
`-emit-llvm` appended. This ensures the bitcode is compiled with the same
flags as the actual build — the same include paths, the same preprocessor
macros, the same language standard.

Without this, multi-file projects fail in subtle ways: a header with `#define
MAX_KEY_LEN 64` might not be found, so IKOS sees `MAX_KEY_LEN` as an
undefined symbol and cannot prove the buffer overflow. The sound analysis
produces no output — not because there is no bug, but because the analysis
was incomplete.

A parallel fix was needed in the validator: when a patch changes a file,
the validator recompiles it to check the patch is syntactically correct. The
recompilation must use the same flags as the original build, which means
looking up the file in the same `compile_commands.json` database.

---

## Stage 5 — IKOS witness traces

The LLM scan produces findings, but its reasoning is probabilistic. The LLM
might correctly identify a buffer overflow but generate a patch that doesn't
fix the root cause — for example, adding a length check on the wrong variable.

IKOS produces something the LLM does not: a *counterexample witness trace*.
When IKOS flags a buffer overflow, it records the abstract interval state at
each statement along the execution path — the checker name, the status, the
call context. This is stored in a SQLite database (`whole_program.db`).

The key insight was to inject this trace into the context generation prompt
before patch synthesis. Instead of asking the LLM to re-derive reachability
from source, you give it the proof: "IKOS has verified that on this path,
this buffer of size 64 receives input of unbounded length." The LLM then
generates a patch against known facts rather than suspected ones.

This changed the dynamic: IKOS findings became higher-quality inputs to the
repair loop than LLM findings, because the patch generator had proven
execution data rather than inferred risk.

---

## Stage 6 — OSS-CRS integration

The [AIxCC competition](https://aicyberchallenge.com/) produced a shared
interface for vulnerability scanning tools — the OSS-CRS `libCRS` API. Tools
that implement this interface can interoperate: each tool submits findings via
`libCRS.submit()` and picks up findings from other tools via
`libCRS.register_fetch_dir()`.

SCAR was extended in both directions:

1. **As a participant**: the repair loop registers its accepted patches with
   the CRS ensemble so other tools can see them.
2. **As a host**: the `osscrs-scan` task runs external tools that call
   `libCRS.submit()`, intercepting their output via a shim that translates
   it into SCAR's internal findings format.

The shim approach meant that any OSS-CRS-compatible tool could be plugged
into SCAR without modifying the tool's code. The bridge file is copied from
the `scar-agent` image into the shared PVC at runtime, then injected into
the external tool's container via `PYTHONPATH`. The tool calls `import
libCRS` and gets SCAR's shim instead of the real CRS sidecar.

---

## Stage 7 — Patch application robustness

Generating a patch is only useful if it can be applied. The standard `patch`
binary applies unified diffs by matching context lines — the unchanged lines
that surround the modification. LLMs generate syntactically correct diffs but
frequently hallucinate context lines that don't exactly match the source.

Two changes addressed this:

**Fuzzy matching**: `patch --fuzz=3 --ignore-whitespace` tolerates up to 3
lines of context mismatch and ignores whitespace differences. This handles
most cases where the LLM slightly misquotes a comment or blank line.

**Python fallback**: when fuzzy matching fails, a Python applier in
`validator.py` ignores context entirely. It extracts the `+` and `-` lines
from the diff, locates the removed block by line number with a ±15-line
search, and applies only the change. This handles cases where the LLM
fabricates context that has no relationship to the actual source.

The two-pass approach — standard apply first, Python fallback second — means
the standard apply's correctness guarantees are preserved when possible, with
the Python fallback only engaging when necessary.

---

## Stage 8 — Workshop infrastructure

Running SCAR as a single pipeline against a single repository is one thing.
Running it as a student competition with multiple teams and a live leaderboard
is another.

Three pipeline variants were created:

- **v1**: LLM scan only. No IKOS. Fast, demonstrates the baseline capability.
- **v2**: Full pipeline. IKOS + LLM + OSS-CRS in parallel.
- **v3**: v2 plus two stub slots for student-implemented tools.

The stub slots are the pedagogical core of v3. Students replace
`scar-stub-fuzzer.yaml` and `scar-stub-custom-scan.yaml` with real tools.
The repair loop picks up their findings automatically — no changes to the
pipeline YAML required, because of the `findings-*.json` convention
established in Stage 4.

A competition dashboard was built to collect results from all teams. Each
pipeline run ends with a `report` task that POSTs metrics to the dashboard
API. The scoring formula (`patches × 3 + unique CWEs × 2 + tool diversity × 1`)
rewards depth (fixing diverse bug classes) over breadth (finding many
instances of the same bug).

A persistent SQLite database backed by a PVC ensures scores survive pod
restarts. The first implementation used `/data` for the database path, which
failed immediately in OpenShift because the container runs as a non-root UID
that cannot write to volumes mounted at `/data` without an explicit
`fsGroup`. The fix was to default to `/tmp/dashboard.db` — no permission
issues, at the cost of losing data on restart — then add the PVC as an
optional upgrade when persistence is needed.

---

## Stage 9 — Token accounting and reliability

Two reliability problems emerged from real pipeline runs:

**Transient server disconnects** killed the repair loop entirely. The LiteLLM
proxy occasionally drops connections mid-request, raising an
`httpcore.RemoteProtocolError`. Without retry logic, a single disconnect
aborted all findings processing and wasted all tokens already spent. Adding
exponential backoff (3 attempts, 3s/6s/12s delays) converted hard failures
into brief pauses.

**Token counting across containers** was broken. The `llm-scan` task runs in
a separate container from the repair loop. Module-level counters in `llm.py`
are lost when the container exits. The fix was to write a partial token file
(`token-usage-llm-scan.json`) at the end of `scan_cmd.py`, then have the
repair loop glob all `token-usage-*.json` partials and merge them before
writing the final total. This also means any future task that calls the LLM
can contribute to the count by writing its own partial file.

Two LLM roles were also separated: `patch_model` for generation tasks
(context briefing, vulnerability scan, patch synthesis) and `review_model`
for evaluation tasks (triage rounds, arbiter verdict). Using different models
for each role improves patch quality — the reviewer brings independent
judgment rather than being consistent with the generator's own blind spots.

---

## Stage 10 — Token efficiency optimisations

Comparing the first real runs on scar-test-c (toy corpus) and scarnet (real
TCP server) revealed a stark difference in token consumption: 168k tokens
for 7 findings on the toy corpus versus 483k tokens for 15 findings on the
real codebase.

The difference had three causes:

**Source file size.** Context generation sent the entire source file with
every finding. A 30-line toy file costs almost nothing; a 600-line server
handler sent through context gen, patch gen, and three triage rounds is
expensive. The fix was function-boundary truncation: a brace-counting
heuristic extracts only the C function enclosing the vulnerable line (capped
at 300 lines), with a ±100-line fallback if the boundaries cannot be
determined. The LLM still has access to cross-function context via the
agentic `GREP:` directives it can emit during triage.

**False positive tax.** A rejected finding pays the same token budget as an
accepted one — context gen, patch gen, and all triage rounds before the
rejection. Early-exit triage addresses this: if any triage round returns
`INVALID`, the remaining rounds are skipped and the arbiter runs immediately
on the completed reasoning. An `INVALID` on round 1 almost never recovers to
`VALID` in later rounds; skipping rounds 2 and 3 saves the bulk of the per-
finding triage cost for rejected patches.

**Unused cppcheck output.** Cppcheck had been running as a supplementary
step in the IKOS task from early in the project, writing its output to
`cppcheck.xml`. But the XML file was never converted to the `findings-*.json`
format, so the repair loop never saw it. A `convert-cppcheck` step now
parses the XML, filters to `error` and `warning` severity, maps CWE numbers
where present, and writes `findings-cppcheck.json` — activating a scanner
that had been running but contributing nothing for the entire project.

---

## What the evolution shows

Looking across all ten stages, a pattern emerges: **every optimisation was
motivated by observing a real run.**

- The build robustness work (Stage 1) came from watching containers fail.
- The compile_commands.json work (Stage 4) came from watching IKOS miss bugs
  because it didn't have the right include paths.
- The Python patch fallback (Stage 7) came from watching LLM-generated diffs
  fail to apply against the files the LLM had hallucinated context for.
- The token optimisations (Stage 10) came from comparing token budgets across
  two runs on different corpora.

The LLM contributes intelligence — it can recognise a format string
vulnerability, generate a syntactically valid patch, and evaluate whether the
patch fixes the root cause. But the system around it is engineered: the
parallel task structure, the deduplication window, the two-pass patch
application, the pluggable findings convention, the brace-counting heuristic,
the early-exit condition. None of those came from a model. They came from
identifying failure modes and designing around them.

That is what building a system with LLMs at the centre looks like: the model
is one component, and the engineering discipline of making it reliable,
efficient, and correct is everything else.
