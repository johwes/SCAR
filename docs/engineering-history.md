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

## Stage 11 — Real-codebase hardening

Running the full pipeline against scarnet — a real TCP server rather than a
collection of toy programs — exposed three silent failures that had never
appeared on the test corpus.

**IKOS entry point strategy.** On scar-test-c, each program has its own
`main` function and IKOS analyses each independently. On scarnet, a single
`main` runs the accept loop, dispatching through several call frames before
reaching any vulnerable code. Abstract interpretation applies widening
operators at loop and call boundaries to guarantee convergence; over a deep
call chain this loses interval precision. By the time IKOS reaches a
`strcpy` call five frames in, the source string's length has been widened to
`[0, +∞)` and the alarm may not fire.

The entry point was changed from `main` to every public non-`main` API
function in the linked binary (capped at 150 to avoid shell ARG_MAX limits on
large targets like Nginx). IKOS then analyses each function with fully
unconstrained inputs one frame deep, preserving the interval precision that
degrades over deep whole-program chains.

This change was still worth making, but it surfaced a more fundamental
insight: even with perfect precision, IKOS cannot detect bugs that depend on
*network input*. Knowing that a string came from a socket read tells IKOS
nothing about its length — that fact is only known at runtime. On scar-test-c
the inputs are literals; IKOS can bound them. On a TCP server every dangerous
value is an attacker-controlled byte stream. The entry point fix improves
IKOS precision for bounded programs; it does not change the input-domain
problem on network servers.

**OSS-Fuzz CCDB gap.** The LLM scan file discovery used the
`compile_commands.json` database to identify which C files to scan. This
works for normal build systems. OSS-Fuzz's `build.sh` convention compiles
only the fuzz harness, not the server binary — so `main.c` had no CCDB
entry and was silently skipped. The fix was to union CCDB files with non-CCDB
files that pass a fuzz/test path filter: application code outside the CCDB
gets scanned, harness files do not. The log now shows
`5 C file(s) to scan (4 from CCDB, +1 outside CCDB)` so the gap is visible.

**cppcheck flag silent failure.** The IKOS task runs cppcheck as a
supplementary step and passes `-i .scar/` to exclude the build artefact
directory. The cppcheck version in the container predated the `--exclude`
flag; the invocation was silently producing empty XML. The flag was corrected
to `-i "$WS/.scar"` which all versions support. Cppcheck findings were
invisible until this was caught by watching the converted JSON output contain
zero entries across every run.

---

## Stage 12 — Repair loop context audit

The repair loop spends approximately 87% of the total token budget — roughly
410k of 473k tokens on a typical scarnet run. The 13% spent on detection
(the LLM scan) is nearly irrelevant to the cost.

An external review of the repair loop code identified a structural oversight:
`context_gen.py` does careful work to truncate source to function boundaries,
capped at 300 lines. But `patch_gen.py` then reads the entire source file
independently and appends it to the patch prompt. `triage.py` does the same
— re-reading the full source into `base_context` and re-sending it at the
start of every triage round.

The truncation work was being undone one step later.

The fix was to thread the briefing produced by `context_gen` through to both
`patch_gen` and `triage`, replacing the full-file reads. The patch system
prompt was updated to say "fix ALL occurrences visible in the security
briefing" rather than "scan the ENTIRE source file" — an instruction that was
impossible to follow once the full file was removed. Triage falls back to
reading the full source only if no briefing is provided, preserving
compatibility with direct CLI invocations. The agentic `GREP:` directive
remains available in triage for any cross-function lookups the briefing
doesn't cover.

The token saving per finding scales with source file size and number of
triage rounds. For scarnet's small files (~150 lines) the saving is moderate.
For a larger target the compounding effect across rounds is significant.

---

## Stage 13 — Concurrency, correctness under review, and log observability

### Field correction to Stage 12

Before the parallelism work, a regression from Stage 12 needed fixing.
Threading the pre-generated briefing through to `patch_gen` (the natural
extension of the same briefing-threading done for triage) produced a 14% drop
in accepted patches — from 14 to 12 — on a real scarnet run. The root cause:
patch synthesis needs the full source to correctly reference constants, struct
fields, and types defined outside the vulnerable function. The function-bounded
briefing deliberately excludes that context. Triage is tolerant of this
truncation because the agentic `GREP:` directive covers cross-function lookups;
patch generation is not. `patch_gen` was reverted to reading the full source,
while triage kept the briefing-only approach where the saving across up to five
rounds per finding is substantial.

