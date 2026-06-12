#!/bin/bash
# Instructor setup: load pre-generated traces onto scar-workshop-pvc.
#
# Run once before the workshop from the root of the SCAR repo:
#
#   ./scripts/setup-workshop-pvc.sh
#
# Requires: oc or kubectl in PATH, cluster access, and RWX storage available
# in the namespace (the PVC requests ReadWriteMany).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARBALL="$REPO_ROOT/examples/trace-scar-v1-v2-scar-test-c.tar.xz"
PVC_YAML="$REPO_ROOT/pipeline/pvc-workshop.yaml"

if ! command -v oc &>/dev/null && ! command -v kubectl &>/dev/null; then
    echo "[error] oc or kubectl not found in PATH"
    exit 1
fi

CMD=$(command -v oc 2>/dev/null || command -v kubectl)

if [[ ! -f "$TARBALL" ]]; then
    echo "[error] tarball not found: $TARBALL"
    echo "        Run from the root of the SCAR repo."
    exit 1
fi

echo "[setup] Creating scar-workshop-pvc..."
$CMD apply -f "$PVC_YAML"

echo "[setup] Starting loader pod..."
$CMD apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: scar-workshop-loader
spec:
  containers:
    - name: loader
      image: registry.access.redhat.com/ubi9:latest
      command: ["sleep", "infinity"]
      volumeMounts:
        - name: workshop
          mountPath: /workshop
  volumes:
    - name: workshop
      persistentVolumeClaim:
        claimName: scar-workshop-pvc
  restartPolicy: Never
EOF

echo "[setup] Waiting for loader pod to be ready..."
$CMD wait --for=condition=Ready pod/scar-workshop-loader --timeout=120s

echo "[setup] Copying trace archive..."
$CMD cp "$TARBALL" scar-workshop-loader:/workshop/traces.tar.xz

echo "[setup] Extracting traces..."
$CMD exec scar-workshop-loader -- bash -c "
cd /workshop
python3 -c \"import tarfile; tarfile.open('traces.tar.xz', 'r:xz').extractall('.')\"
rm traces.tar.xz
echo '[setup] Contents:'
ls /workshop/
"

echo "[setup] Deleting loader pod..."
$CMD delete pod scar-workshop-loader

echo ""
echo "[setup] Done. scar-workshop-pvc is ready."
echo "        Apply the inspector pod to let students inspect traces:"
echo "        oc apply -f docs/scar-workshop-inspector-pod.yaml"
