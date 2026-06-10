#!/bin/bash
# Probe vLLM structured output for patch generation.
#
# Tests whether constrained JSON generation improves patch quality versus the
# current free-text unified-diff approach. Uses parse.c:46 (strcpy into a
# fixed-size key buffer) — a known failure where the model bounds the copy
# but misses the null-terminator when generating unstructured output.
#
# Usage:
#   LLM_BASE_URL=http://... LLM_API_KEY=... LLM_PATCH_MODEL=... \
#     ./scripts/test-structured-patch.sh
#
# Env vars mirror what SCAR uses (LLM_MODEL as fallback for both roles):
#   LLM_BASE_URL    required  Base URL of the OpenAI-compatible endpoint
#   LLM_API_KEY     required  API key (can be "dummy" for local vLLM)
#   LLM_PATCH_MODEL optional  Model name; falls back to LLM_MODEL
#   LLM_MODEL       optional  Fallback model name

set -euo pipefail

BASE_URL="${LLM_BASE_URL:?set LLM_BASE_URL}"
API_KEY="${LLM_API_KEY:?set LLM_API_KEY}"
MODEL="${LLM_PATCH_MODEL:-${LLM_MODEL:?set LLM_PATCH_MODEL or LLM_MODEL}}"

echo "[test] endpoint : $BASE_URL"
echo "[test] model    : $MODEL"
echo ""

# ── Vulnerable snippet ────────────────────────────────────────────────────────
# Minimal reproduction of parse.c:46 from scarnet.
# The known failure: the model produces strncpy but omits the NUL terminator,
# leaving the buffer vulnerable to out-of-bounds reads on truncation.
read -r -d '' SNIPPET << 'EOF'
#define MAX_KEY_LEN 64

typedef struct {
    char key[MAX_KEY_LEN];
    int  value;
} Record;

/* Parse one '|'-delimited field from src into out->key. */
int parse_field(char **src, Record *out) {
    char *tok = strtok_r(NULL, "|", src);
    if (!tok) return -1;
    strcpy(out->key, tok);          /* line 13 — vulnerable */
    return 0;
}
EOF

# ── JSON schema for structured output ────────────────────────────────────────
# The model must reason before committing to changes.
# Each change targets one line: old (exact original) → new (replacement text).
# Multiple new lines go in a single "new" value separated by \n.
read -r -d '' SCHEMA << 'EOF'
{
  "type": "object",
  "properties": {
    "reasoning": {
      "type": "string",
      "description": "Step-by-step explanation of the vulnerability and the fix"
    },
    "changes": {
      "type": "array",
      "description": "One entry per source line that must change",
      "items": {
        "type": "object",
        "properties": {
          "line":  { "type": "integer", "description": "1-based line number in the snippet" },
          "old":   { "type": "string",  "description": "Exact original line (including indentation)" },
          "new":   { "type": "string",  "description": "Replacement text; use \\n to insert multiple lines" }
        },
        "required": ["line", "old", "new"],
        "additionalProperties": false
      }
    }
  },
  "required": ["reasoning", "changes"],
  "additionalProperties": false
}
EOF

# ── Build request payload ─────────────────────────────────────────────────────
PAYLOAD=$(python3 - << PYEOF
import json, sys

system = """\
You are an expert C security engineer. Fix the reported vulnerability minimally.

Rules:
- Never use strcpy, strcat, sprintf, gets
- When replacing strcpy: use strncpy(dst, src, sizeof(dst)-1) AND explicitly
  null-terminate: dst[sizeof(dst)-1] = '\\0'; — both lines are required
- Preserve all existing function signatures and struct layouts
- Fix ONLY the reported line — no unrelated changes
"""

user = """Finding: CWE-121 Stack Buffer Overflow
File: parse.c, Line 13
Message: strcpy into fixed-size key field with no bounds check — input length unconstrained

Source:
\`\`\`c
""" + """$SNIPPET""" + """
\`\`\`"""

schema = $SCHEMA

payload = {
    "model": "$MODEL",
    "temperature": 0.1,
    "messages": [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ],
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name":   "patch",
            "strict": True,
            "schema": schema,
        },
    },
}

print(json.dumps(payload))
PYEOF
)

# ── Send request ──────────────────────────────────────────────────────────────
echo "[test] sending request..."
RESPONSE=$(curl -s \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$BASE_URL/v1/chat/completions")

# Check for API-level error
if echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'choices' in d else 1)" 2>/dev/null; then
    true
else
    echo "[test] ERROR: unexpected response:"
    echo "$RESPONSE" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || echo "$RESPONSE"
    exit 1
fi

# ── Parse and display ─────────────────────────────────────────────────────────
python3 - << PYEOF
import json, sys

response = json.loads("""$RESPONSE""")
content  = json.loads(response["choices"][0]["message"]["content"])
usage    = response.get("usage", {})

sep = "=" * 66

print()
print(sep)
print("  REASONING")
print(sep)
print(content["reasoning"])

print()
print(sep)
print("  CHANGES")
print(sep)
for c in content["changes"]:
    print(f"  line {c['line']}:")
    print(f"    - {c['old'].strip()}")
    for nl in c["new"].split("\\n"):
        print(f"    + {nl.strip()}")
print()

# ── Correctness checks ────────────────────────────────────────────────────────
print(sep)
print("  CORRECTNESS CHECKS")
print(sep)

all_new = " ".join(c["new"] for c in content["changes"])

checks = [
    ("no strcpy",          "strcpy"  not in all_new),
    ("uses strncpy",       "strncpy" in all_new),
    ("null-terminates",    "= '\\\\0'" in all_new or '= "\\\\0"' in all_new or "[sizeof" in all_new and "\\\\0" in all_new),
    ("uses sizeof",        "sizeof"  in all_new),
    ("exactly 1 change",   len(content["changes"]) == 1),
]

all_passed = True
for label, passed in checks:
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {label}")
    if not passed:
        all_passed = False

print(sep)
print(f"  tokens: {usage.get('prompt_tokens',0):,} prompt + {usage.get('completion_tokens',0):,} completion")
print(sep)
print()

sys.exit(0 if all_passed else 1)
PYEOF
