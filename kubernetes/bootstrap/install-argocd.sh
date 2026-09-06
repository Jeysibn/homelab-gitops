#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
ARGOCD_VERSION="v3.5.1"

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

echo "==> Verifying the repo server can resolve GitHub before creating the root app..."
kubectl exec -n argocd deployment/argocd-repo-server -- \
  getent hosts github.com >/dev/null

echo "==> Applying Root Application (App-of-Apps pattern)..."
kubectl apply -f "$REPO_ROOT/kubernetes/bootstrap/root-app.yaml"

echo "==> Argo CD bootstrap complete!"
