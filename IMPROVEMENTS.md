# SCAR — Improvement Roadmap

Ordered from lowest to highest implementation effort.

---

## Low Hanging Fruit

### Deduplicate IKOS findings by file+line
When multiple IKOS checkers fire on the same location (e.g. `boa` and `uva` both
flagging `oob_read.c:7`), the lower-priority finding wastes 8 LLM calls generating
an identical patch. Keep the highest-severity finding per `(file_path, line)` pair.
A simple set-based filter in `__main__.py` after loading IKOS results.

### IKOS witness traces as patch context
IKOS already generates counterexample traces — the exact execution path, branch
conditions, and variable values that lead to the bug. We discard this today.
Injecting the trace into the patch generation prompt gives the LLM precise, proven
context rather than having it re-derive the execution path from source alone.
Concretely: parse the witness from the IKOS output DB and append it to the
`patch_gen` user message.

### Pre-expand macros with `clang -E`
The LLM sees `buf[MAX_SIZE]` and has to emit a `GREP: MAX_SIZE` directive to resolve
it. Running `clang -E` before analysis produces concrete values inline. Feed the
expanded source alongside the original so the LLM sees both the readable form and
the resolved constants. Low cost — clang is already in the IKOS container.

### Disable or bound the `uva` checker on files with `boa` findings
The `uva` (uninitialized variable) checker produces false positives on out-of-bounds
array accesses that `boa` already covers. Either suppress `uva` findings at locations
where `boa` has fired, or define an explicit checker priority order
(`boa > dbz > nullity > dfa > sio > uva`) and drop lower-priority duplicates.

---

## Medium Effort

### Caller context injection (1-level call graph)
The LLM currently sees only the file containing the vulnerability. For reachability
assessment it needs to know: who calls the vulnerable function, and with what data?
Use `clang --analyze` or `clang -ast-dump` to extract direct callers and inject their
signatures and relevant argument sources into the context prompt. This allows the
triage stage to correctly assess whether a bug is attacker-reachable rather than
marking theoretical issues as VALID.

### RAG over accepted patches
Store each accepted patch in a vector database indexed by CWE, rule ID, and code
pattern. When processing a new finding, retrieve the 2–3 most similar past patches
and inject them as additional few-shot examples in `patch_gen`. Quality compounds
over time — repeated patterns (same developer habits, same library usage) produce
better patches as the system builds institutional memory.

### Incremental / delta analysis
Running the full pipeline on every commit is expensive for large repos. Build a
dependency graph from the call graph and only re-analyse:
- Files that changed in the commit
- Files that call changed functions (transitive, up to N hops)

Makes CI integration practical at scale. Requires persisting the call graph and
a mapping from file to its dependents between runs.

### Parallel repair loop (per-file concurrency)
The repair loop is single-threaded and processes each finding sequentially. For
repos with many findings the LLM API calls dominate wall time and are naturally
parallel. Safe model: group findings by `file_path` and process each group
sequentially within a `ThreadPoolExecutor` worker — this keeps sibling temp-file
names unique per thread and allows one `context_gen` briefing call per file
instead of one per finding, halving LLM calls on multi-finding files. A
`max_workers` cap (e.g. 4) prevents rate-limit 429s on the LLM endpoint.

### Patch dependency tracking
When multiple findings exist in the same file, patches may conflict or one patch
may render another redundant (as seen with the two `doublefree.c` patches — the
IKOS patch and the LLM patch each fix one bug but not both). Track which patches
touch overlapping line ranges and either merge them or apply them in dependency
order before the final compilation check.

### Function-boundary chunking for large files
The current approach passes entire files to the LLM up to the token limit, cutting
arbitrarily if exceeded. For large files, chunk at function boundaries using the
clang AST, keeping related functions (callee + its direct callees within the file)
together. Prevents context truncation mid-function and reduces noise from unrelated
code.

---

## Higher Complexity

### Program slicing around vulnerable statements
Instead of feeding a 3,000-line file to the LLM, extract only the program slice
relevant to the vulnerability — the set of statements that affect the value or
control flow at the vulnerable line. LLVM has slice infrastructure. Typical slices
are 20–80 lines regardless of file size, dramatically reducing hallucination from
irrelevant context and allowing analysis of files that would otherwise exceed the
token limit.

