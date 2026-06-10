#!/bin/bash
# Probe vLLM structured output for patch generation.
#
# Two modes:
#   1. --trace <path/to/2-patch-gen.md>
#      Reads the exact system+user prompt from a SCAR trace file and replays it
#      with json_schema constrained generation. Also shows the original response
#      from the trace for direct comparison.
#
#   2. (no args) built-in example
#      Uses a hand-crafted parse.c:46 strcpy snippet as a self-contained smoke test.
#
# Usage:
#   LLM_BASE_URL=http://... LLM_API_KEY=... LLM_PATCH_MODEL=... \
#     ./scripts/test-structured-patch.sh [--trace scar-traces/02-parse-46-llm-scan/2-patch-gen.md]
#
# Env vars mirror what SCAR uses (LLM_MODEL as fallback):
#   LLM_BASE_URL    required  Base URL of the OpenAI-compatible endpoint
#   LLM_API_KEY     required  API key (can be "dummy" for local vLLM)
#   LLM_PATCH_MODEL optional  Falls back to LLM_MODEL
#   LLM_MODEL       optional  Fallback model name

set -euo pipefail

BASE_URL="${LLM_BASE_URL:?set LLM_BASE_URL}"
API_KEY="${LLM_API_KEY:?set LLM_API_KEY}"
MODEL="${LLM_PATCH_MODEL:-${LLM_MODEL:?set LLM_PATCH_MODEL or LLM_MODEL}}"

TRACE_FILE=""
FAILURE_HINT=""
while [[ $# -gt 0 ]]; do
    case "${1:-}" in
        --trace)        TRACE_FILE="${2:?--trace requires a path argument}"; shift 2 ;;
        --failure-hint) FAILURE_HINT="${2:?--failure-hint requires a string}"; shift 2 ;;
        *) echo "[error] unknown argument: $1"; exit 1 ;;
    esac
done

# Normalise endpoint: LLM_BASE_URL includes /v1 (OpenAI client convention),
# so append only /chat/completions. Strip trailing slash first.
ENDPOINT="${BASE_URL%/}"
[[ "$ENDPOINT" == */v1 ]] || ENDPOINT="$ENDPOINT/v1"
ENDPOINT="$ENDPOINT/chat/completions"

echo "[test] endpoint : $ENDPOINT"
echo "[test] model    : $MODEL"
if [[ -n "$TRACE_FILE" ]]; then
    echo "[test] mode     : trace file: $TRACE_FILE"
else
    echo "[test] mode     : built-in example"
fi
if [[ -n "$FAILURE_HINT" ]]; then
    echo "[test] hint     : $FAILURE_HINT"
fi
echo ""

# ── JSON schema for structured output ────────────────────────────────────────
# Language-model must produce reasoning before committing to line edits.
# Each change targets one source line: old (exact original) → new (replacement).
# Multiple replacement lines go in a single "new" value joined with \n.
SCHEMA='{
  "type": "object",
  "properties": {
    "reasoning": {
      "type": "string",
      "description": "Step-by-step explanation of the vulnerability and fix strategy"
    },
    "changes": {
      "type": "array",
      "description": "One entry per source line that must change",
      "items": {
        "type": "object",
        "properties": {
          "line": { "type": "integer", "description": "1-based line number" },
          "old":  { "type": "string",  "description": "Exact original line including indentation" },
          "new":  { "type": "string",  "description": "Replacement; use \\n for multiple lines" }
        },
        "required": ["line", "old", "new"],
        "additionalProperties": false
      }
    }
  },
  "required": ["reasoning", "changes"],
  "additionalProperties": false
}'

# ── Extract prompts ───────────────────────────────────────────────────────────
if [[ -n "$TRACE_FILE" ]]; then
    # Parse the markdown trace produced by scar/llm.py write_trace().
    # Sections are delimited by "---\n\n## <Role>" headers.
    # The Response section is extracted separately for comparison display.
    read -r -d '' PROMPTS_JSON << 'PYEOF' || true
