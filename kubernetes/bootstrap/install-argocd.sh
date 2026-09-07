#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ARGOCD_VERSION="v3.5.1"
ROOT_APP="$SCRIPT_DIR/root-app.yaml"

if (( EUID == 0 )); then
  echo "ERROR: Run this script as your normal user, not with sudo." >&2
  echo "Use: bash $SCRIPT_DIR/install-argocd.sh" >&2
  exit 1
fi

command -v kubectl >/dev/null 2>&1 || {
  echo "ERROR: kubectl is not available. Install K3s first." >&2
  exit 1
}

[[ -f "$ROOT_APP" ]] || {
  echo "ERROR: root-app.yaml not found: $ROOT_APP" >&2
  exit 1
}

kubectl get --raw=/readyz >/dev/null 2>&1 || {
  echo "ERROR: Kubernetes API is not reachable with the current kubeconfig." >&2
  exit 1
}

echo "==> Creating Argo CD namespace..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

echo "==> Installing Argo CD ${ARGOCD_VERSION}..."
kubectl apply --server-side --force-conflicts -n argocd \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

echo "==> Waiting for Application CRD..."
kubectl wait --for=condition=Established \
  crd/applications.argoproj.io \
  --timeout=120s

echo "==> Applying root application..."
kubectl apply -f "$ROOT_APP"

echo "==> Configuring repo-server health checks..."
kubectl patch configmap argocd-cmd-params-cm -n argocd --type merge \
  -p '{"data":{"reposerver.grpc.enable.txt.service.config":"false"}}'
kubectl rollout restart deployment/argocd-repo-server -n argocd

echo "==> Waiting for Argo CD..."
kubectl rollout status deployment/argocd-repo-server \
  -n argocd --timeout=300s

kubectl wait --for=condition=Ready pod \
  -n argocd \
  -l app.kubernetes.io/part-of=argocd \
  --timeout=300s

kubectl get applications -n argocd

echo "==> Argo CD bootstrap complete"
