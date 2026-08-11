#!/usr/bin/env bash
set -euo pipefail

echo "==> Creating argocd namespace..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

echo "==> Installing ArgoCD..."
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

echo "==> Waiting for ArgoCD components to be ready..."
kubectl rollout status deployment/argocd-server -n argocd --timeout=300s

echo "==> Applying Root Application (App-of-Apps pattern)..."
kubectl apply -f kubernetes/bootstrap/root-app.yaml

echo "==> ArgoCD bootstrap complete!"