### Cross-file data flow tracking
For real codebases, untrusted data often crosses multiple files before reaching a
vulnerable sink. A network read in `io.c` flows into a struct, which is passed to
`parser.c`, which calls the vulnerable function in `crypto.c`. None of the
individual files look dangerous in isolation. Requires whole-program taint tracking
(Joern, CodeQL, or LLVM's DataFlowSanitizer in static mode) to construct the
inter-file data flow graph and inject the relevant path segments into context.

### Reachability filtering
Before spending LLM calls on a finding, determine whether it is reachable from an
external entry point (network socket, file read, CLI argument, IPC). IKOS performs
interprocedural analysis so its findings are generally reachable, but LLM scan
findings are not verified. Use the call graph to trace from the vulnerable location
back to a known untrusted source; reject findings with no external path rather than
passing them to triage. Reduces false positive rate for the LLM scan significantly.

### Automated patch application and re-scan
After accepting a patch, apply it to the source, re-run IKOS on the patched file,
and verify the finding no longer appears. Currently acceptance is based solely on
LLM triage — the patch is never actually applied and verified to fix the original
finding. Closing this loop would make acceptance criteria much stronger and catch
cases where a syntactically valid patch does not actually eliminate the bug.

### ESBMC integration as a findings source
ESBMC (Efficient SMT-Based C Model Checker) compiles C to GOTO-IR and uses
bounded model checking to generate concrete mathematical counterexamples: exact
execution traces with input values that reproduce the bug. This is a stronger signal
than IKOS abstract intervals — the LLM gets a proven crash path, not a range.
ESBMC can be wired in as a Tekton task writing `.scar/findings-esbmc.json` via the
pluggable findings convention, requiring no changes to the repair loop.
The main constraint is path explosion: ESBMC's loop unwind bound `k` must be tuned
per project. Start with `--unwind 5` and escalate on timeout.

### Fuzzing integration (libAFL / AFL++)
DARPA AIxCC first-place team (ATLANTIS) won using parallel fuzzing with libAFL,
directed by LLM-generated seeds and concolic execution. Fuzzing finds bugs IKOS
cannot reach (complex input-dependent paths, parser state machines) and produces
concrete crash inputs (PoVs) that can be fed into the repair loop as findings.
Wire as a Tekton task: run `afl-fuzz` or `libAFL` against an instrumented binary
for a bounded time, convert crash inputs to findings using `afl-analyze`, write
`.scar/findings-fuzzer.json`. The PoV file can additionally be used for re-scan
verification after patch acceptance (see "Automated patch application and re-scan").

---

## Embedded / Cross-Compilation (Future Direction)

### ARM cross-compilation target support
SCAR currently analyses x86 bitcode only. Safety-critical embedded firmware
(Zephyr, FreeRTOS, bare-metal Cortex-M) uses arm-none-eabi-gcc and cannot be
compiled with native clang. The path:
1. Inject `-save-temps=obj` into the cross-compiler command via bear interception,
   producing preprocessed `.i` files (all macros, register definitions resolved).
2. Re-compile each `.i` with target-configured clang:
   `clang -c -emit-llvm -target arm-none-eabi -march=armv7e-m -mfloat-abi=hard
    -mfpu=fpv4-sp-d16 -D__IKOS__ -g -O0 -Xclang -disable-O0-optnone file.i -o file.bc`
3. Pass `--hardware-addresses` to IKOS so that MMIO pointer casts
   (`*(volatile uint32_t *)0x4000C000`) are modelled as valid bounded regions
   rather than flagged as null or out-of-bounds.
4. IKOS volatile support is native — every volatile read yields an unconstrained
   symbolic interval covering the full data type range, correctly modelling
   asynchronous hardware register updates without false alarms.
This enables SCAR to scan Zephyr BLE stack CVEs (e.g. CVE-2021-3434 stack overflow
in `le_ecred_conn_req()`, CVE-2021-3432 divide-by-zero), FreeRTOS MPU bypass bugs,
and the ENSBench suite of lwIP/FreeRTOS-TCP network stack vulnerabilities.

---

## Notes

- Items within each section are roughly ordered by impact/effort ratio.
- The IKOS witness trace improvement has the highest impact-to-effort ratio of any
  item — IKOS already generates the data, it just needs to be surfaced.
- Reachability filtering and cross-file data flow are the approaches most responsible
  for the gap between demo-scale results and production-scale results on codebases
  like OpenSSL.
- DARPA AIxCC results show fuzzing + LLM outperforms static-only approaches for
  finding deep, input-dependent vulnerabilities. SCAR's pluggable findings convention
  makes fuzzer integration straightforward without touching the repair loop.
