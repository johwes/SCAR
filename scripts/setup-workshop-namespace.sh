#!/bin/bash
# Deploy a student namespace for the SCAR workshop.
#
# Usage:
#   ./scripts/setup-workshop-namespace.sh <team-name> [dashboard-namespace]
#
# Required env vars:
#   LLM_BASE_URL       OpenAI-compatible endpoint base URL
#   LLM_API_KEY        API key
#   LLM_MODEL          Model name (used for both patch and review if specific vars not set)
#
# Optional env vars:
#   LLM_PATCH_MODEL    Override model for patch generation
#   LLM_REVIEW_MODEL   Override model for triage review
#   DASHBOARD_URL      Full URL to dashboard (overrides default)
#
set -euo pipefail

TEAM="${1:?Usage: $0 <team-name> [dashboard-namespace]}"
DASHBOARD_NS="${2:-scar}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── Validate required env vars ────────────────────────────────────────────────
echo "[setup] Validating environment..."
missing=()
for var in LLM_BASE_URL LLM_API_KEY; do
    [[ -z "${!var:-}" ]] && missing+=("$var")
done

PATCH_MODEL="${LLM_PATCH_MODEL:-${LLM_MODEL:-}}"
REVIEW_MODEL="${LLM_REVIEW_MODEL:-${LLM_MODEL:-}}"
[[ -z "$PATCH_MODEL"  ]] && missing+=("LLM_PATCH_MODEL or LLM_MODEL")
[[ -z "$REVIEW_MODEL" ]] && missing+=("LLM_REVIEW_MODEL or LLM_MODEL")

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "[error] Missing required environment variables:"
    for v in "${missing[@]}"; do echo "        $v"; done
    exit 1
fi

DASHBOARD_URL="${DASHBOARD_URL:-http://scar-dashboard.${DASHBOARD_NS}.svc.cluster.local}"

echo "[setup] Team namespace : $TEAM"
echo "[setup] Dashboard URL  : $DASHBOARD_URL"
echo "[setup] Patch model    : $PATCH_MODEL"
echo "[setup] Review model   : $REVIEW_MODEL"
echo ""

# ── Create namespace ──────────────────────────────────────────────────────────
echo "[setup] Creating namespace '$TEAM'..."
oc new-project "$TEAM" 2>/dev/null || oc project "$TEAM"

# ── PVC ───────────────────────────────────────────────────────────────────────
echo "[setup] Applying PVC..."
oc apply -f "$REPO_ROOT/pipeline/pvc.yaml" -n "$TEAM"

# ── Tasks ─────────────────────────────────────────────────────────────────────
echo "[setup] Applying Tekton tasks..."
oc apply -f "$REPO_ROOT/pipeline/tasks/" -n "$TEAM"

# ── Pipelines ─────────────────────────────────────────────────────────────────
echo "[setup] Applying pipelines..."
oc apply -f "$REPO_ROOT/pipeline/pipeline-v1-llm-only.yaml" -n "$TEAM"
oc apply -f "$REPO_ROOT/pipeline/pipeline-v2-full.yaml"     -n "$TEAM"
oc apply -f "$REPO_ROOT/pipeline/pipeline-v3-extended.yaml" -n "$TEAM"

# ── LLM credentials secret ────────────────────────────────────────────────────
echo "[setup] Creating LLM credentials secret (scar-llm-credentials)..."
oc create secret generic scar-llm-credentials \
    --from-literal=base_url="$LLM_BASE_URL" \
    --from-literal=api_key="$LLM_API_KEY" \
    --from-literal=patch_model="$PATCH_MODEL" \
    --from-literal=review_model="$REVIEW_MODEL" \
    -n "$TEAM" --dry-run=client -o yaml | oc apply -f -

# ── Dashboard ConfigMap ───────────────────────────────────────────────────────
echo "[setup] Creating dashboard ConfigMap (scar-dashboard)..."
oc create configmap scar-dashboard \
    --from-literal=url="$DASHBOARD_URL" \
    -n "$TEAM" --dry-run=client -o yaml | oc apply -f -

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "[done] Namespace '$TEAM' is ready."
echo ""
echo "Start a pipeline run:"
echo ""
echo "  tkn pipeline start scar-v2 \\"
echo "    --namespace $TEAM \\"
echo "    --param repo-url=<target-repo-url> \\"
echo "    --workspace name=shared-data,claimName=scar-pvc \\"
echo "    --use-param-defaults \\"
echo "    --pipeline-timeout 3h \\"
echo "    --showlog"
echo ""
