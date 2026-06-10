# SCAR — Improvement Roadmap

Ordered from lowest to highest implementation effort.

---

## Low Hanging Fruit

### Deduplicate Intra-IKOS findings by file+line
When multiple native IKOS checkers fire on the exact same line (e.g. `boa` and `uva` both flagging `oob_read.c:7`), the lower-priority check wastes multiple LLM calls generating a redundant patch. We should apply a set-based coordinate filter in `__main__.py` right after loading the SARIF bridge array to keep only the highest-severity finding per unique `(file_path, line)` statement.

### Pre-expand macros with `clang -E`
The LLM frequently encounters code statements containing fixed-size allocations like `char buf[MAX_SIZE]` and is forced to issue an agentic `GREP: MAX_SIZE` directive to resolve its value. Running a localized `clang -E` pass before analysis produces concrete integer definitions inline. Passing the macro-expanded source string alongside the original codebase allows the LLM to read the human form while immediately seeing the mathematically resolved constant limits.

### Source-file adversarial content scan
Before feeding source files to the LLM, run a lightweight regex scan for prompt-injection patterns in comments, string literals, and identifier names (e.g. `IGNORE PREVIOUS INSTRUCTIONS`, `[SYSTEM]`, instruction-like phrasing in block comments). A source file submitted by an external party could embed content that manipulates the patch generator or triage reviewer — a risk that increases as SCAR is used in open-submission or competition contexts. The scan adds negligible overhead and can be implemented as a pre-processing step in `scan_cmd.py` or `context_gen.py`, logging a warning and optionally stripping suspicious comment blocks before sending to the LLM.

### Suppress `uva` (uninitialized variable) alerts on proven `boa` sinks
The native `uva` checker frequently flags uninitialized states on out-of-bounds array reads that `boa` has already definitively proven to be buffer overflows. Defining an explicit static priority hierarchy (`boa > dbz > nullity > dfa > sio > uva`) and automatically dropping lower-priority duplicate alerts on identical lines will optimize token efficiency.

---

## Medium Effort

### Parallel repair loop (per-file concurrency)
*Implemented.* Findings are grouped by resolved file path and processed concurrently via a `ThreadPoolExecutor` (controlled by `--max-workers`, default 4). Findings within the same file still run sequentially so patch-compounding can be added later without restructuring. The remaining step is actually compounding patches within a file group — applying each accepted patch to an in-memory scratchpad before the next finding is processed.

### Patch dependency tracking & compounding updates
When multiple distinct vulnerabilities exist in different functions of the same file, independent patches will conflict or step on each other's line hunk coordinates. By building on top of the file-grouped sequential threading layout, the engine should apply each accepted patch to its local disk scratchpad immediately after a successful triage phase, forcing subsequent patch synthesis tasks for that file to evaluate the continuously healed state.

### Caller context injection (1-level call graph)
The context generation phase is currently blind to cross-module execution origins. For sophisticated logic validation, the model needs to know: who calls the vulnerable function, and what values are passed inside the caller argument array? Utilizing `clang --analyze` or an AST map to extract direct callers and inject their functional definitions into the prompt allows the triage stage to correctly filter out un-reachable code pathways.

### Function-boundary chunking for large files
Passing complete files to the LLM can easily overflow active token context windows or introduce noise on massive source frameworks. Utilizing the Clang AST compiler hooks to slice source modules explicitly at function boundaries keeps related functional clusters intact while dropping completely irrelevant background blocks.

*Partially implemented:* `context_gen.py` now uses a brace-counting heuristic to extract the enclosing C function (capped at 300 lines) instead of sending the whole file. The remaining step is replacing the heuristic with a proper Clang AST pass for correctness on macro-heavy code.

### RAG over accepted patches
Store each accepted patch in a vector database indexed by CWE rule ID and structural code snippet layout. When processing a new vulnerability, retrieve the top 2–3 closest historical patches and inject them as few-shot examples into `patch_gen` to adapt to the project's coding conventions.

A more precise retrieval strategy (from the PredicateFix line of research) uses static analysis predicates as query keys rather than embedding similarity. CodeQL predicates capture the structural constraint missing from the vulnerable code — the "bridging predicate" — and retrieval finds historical patches where adding exactly that constraint made the warning disappear. This predicate-keyed retrieval substantially outperforms embedding-similarity on complex multi-constraint vulnerabilities because it matches on semantic structure rather than surface-level token proximity.

### Pre-filter classifier (false-positive gate)
Static analyzers and LLM scans both produce findings that fail triage at a measurable rate — estimated 30–35% for low-confidence LLM scan findings. Each rejected finding still consumes the full context-generation and patch-synthesis token budget. A lightweight pre-filter classifier runs between finding ingestion and the repair loop: it scores each finding for likely validity using rule-id base rates and line-context signals, and discards findings below a confidence threshold before they enter the repair loop. This integrates naturally with the specialist routing architecture described in `docs/experimental-design.md` — the classifier gate fires before routing, so only plausible findings reach any specialist track.

### Serving-layer optimizations (prefix caching, structured output)
Two serving-layer improvements require no SCAR code changes — only endpoint configuration:

**Prefix caching (RadixAttention):** System prompts for context generation, patch synthesis, and triage are repeated on every API call. SGLang's RadixAttention and vLLM's prefix cache eliminate KV recomputation for shared prefixes across requests. The triage loop sends the same system prompt 3–5× per finding; caching eliminates 60–80% of prefill cost for those calls. Enable via `--enable-prefix-caching` in SGLang or the equivalent vLLM flag.

