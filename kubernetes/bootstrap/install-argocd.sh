#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
ARGOCD_VERSION="v3.5.1"
NETWORK_VERIFY_SCRIPT="$REPO_ROOT/kubernetes/bootstrap/verify-cluster-network.sh"

[[ -f "$NETWORK_VERIFY_SCRIPT" ]] || {
  echo "ERROR: missing cluster network acceptance test: $NETWORK_VERIFY_SCRIPT" >&2
  exit 1
}

echo "==> Re-validating cluster networking before Argo CD installation..."
bash "$NETWORK_VERIFY_SCRIPT"

echo "==> Creating argocd namespace..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

echo "==> Installing Argo CD ${ARGOCD_VERSION}..."
kubectl apply -n argocd --server-side \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

echo "==> Waiting for Argo CD CRDs..."
kubectl wait --for=condition=Established crd/applications.argoproj.io --timeout=300s
kubectl wait --for=condition=Established crd/applicationsets.argoproj.io --timeout=300s

echo "==> Waiting for Argo CD reconciliation components..."
kubectl rollout status statefulset/argocd-application-controller -n argocd --timeout=300s
kubectl rollout status deployment/argocd-repo-server -n argocd --timeout=300s
kubectl rollout status deployment/argocd-server -n argocd --timeout=300s

echo "==> Verifying repo-server DNS before creating root-app..."
if ! kubectl exec -n argocd deployment/argocd-repo-server -- getent hosts github.com >/dev/null; then
  echo "ERROR: argocd-repo-server cannot resolve github.com. Refusing to create root-app." >&2
  kubectl logs -n argocd deployment/argocd-repo-server --tail=100 >&2 || true
  exit 1
fi

echo "==> Applying Root Application (App-of-Apps pattern)..."
kubectl apply -f "$REPO_ROOT/kubernetes/bootstrap/root-app.yaml"

echo "==> Waiting for the root application to fetch Git and reconcile..."
if ! kubectl wait application/root-app -n argocd \
  --for=jsonpath='{.status.sync.status}'=Synced --timeout=300s; then
  echo "ERROR: root-app did not reach Synced state." >&2
  kubectl get application root-app -n argocd -o yaml >&2 || true
  kubectl logs -n argocd deployment/argocd-repo-server --tail=100 >&2 || true
  exit 1
fi

kubectl get applications -n argocd

echo "==> Argo CD bootstrap complete!"