The lesson is narrow but important: the same briefing works differently in
different roles. Triage is adversarial and exploratory — gaps can be filled by
grep. Patch synthesis is constructive and needs a complete picture of the
codebase before generating code that compiles.

### Two bugs from external review

An external architecture review identified two correctness bugs that had gone
unnoticed in earlier testing.

**Tool diversity miscounting.** `scar-report.yaml` computes `tool_diversity`
as the number of unique `origin` strings across all accepted patches. This
rewards multi-tool pipelines — finding the same bug with both a fuzzer and
static analysis is more valuable than finding it twice with the same tool. The
origin was supposed to come from the filename: `findings-fuzzer.json` should
produce `origin = "fuzzer"`, and `findings-cppcheck.json` should produce
`origin = "cppcheck"`. In practice, every non-IKOS finding was stamped
`"llm"` — the default from an unrelated code path that was reached before the
filename-based extraction ran. Fuzzer and cppcheck patches were accepted but
counted as a single tool. The fix extracted the tool name from the filename
stem at load time and stored it in a parallel `id(finding) → tool_name` dict,
looked up during patch acceptance.

**Validator comment false-positive.** The validator's safety checks run regex
patterns against the lines added by a patch. One pattern caught `strcpy(` in
added code. A patch that replaced `strcpy` with `strncpy` would naturally
include a code comment explaining the change — something like
`/* replaced strcpy( with strncpy */`. The regex matched the comment, not the
call, and rejected the patch. The fix stripped line and block comments from the
added and removed code before running all three safety patterns, so the check
fires only on actual code.

Both bugs are the kind that testing on toy corpora misses. On scar-test-c,
tools rarely find the same bug through different paths, so the diversity
miscounting didn't change any score. Simple patches on short files rarely
produce explanatory comments, so the comment trap never fired.

### Motivating the parallel repair loop

A complete pipeline run against scarnet produced wall-clock data: 48 minutes
54 seconds, 501,492 tokens, 12 accepted patches, score 62. The breakdown
showed IKOS taking under a minute and the LLM scan running sequentially across
5 files — roughly 10 minutes — while the repair loop ran 12 findings
sequentially at roughly 3 minutes each for a total of around 36 minutes.

For a 3-hour student workshop where one or two pipeline runs provide the
entire learning feedback loop, 49 minutes per run is a serious constraint.
Halving it would double the iterations students can attempt.

### Parallel repair loop

Findings were already being read from multiple independent tools. The repair
loop itself was the last sequential bottleneck.

The core observation is that findings in *different source files* are
independent: fixing `handler.c:46` does not affect the source visible when
fixing `parse.c:31`. Findings in the *same* file are not fully independent —
a future patch-compounding feature would apply each accepted patch to a
per-file scratchpad before processing the next finding, so ordering within a
file matters. The design respects this distinction:

- Findings are grouped by resolved file path.
- Each file group runs sequentially inside one `ThreadPoolExecutor` worker.
- Different file groups run concurrently across workers.
- Concurrency is capped at `--max-workers` (default 4) so the loop stays
  predictable on large projects.

On a scarnet run with findings spread across 4 files, this reduces repair-loop
wall time from ~36 minutes to ~9 minutes — one worker per file, bounded by the
slowest file group rather than the sum of all.

Patch compounding — actually writing each accepted patch back to a per-file
scratchpad before the next finding — is preserved as future work. The
sequential-within-group order is the structural foundation; the scratchpad
mechanism is not yet implemented, and today each finding reads the original
file from disk. Mutating source files mid-pipeline was considered and rejected:
if the pipeline is interrupted after partial mutations, the source tree is left
in an indeterminate state that makes reruns unreliable.

### Thread safety

The `ThreadPoolExecutor` immediately surfaced a race condition in `llm.py`.
Python's `_prompt_tokens += response.usage.prompt_tokens` compiles to three
bytecodes — LOAD, BINARY_ADD, STORE. The GIL can yield between any two of
them. Under concurrent workers, increments from different threads interleave,
silently dropping some counts. The token total is used as a leaderboard
tiebreaker; silent corruption matters.

The fix added a `threading.Lock()` protecting both the token counters and the
lazy `_client` singleton. The singleton race is less likely (first
initialisation happens before workers start) but is a correctness issue
regardless. `get_usage()` is also guarded so callers always observe a
consistent snapshot.

Two-model deployments (`LLM_PATCH_MODEL` and `LLM_REVIEW_MODEL`) work without
changes: the `model` parameter is per-request, not per-client. The single
`OpenAI` client is an HTTP connection pool, and both models share it.

