#!/bin/bash
# tests/mock_pipeline.sh — Simulate the Tekton PVC workspace layout locally.
#
# Usage:
#   tests/mock_pipeline.sh <source-dir> [--run-repair]
#
# Arguments:
#   source-dir    Path to the C source you want to scan (e.g. ~/scarnet)
#   --run-repair  Also invoke the SCAR repair loop against the mock workspace
#                 (requires LLM_BASE_URL, LLM_API_KEY, LLM_PATCH_MODEL,
#                 LLM_REVIEW_MODEL to be set in the environment)
#
# The script recreates the directory layout the Tekton PVC provides so you
# can test your custom scanner without a cluster commit/push/wait cycle.
# Edit the "Executing student tool" section below to call your scanner.

set -euo pipefail

# ── Arguments ──────────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
    echo "Usage: $0 <source-dir> [--run-repair]"
    echo ""
    echo "  source-dir    Path to the C source directory to scan"
    echo "  --run-repair  Also run the SCAR repair loop after your tool"
    exit 1
fi

SRC_DIR="$1"
RUN_REPAIR=false
shift
for arg in "$@"; do
    case "$arg" in
        --run-repair) RUN_REPAIR=true ;;
        *) echo "[!] Unknown argument: $arg"; exit 1 ;;
    esac
done

if [ ! -d "$SRC_DIR" ]; then
    echo "[-] Source directory not found: $SRC_DIR"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Workspace layout (mirrors Tekton PVC mount) ────────────────────────────
export SCAR_WORKSPACE="/tmp/scar-mock-ws"
export SCAR_SRC="$SCAR_WORKSPACE/src"
export SANDBOX_SRC="/tmp/osscrs-sandbox"
export PYTHONPATH="$REPO_ROOT/scar/libCRS_bridge${PYTHONPATH:+:$PYTHONPATH}"

echo "[*] Initializing mock workspace at $SCAR_WORKSPACE"
rm -rf "$SCAR_WORKSPACE" "$SANDBOX_SRC"
mkdir -p "$SCAR_WORKSPACE/.scar" "$SCAR_SRC" "$SANDBOX_SRC"

echo "[*] Staging source: $SRC_DIR"
cp -r "$SRC_DIR/." "$SANDBOX_SRC/"
cp -r "$SRC_DIR/." "$SCAR_SRC/"

# ── Execute student tool ───────────────────────────────────────────────────
# Replace the placeholder below with your scanner invocation, for example:
#   python3 pipeline/tasks/my_custom_scanner.py
#   /usr/local/bin/my-tool --src "$SANDBOX_SRC" --out "$SCAR_WORKSPACE/.scar/findings-mytool.json"
echo "[*] Executing student tool..."
echo "    (edit this script to replace the placeholder with your scanner)"
# python3 pipeline/tasks/my_custom_scanner.py

# ── Verify findings output ─────────────────────────────────────────────────
echo "[*] Verifying findings output..."
if ls "$SCAR_WORKSPACE"/.scar/findings-*.json >/dev/null 2>&1; then
    echo "[+] Findings file(s) found:"
    for f in "$SCAR_WORKSPACE"/.scar/findings-*.json; do
        count=$(python3 -c "import json; print(len(json.load(open('$f'))))")
        echo "    $(basename "$f")  ($count finding(s))"
    done
else
    echo "[-] No findings-*.json found in $SCAR_WORKSPACE/.scar/"
    echo "    Your tool must write at least one file matching that pattern."
    exit 1
fi

# ── Validate schema ────────────────────────────────────────────────────────
VALIDATOR="$REPO_ROOT/scar/validate_schema.py"
if [ -f "$VALIDATOR" ]; then
    echo "[*] Validating schema..."
    python3 "$VALIDATOR" "$SCAR_WORKSPACE"/.scar/findings-*.json
fi

# ── Optional: run repair loop end-to-end ──────────────────────────────────
if [ "$RUN_REPAIR" = "true" ]; then
    echo "[*] Running SCAR repair loop..."
    if [ -z "${LLM_BASE_URL:-}" ] || [ -z "${LLM_API_KEY:-}" ]; then
        echo "[!] LLM_BASE_URL and LLM_API_KEY must be set to run the repair loop."
        echo "    Schema validation passed — skipping repair."
        exit 0
    fi
    cd "$REPO_ROOT"
    python3 -u -m scar \
        "$SCAR_WORKSPACE/.scar/results.sarif" \
        "$SCAR_WORKSPACE" \
        --triage-rounds 2 \
        --min-confidence 0.6 \
        --output "$SCAR_WORKSPACE/.scar/scar-results.json"
    echo "[+] Repair complete. Results: $SCAR_WORKSPACE/.scar/scar-results.json"
fi

echo "[+] Mock pipeline run complete."