PYEOF
    PROMPTS_JSON=$(python3 - "$TRACE_FILE" << 'PYEOF'
import sys, re, json

text = open(sys.argv[1], encoding="utf-8").read()

def extract(label):
    # Section separator is "---\n\n## NextSection" — NOT a bare "---" which
    # also appears inside unified diffs as "--- a/file". Match only the
    # sequence that precedes another "## " heading or the end of file.
    pat = rf"## {label}\n\n(.*?)(?=\n---\n\n##|\Z)"
    m = re.search(pat, text, re.DOTALL)
    return m.group(1).strip() if m else ""

system   = extract("System")
user     = extract("User")
original = extract("Response")

print(json.dumps({"system": system, "user": user, "original": original}))
PYEOF
    )

    SYSTEM_PROMPT=$(echo "$PROMPTS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['system'])")
    USER_PROMPT=$(echo   "$PROMPTS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['user'])")
    ORIGINAL=$(echo      "$PROMPTS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['original'])")

    # When simulating the structured retry (--failure-hint set), swap the system
    # prompt for STRUCTURED_SYSTEM_PROMPT from patch_gen.py — that is the prompt
    # the real retry uses, not the diff-format prompt stored in the trace.
    if [[ -n "$FAILURE_HINT" ]]; then
        # Derive repo root from the script's own location so this works regardless
        # of the working directory (e.g. running from $home/openshift/ with the
        # repo at $home/openshift/SCAR/).
        REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
        STRUCTURED_PROMPT=$(python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
from scar.patch_gen import STRUCTURED_SYSTEM_PROMPT
print(STRUCTURED_SYSTEM_PROMPT)
" "$REPO_ROOT" 2>/dev/null)
        if [[ -n "$STRUCTURED_PROMPT" ]]; then
            echo "[test] using STRUCTURED_SYSTEM_PROMPT from patch_gen.py (retry simulation)"
            SYSTEM_PROMPT="$STRUCTURED_PROMPT"
        else
            echo "[test] warning: could not import STRUCTURED_SYSTEM_PROMPT — using trace system prompt"
        fi
        echo ""
    fi

    echo "=== Original response (from trace) ==="
    echo "$ORIGINAL"
    echo ""

else
    # Built-in: parse.c:46 strcpy into a fixed-size key buffer.
    # Known failure: model produces strncpy but omits the NUL terminator.
    SYSTEM_PROMPT='You are an expert C security engineer. Fix the reported vulnerability minimally.

Rules:
- Never use strcpy, strcat, sprintf, gets
- When replacing strcpy: use strncpy(dst, src, sizeof(dst)-1) AND explicitly
  null-terminate: dst[sizeof(dst)-1] = '"'"'\0'"'"'; — both lines are required
- Preserve all existing function signatures and struct layouts
- Fix ONLY the reported line — no unrelated changes'

    USER_PROMPT='Finding: CWE-121 Stack Buffer Overflow
File: parse.c, Line 13
Message: strcpy into fixed-size key field with no bounds check — input length unconstrained

Source:
```c
#define MAX_KEY_LEN 64

typedef struct {
    char key[MAX_KEY_LEN];
    int  value;
} Record;

/* Parse one |-delimited field from src into out->key. */
int parse_field(char **src, Record *out) {
    char *tok = strtok_r(NULL, "|", src);
    if (!tok) return -1;
    strcpy(out->key, tok);          /* line 13 — vulnerable */
    return 0;
}
```'
fi

# ── Helper: send one request, return response or empty string on HTTP error ───
send_request() {
    local payload="$1"
    curl -s \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$ENDPOINT"
}

has_choices() {
    python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'choices' in d else 1)" 2>/dev/null
}

# ── Step 1: verify basic connectivity ────────────────────────────────────────
echo "[test] step 1/3 — verifying basic connectivity..."
PING_PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'model': '$MODEL',
    'temperature': 0.1,
    'max_tokens': 10,
    'messages': [{'role': 'user', 'content': 'Reply with the word OK.'}],
}))
")
PING=$(send_request "$PING_PAYLOAD")
if ! echo "$PING" | has_choices; then
    echo "[FAIL] basic request failed — endpoint or model name is wrong"
    echo "$PING"
    exit 1
fi
echo "[test] connectivity OK"
echo ""

