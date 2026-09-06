#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_DIR="$REPO_ROOT/kubernetes/bootstrap"

if (( EUID == 0 )); then
  echo "ERROR: Run the bootstrap as your normal user, not with sudo." >&2
  echo "Use: bash $REPO_ROOT/bootstrap.sh" >&2
  exit 1
fi

for script in install-k3s.sh install-argocd.sh; do
  [[ -f "$BOOTSTRAP_DIR/$script" ]] || {
    echo "ERROR: Missing bootstrap script: $BOOTSTRAP_DIR/$script" >&2
    exit 1
  }
done

echo "==> Repository: $REPO_ROOT"
echo "==> Step 1/2: K3s + Calico"
bash "$BOOTSTRAP_DIR/install-k3s.sh"

echo
echo "==> Step 2/2: Argo CD"
bash "$BOOTSTRAP_DIR/install-argocd.sh"

echo
echo "==> Homelab bootstrap complete"
echo "    Argo CD will reconcile the GitOps applications from main."
