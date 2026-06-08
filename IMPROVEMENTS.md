# SCAR — Improvement Roadmap

Ordered from lowest to highest implementation effort.

---

## Low Hanging Fruit

### Deduplicate Intra-IKOS findings by file+line
When multiple native IKOS checkers fire on the exact same line (e.g. `boa` and `uva` both flagging `oob_read.c:7`), the lower-priority check wastes multiple LLM calls generating a redundant patch. We should apply a set-based coordinate filter in `__main__.py` right after loading the SARIF bridge array to keep only the highest-severity finding per unique `(file_path, line)` statement.

### Pre-expand macros with `clang -E`
The LLM frequently encounters code statements containing fixed-size allocations like `char buf[MAX_SIZE]` and is forced to issue an agentic `GREP: MAX_SIZE` directive to resolve its value. Running a localized `clang -E` pass before analysis produces concrete integer definitions inline. Passing the macro-expanded source string alongside the original codebase allows the LLM to read the human form while immediately seeing the mathematically resolved constant limits.

### Suppress `uva` (uninitialized variable) alerts on proven `boa` sinks
The native `uva` checker frequently flags uninitialized states on out-of-bounds array reads that `boa` has already definitively proven to be buffer overflows. Defining an explicit static priority hierarchy (`boa > dbz > nullity > dfa > sio > uva`) and automatically dropping lower-priority duplicate alerts on identical lines will optimize token efficiency.

---

## Medium Effort

### Parallel repair loop (per-file concurrency)
The current repair loop is strictly single-threaded and processes each finding sequentially, causing LLM API execution limits to dominate overall pipeline wall-clock time. The safe model for acceleration is to group findings by their absolute `file_path` and process each file-group concurrently using a `ThreadPoolExecutor` worker pool. This preserves unique sibling temp-file naming boundaries (`.scar_tmp_*`) per thread and cuts LLM spending in half by running exactly one `context_gen` call per source file instead of per individual finding.

### Patch dependency tracking & compounding updates
When multiple distinct vulnerabilities exist in different functions of the same file, independent patches will conflict or step on each other's line hunk coordinates. By building on top of the file-grouped sequential threading layout, the engine should apply each accepted patch to its local disk scratchpad immediately after a successful triage phase, forcing subsequent patch synthesis tasks for that file to evaluate the continuously healed state.

### Caller context injection (1-level call graph)
The context generation phase is currently blind to cross-module execution origins. For sophisticated logic validation, the model needs to know: who calls the vulnerable function, and what values are passed inside the caller argument array? Utilizing `clang --analyze` or an AST map to extract direct callers and inject their functional definitions into the prompt allows the triage stage to correctly filter out un-reachable code pathways.

### Function-boundary chunking for large files
Passing complete files to the LLM can easily overflow active token context windows or introduce noise on massive source frameworks. Utilizing the Clang AST compiler hooks to slice source modules explicitly at function boundaries keeps related functional clusters intact while dropping completely irrelevant background blocks.

### RAG over accepted patches
Store each accepted patch in a vector database indexed by its corresponding CWE rule ID and structural code snippet layout. When processing a newly discovered vulnerability, retrieving the top 2–3 most closely aligned historical patches and injecting them as multi-shot programming solutions inside `patch_gen` allows the agent to adapt natively to a specific engineering team's variable styles and design conventions.

---

## Higher Complexity

### Program slicing around vulnerable statements
Instead of feeding complete source paths to the model, extract a precise, minimized program slice relevant to the vulnerability location. By tracing data-flow tracking trees and control dependencies inside the LLVM IR infrastructure, the slice isolates the exact group of statements that affect the variable states at the vulnerable line, dramatically lowering prompt contexts and squeezing large frameworks into narrow token windows.

### Cross-file data flow tracking
Untrusted remote inputs frequently traverse multiple structural definitions and files before hitting a dangerous execution sink (e.g., an network read inside `io.c` writes to a global struct passed to `parser.c`, which triggers a vulnerability inside `util.c`). Integrating full data-flow tracking graphs (via platforms like CodeQL, Joern, or static LLVM DataFlowSanitizers) is required to capture interprocedural taint trajectories.

### Automated patch application and re-scan validation
The final acceptance of a candidate patch is currently determined exclusively by speculative LLM triage rounds. Closing the verification loop completely requires applying the diff, compiling the target, and re-running the whole-program linked static analysis pass (`ikos`) on the newly healed state to verify mathematically that the original error has been completely eradicated from the system.

### ESBMC integration as a pluggable findings source
Efficient SMT-Based C Model Checking (ESBMC) compiles targets into GOTO-IR structures and leverages bounded model checking to output concrete mathematical counterexamples. This represents an incredibly strong signal for patch generation because the model receives an absolute, step-by-step crash path tracking concrete variable inputs. This can be clean integrated as a standalone task dropping a `.scar/findings-esbmc.json` log payload.

### Fuzzing integration (libAFL / AFL++)
Integrating dynamic fuzzing platforms like AFL++ or libAFL allows the system to catch deep, stateful, input-dependent vulnerabilities that static abstract interpretation blocks cannot model. The task would run an instrumented execution binary for a bounded execution window, isolate distinct crashes, and output them directly into the pipeline loop using the shared `.scar/findings-fuzzer.json` file-system layout.

---

## Notes
* DARPA AIxCC results prove that hybrid ensembles leveraging directed fuzzing alongside LLM orchestration significantly outperform single-engine static analysis runs. SCAR's generic, filesystem-driven pluggable findings convention allows students or platform engineers to easily integrate advanced tool blocks without breaking core repair loop logic.