**Structured output speed (XGrammar-2):** `generate_structured` uses JSON schema–constrained generation. On Outlines-backed endpoints this can be slow (FSM expansion per token). XGrammar-2 JIT-compiles grammars to pushdown automata, achieving 6–10× faster constrained token generation — a drop-in swap at the serving layer with no patch to SCAR.

---

## Higher Complexity

### Program slicing around vulnerable statements
Instead of feeding complete source paths to the model, extract a precise, minimized program slice relevant to the vulnerability location. By tracing data-flow tracking trees and control dependencies inside the LLVM IR infrastructure, the slice isolates the exact group of statements that affect the variable states at the vulnerable line, dramatically lowering prompt contexts and squeezing large frameworks into narrow token windows.

### Cross-file data flow tracking
Untrusted remote inputs frequently traverse multiple structural definitions and files before hitting a dangerous execution sink (e.g., an network read inside `io.c` writes to a global struct passed to `parser.c`, which triggers a vulnerability inside `util.c`). Integrating full data-flow tracking graphs (via platforms like CodeQL, Joern, or static LLVM DataFlowSanitizers) is required to capture interprocedural taint trajectories.

### Automated patch application and re-scan validation
The final acceptance of a candidate patch is currently determined exclusively by speculative LLM triage rounds. Closing the verification loop completely requires applying the diff, compiling the target, and re-running the whole-program linked static analysis pass (`ikos`) on the newly healed state to verify mathematically that the original error has been completely eradicated from the system.

### ESBMC integration as a pluggable findings source
Efficient SMT-Based C Model Checking (ESBMC) compiles targets into GOTO-IR structures and leverages bounded model checking to output concrete mathematical counterexamples. This represents an incredibly strong signal for patch generation because the model receives an absolute, step-by-step crash path tracking concrete variable inputs. This can be cleanly integrated as a standalone task dropping a `.scar/findings-esbmc.json` log payload.

### KLEE symbolic execution as a finding source
KLEE operates on LLVM bitcode — the same artifact the `build-bitcode` task already produces for IKOS — making it a near-zero-cost addition to the build pipeline. Instead of exploring paths randomly like a fuzzer, KLEE uses SMT solving to systematically enumerate all feasible execution paths through a function, generating a concrete `.ktest` input for each path that reaches an error condition. A function-level harness (marking the input buffer symbolic with `klee_make_symbolic`, then calling into the API) is an instance of Under-Constrained Symbolic Execution: starting at an API boundary rather than `main` avoids the path explosion of whole-program analysis while retaining full symbolic precision. KLEE's primary advantage over libFuzzer is deterministic boundary coverage — it finds exact-boundary bugs (e.g. a buffer that only overflows at precisely `N` bytes) in one run rather than waiting for random mutation. `.ktest` outputs can be replayed through an ASan build and converted to `.scar/findings-klee.json` using the same crash-to-findings pattern as the fuzzer task. A secondary use is seeding the libFuzzer corpus: KLEE's systematically chosen inputs start the fuzzer from a diverse coverage baseline.

### LLM-generated ACSL annotations + Frama-C formal verification
Rather than relying entirely on speculative triage rounds, a deterministic formal check can be inserted between patch generation and triage: the LLM generates ACSL pre-conditions, post-conditions, and loop invariants for the patched function; Frama-C's E-ACSL plugin translates these into runtime-checked assertions via source-to-source transformation; if Frama-C accepts the annotations as formally consistent, the patch advances to a shorter triage pass (1–2 rounds instead of 5). This neuro-symbolic arrangement — LLM proposes invariants, formal kernel accepts or rejects deterministically — eliminates the "semantic rollback" failure mode where a patch is plausible-looking but mathematically incorrect. It is high effort to operationalize (Frama-C container, ACSL generation prompt, annotation feedback loop on rejection) but produces the strongest correctness signal available without a full proof kernel like Lean or Coq.

### Diversified fault localization for crash findings
When a fuzzer or ASan run produces a crash, the crash site (the line that fires the assertion) is frequently downstream from the actual root cause. Sending only that line to the repair LLM produces superficial downstream fixes. The Kumushi pattern addresses this: generate a *family* of variant inputs around the crashing seed (by mutation), run each through the instrumented binary, and rank functions by crash-trigger frequency across the family. The function present in every crash trace is almost certainly the root cause; functions appearing in only some traces are secondary effects. The repair LLM then receives the ranked function list alongside the concrete crash trace — allowing it to target the upstream root cause rather than the downstream crash site. This is implementable as a post-processing step on the existing fuzzer task output with no changes to the repair loop itself.

### Fuzzing integration (libFuzzer / AFL++)
Integrating a coverage-guided fuzzer catches deep, stateful, input-dependent vulnerabilities that static abstract interpretation cannot model — particularly bugs requiring specific boundary values or multi-call session state. The task runs an instrumented binary for a bounded wall-clock window, isolates distinct crashes, and converts ASan reports into `.scar/findings-fuzzer.json`. libFuzzer is the recommended starting point: the target already has an OSS-Fuzz harness, it uses the same clang toolchain as the existing bitcode build, and its seed corpus mechanism pairs naturally with KLEE-generated test cases. See `fuzzer-extension-guide.md` in the artifacts directory for harness design, Tekton task structure, and crash-to-findings conversion.

---

## Notes
* DARPA AIxCC results prove that hybrid ensembles leveraging directed fuzzing alongside LLM orchestration significantly outperform single-engine static analysis runs. SCAR's generic, filesystem-driven pluggable findings convention allows students or platform engineers to easily integrate advanced tool blocks without breaking core repair loop logic.
