"""Stage 2: LLM-driven vulnerability discovery (adapted from nano-analyzer).

Scans C source files for zero-day vulnerabilities using an LLM, producing
findings that supplement IKOS static analysis — particularly for bug classes
IKOS cannot model (string functions, logic errors, protocol bugs).
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import llm, grep_tool

SCAN_SYSTEM_PROMPT = """\
You are a security researcher hunting for zero-day vulnerabilities. \
Analyze the code step by step, tracing how untrusted data flows into \
each function. For every function, ask yourself:

1. Can any parameter be NULL, too large, negative, or otherwise \
   invalid when this function is called with malformed input?
2. Are there copies into fixed-size buffers without size validation?
3. Can integer arithmetic overflow, wrap, or produce negative values \
   that are then used as sizes or indices?
4. Are tagged unions / variant types accessed without verifying the \
   type discriminator first?
5. Are return values from fallible operations checked before use?

Focus on bugs that an external attacker can trigger through untrusted \
input. Deprioritize static helpers with safe call sites, allocation \
wrappers, platform-specific dead code, and theoretical issues.

After your analysis, output a JSON array of findings. Each finding \
must have: severity, title, function, line, description. \
severity must be one of: critical, high, medium, low, informational. \
Output ONLY the JSON array at the end — your reasoning goes before it.\
"""

_FEWSHOT_USER = """\
Analyze the following source file for zero-day vulnerabilities.

File: example/net/parser.c

```c
void parse_packet(struct packet *pkt, const char *data, int len) {
    char header[64];
    memcpy(header, data, len);
    process_header(header);
}

int handle_request(struct request *req) {
    struct session *sess = lookup_session(req->session_id);
    return sess->handler(req);
}

static void log_debug(const char *msg) {
    if (msg) printf("%s\\n", msg);
}
```

Provide a detailed security analysis.\
"""

_FEWSHOT_ASSISTANT = """\
`parse_packet`: `data` and `len` come from the network. Copies `len` \
bytes into 64-byte stack buffer with no bounds check — overflow if \
`len > 64`. `handle_request`: `lookup_session()` can return NULL but \
result is dereferenced. `log_debug`: safe, already checks NULL.

```json
[
  {"severity": "critical", "title": "Stack buffer overflow via unchecked len", "function": "parse_packet()", "line": 3, "description": "memcpy copies attacker-controlled len bytes into 64-byte stack buffer without bounds check"},
  {"severity": "high", "title": "NULL deref on failed session lookup", "function": "handle_request()", "line": 8, "description": "lookup_session() may return NULL for unknown session_id but result is dereferenced unconditionally"}
]
```\
"""

_JSON_RE = re.compile(r"```json\s*(\[.*?\])\s*```", re.DOTALL)
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "informational": 4}
_TRIAGE_THRESHOLD = 2  # medium and above


@dataclass
class LLMFinding:
    severity: str
    title: str
    function: str
    line: int
    description: str
    file_path: str


def scan(source_path: str | Path, briefing: str, repo_dir: str | Path) -> list[LLMFinding]:
    """Scan a single C source file for vulnerabilities using the LLM.

    Uses the security briefing from context_gen (Stage 1) as additional
    context, then emits findings as a JSON array.
    """
    source = Path(source_path).read_text(encoding="utf-8", errors="replace")

    user_content = (
        f"Analyze the following source file for zero-day vulnerabilities.\n\n"
        f"File: {source_path}\n\n"
        f"```c\n{source}\n```\n\n"
        f"Security briefing:\n{briefing}\n\n"
        f"Provide a detailed security analysis."
    )

    messages = [
        {"role": "system", "content": SCAN_SYSTEM_PROMPT},
        {"role": "user", "content": _FEWSHOT_USER},
        {"role": "assistant", "content": _FEWSHOT_ASSISTANT},
        {"role": "user", "content": user_content},
    ]

    response = llm.chat(messages, temperature=0.1)

    directives = grep_tool.extract_directives(response)
    if directives:
        grep_results = grep_tool.execute(directives, repo_dir)
        if grep_results:
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": f"Grep results:\n{grep_results}\n\nRevise your JSON findings if needed.",
            })
            response = llm.chat(messages, temperature=0.1)

    return _parse(response, str(source_path))


def _parse(response: str, file_path: str) -> list[LLMFinding]:
    raw = None
    m = _JSON_RE.search(response)
    if m:
        try:
            raw = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    if raw is None:
        start = response.rfind("[")
        end = response.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                raw = json.loads(response[start:end])
            except json.JSONDecodeError:
                pass

    if not raw:
        return []

    findings = []
    for item in raw:
        severity = item.get("severity", "medium").lower()
        if severity not in _SEVERITY_RANK:
            severity = "medium"
        if _SEVERITY_RANK[severity] > _TRIAGE_THRESHOLD:
            continue  # skip low / informational
        findings.append(LLMFinding(
            severity=severity,
            title=item.get("title", "unknown"),
            function=item.get("function", ""),
            line=int(item.get("line", 0)),
            description=item.get("description", ""),
            file_path=file_path,
        ))
    return findings