# ── Step 2: try json_schema (full constrained generation) ────────────────────
echo "[test] step 2/3 — trying response_format: json_schema..."
PAYLOAD_SCHEMA=$(python3 -c "
import json, sys
system = sys.argv[1]; user = sys.argv[2]
schema = json.loads(sys.argv[3]); model = sys.argv[4]; hint = sys.argv[5]
if hint:
    # Inject failure hint the same way generate_structured() does in patch_gen.py,
    # prepending it just before the source file block.
    marker = 'Source file ('
    idx = user.find(marker)
    if idx >= 0:
        user = user[:idx] + f'Previous attempt failed — {hint}. Do not repeat this mistake.\n\n' + user[idx:]
    else:
        user = f'Previous attempt failed — {hint}. Do not repeat this mistake.\n\n' + user
print(json.dumps({
    'model': model, 'temperature': 0.1,
    'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
    'response_format': {'type': 'json_schema', 'json_schema': {'name': 'patch', 'strict': True, 'schema': schema}},
}))
" "$SYSTEM_PROMPT" "$USER_PROMPT" "$SCHEMA" "$MODEL" "$FAILURE_HINT")

RESPONSE=$(send_request "$PAYLOAD_SCHEMA")
SCHEMA_MODE="json_schema"

if ! echo "$RESPONSE" | has_choices; then
    echo "[test] json_schema not supported — falling back to json_object mode"
    echo ""

    # ── Step 3: fallback to json_object + prompt-level instruction ───────────
    echo "[test] step 3/3 — trying response_format: json_object..."
    PAYLOAD_OBJ=$(python3 -c "
import json, sys
system = sys.argv[1]; user = sys.argv[2]; model = sys.argv[3]; hint = sys.argv[4]
if hint:
    marker = 'Source file ('
    idx = user.find(marker)
    if idx >= 0:
        user = user[:idx] + f'Previous attempt failed — {hint}. Do not repeat this mistake.\n\n' + user[idx:]
    else:
        user = f'Previous attempt failed — {hint}. Do not repeat this mistake.\n\n' + user
# Append schema as instruction since the endpoint won't enforce it
system_ext = system + '''

You MUST respond with a single JSON object matching exactly this structure:
{
  \"reasoning\": \"<string>\",
  \"changes\": [
    { \"line\": <integer>, \"old\": \"<string>\", \"new\": \"<string>\" }
  ]
}
No markdown, no explanation outside the JSON object.'''
print(json.dumps({
    'model': model, 'temperature': 0.1,
    'messages': [{'role': 'system', 'content': system_ext}, {'role': 'user', 'content': user}],
    'response_format': {'type': 'json_object'},
}))
" "$SYSTEM_PROMPT" "$USER_PROMPT" "$MODEL" "$FAILURE_HINT")

    RESPONSE=$(send_request "$PAYLOAD_OBJ")
    SCHEMA_MODE="json_object (prompt-enforced)"

    if ! echo "$RESPONSE" | has_choices; then
        echo "[FAIL] json_object mode also failed"
        echo "$RESPONSE"
        exit 1
    fi
fi

echo "[test] mode used: $SCHEMA_MODE"
echo ""

# ── Parse and display ─────────────────────────────────────────────────────────
python3 -c "
import json, sys

response = json.loads(sys.argv[1])
raw      = response['choices'][0]['message']['content']
usage    = response.get('usage', {})

try:
    content = json.loads(raw)
except json.JSONDecodeError as e:
    print(f'[FAIL] response is not valid JSON: {e}')
    print(raw)
    sys.exit(1)

sep = '=' * 66

print()
print(sep)
print('  REASONING')
print(sep)
print(content.get('reasoning', '(none)'))

print()
print(sep)
print('  CHANGES')
print(sep)
for c in content.get('changes', []):
    print(f\"  line {c['line']}:\")
    print(f\"    - {c['old'].strip()}\")
    for nl in c['new'].split('\\\\n'):
        print(f\"    + {nl}\")
print()

# Correctness checks — adjust per vulnerability if using a custom trace
all_new = ' '.join(c['new'] for c in content.get('changes', []))
checks = [
    ('no strcpy in fix',    'strcpy'  not in all_new),
    ('safe copy used',      any(fn in all_new for fn in ('strncpy', 'snprintf', 'memcpy'))),
    # snprintf is inherently null-terminating; strncpy needs an explicit '\0'
    ('null-terminates',     'snprintf' in all_new
                            or \"= '\\\\0'\" in all_new
                            or '[sizeof' in all_new and \"'\\\\0'\" in all_new),
    ('sizeof used',         'sizeof'  in all_new),
    ('has reasoning',       len(content.get('reasoning','')) > 20),
]

print(sep)
print('  CORRECTNESS CHECKS')
print(sep)
all_passed = True
for label, passed in checks:
    mark = 'PASS' if passed else 'FAIL'
    print(f'  [{mark}] {label}')
    if not passed:
        all_passed = False

print(sep)
print(f\"  tokens: {usage.get('prompt_tokens',0):,} prompt + {usage.get('completion_tokens',0):,} completion\")
print(sep)
print()
sys.exit(0 if all_passed else 1)
" "$RESPONSE"
