#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

echo "==> Installing K3s Node (Disabling default Flannel, Traefik, ServiceLB, Local Storage)..."
curl -sfL https://get.k3s.io | sh -s - server \
  --flannel-backend=none \
  --disable-network-policy \
  --disable=servicelb \
  --disable=traefik \
  --disable=local-storage

echo "==> Configuring Kubeconfig permissions..."
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config
export KUBECONFIG=~/.kube/config

echo "==> Configuring CoreDNS upstream resolvers..."
kubectl apply -f "$REPO_ROOT/kubernetes/bootstrap/coredns-custom.yaml"

echo "==> Deploying Calico CNI Operator..."
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/tigera-operator.yaml

echo "==> Waiting for Calico CRDs and operator..."
kubectl wait --for=condition=established crd/installations.operator.tigera.io --timeout=120s
kubectl wait --for=condition=established crd/apiservers.operator.tigera.io --timeout=120s
kubectl wait --for=condition=available deployment/tigera-operator -n tigera-operator --timeout=120s

echo "==> Applying Calico installation configuration..."
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/custom-resources.yaml

echo "==> Waiting for Calico pods to initialize..."
kubectl rollout status daemonset/calico-node -n calico-system --timeout=120s

echo "==> K3s + Calico Bootstrap Complete!"