### Parallel LLM scan

Once IKOS timing was measured precisely — seconds, not the minutes initially
assumed — the LLM scan became the identified bottleneck in the pre-repair
phase. Files in the LLM scan are fully independent (unlike repair-loop
findings, there is no compounding question), so the parallelism is
straightforward: a `ThreadPoolExecutor` with the same 4-worker default as the
repair loop, each worker scanning one file end-to-end.

The measured improvement: 5 files scanning in ~2 minutes concurrently versus
~10 minutes sequentially. Combined with the parallel repair loop, the expected
total pipeline time drops from 49 minutes to roughly 13–15 minutes.

The `scan_cmd.py` invocation signature was simultaneously moved from `sys.argv`
slices to `argparse`, adding a `--max-workers` flag, without changing the
Tekton task YAML — the positional arguments are backward-compatible.

### Log observability: three iterations

Parallel workers produce interleaved output. Without labelling, a Tekton log
showing 40 `[triage round N/3] reviewing...` lines is unreadable — it is
impossible to tell which lines belong to which finding.

Three iterations of log labelling addressed this progressively.

**Iteration 1 — file stem prefix** (`[handler]`, `[parse]`): each
`_process_file_group` worker prefixes its own log lines with the source file
stem. Workers for different files are now distinguishable. The prefix was
propagated through `context_gen.generate()` and `triage.run()` — both
previously printed bare lines — via a `tag: str = ""` parameter. Callers pass
their tag; callees prepend it.

**Iteration 2 — the stem tag's limitation**: two findings in the same file
(`handler.c:46` and `handler.c:80`) produce identical prefixes. Since findings
in the same file run sequentially in one worker, the output does not interleave
— but the stem tag cannot distinguish them in post-hoc analysis, and the
finding IDs are not visible in the flow.

**Iteration 3 — per-finding `[#N file:line]` tags with global index**: stable
sequential IDs are assigned to all findings before any worker starts, so every
log line — briefing, patch generation, triage rounds, arbiter — carries the
same `[#2 handler:80]` label regardless of which worker produced it. A finding
index is printed at the start of the repair loop:

```
[scar] 3 finding(s) to process:
  #1  CWE-121      handler.c:46   [ikos]
  #2  CWE-416      handler.c:80   [llm-scan]
  #3  CWE-122      parse.c:31     [llm-scan]
```

A complete trace for finding #2 — from briefing through arbiter — is then
recoverable with `grep '\[#2'` against the full Tekton log. This is the
debugging primitive that makes parallel logs navigable.

---

## What the evolution shows

Looking across all thirteen stages, a pattern emerges: **every optimisation was
motivated by observing a real run.**

- The build robustness work (Stage 1) came from watching containers fail.
- The compile_commands.json work (Stage 4) came from watching IKOS miss bugs
  because it didn't have the right include paths.
- The Python patch fallback (Stage 7) came from watching LLM-generated diffs
  fail to apply against the files the LLM had hallucinated context for.
- The token optimisations (Stage 10) came from comparing token budgets across
  two runs on different corpora.
- The entry point and file discovery fixes (Stage 11) came from running on a
  real TCP server and watching tools silently produce no output.
- The repair loop context audit (Stage 12) came from noticing that 87% of
  tokens were spent in a loop that was re-sending context already truncated
  elsewhere.
- The briefing correction (Stage 13) came from measuring a 14% drop in
  accepted patches after extending truncation to patch synthesis.
- The parallelism work (Stage 13) came from measuring a 49-minute wall-clock
  run and identifying the LLM scan and repair loop as sequential bottlenecks.
- The thread safety fix (Stage 13) came from adding a thread pool and
  immediately recognising that `+=` on a shared counter is not atomic.
- The per-finding log tags (Stage 13) came from discovering that file-stem
  prefixes don't distinguish two findings in the same file.

The LLM contributes intelligence — it can recognise a format string
vulnerability, generate a syntactically valid patch, and evaluate whether the
patch fixes the root cause. But the system around it is engineered: the
parallel task structure, the deduplication window, the two-pass patch
application, the pluggable findings convention, the brace-counting heuristic,
the early-exit condition, the thread-safe token counters, the per-finding log
index. None of those came from a model. They came from identifying failure
modes and designing around them.

That is what building a system with LLMs at the centre looks like: the model
is one component, and the engineering discipline of making it reliable,
efficient, and correct is everything else.
