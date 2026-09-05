#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

K3S_VERSION="v1.36.3+k3s1"
CALICO_VERSION="v3.32.1"
CLUSTER_CIDR="10.42.0.0/16"

echo "==> Installing K3s ${K3S_VERSION} (Disabling default Flannel, Traefik, ServiceLB, Local Storage)..."
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" sh -s - server \
  --cluster-cidr="${CLUSTER_CIDR}" \
  --flannel-backend=none \
  --disable-network-policy \
  --disable=servicelb \
  --disable=traefik \
  --disable=local-storage

echo "==> Configuring kubeconfig permissions..."
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown "$(id -u):$(id -g)" ~/.kube/config
export KUBECONFIG="$HOME/.kube/config"

echo "==> Waiting for Kubernetes API..."
kubectl wait --for=condition=Ready node --all --timeout=180s || true

echo "==> Configuring CoreDNS upstream resolvers..."
kubectl apply -f "$REPO_ROOT/kubernetes/bootstrap/coredns-custom.yaml"

echo "==> Deploying Calico ${CALICO_VERSION} operator..."
# Server-side apply with conflict takeover makes reruns deterministic even if
# the operator was previously created using client-side kubectl apply.
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"

echo "==> Waiting for Calico CRDs and operator..."
kubectl wait --for=condition=established crd/installations.operator.tigera.io --timeout=180s
kubectl wait --for=condition=established crd/apiservers.operator.tigera.io --timeout=180s
kubectl wait --for=condition=available deployment/tigera-operator -n tigera-operator --timeout=180s

echo "==> Applying repo-managed Calico installation configuration..."
kubectl apply -f "$REPO_ROOT/kubernetes/bootstrap/calico-installation.yaml"

echo "==> Waiting for Calico networking to become ready..."
kubectl rollout status daemonset/calico-node -n calico-system --timeout=300s
kubectl rollout status deployment/calico-kube-controllers -n calico-system --timeout=300s

echo "==> K3s + Calico Bootstrap Complete!"
