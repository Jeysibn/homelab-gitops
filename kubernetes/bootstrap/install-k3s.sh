#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

K3S_VERSION="v1.36.3+k3s1"
CALICO_VERSION="v3.32.1"
TIGERA_OPERATOR_VERSION="v1.42.3"
CLUSTER_CIDR="10.42.0.0/16"
EXPECTED_OPERATOR_IMAGE="quay.io/tigera/operator:${TIGERA_OPERATOR_VERSION}"

wait_for_resource() {
  local resource="$1"
  local namespace="${2:-}"
  local timeout_seconds="${3:-180}"
  local deadline=$((SECONDS + timeout_seconds))

  while (( SECONDS < deadline )); do
    if [[ -n "$namespace" ]]; then
      if kubectl get "$resource" -n "$namespace" >/dev/null 2>&1; then
        return 0
      fi
    elif kubectl get "$resource" >/dev/null 2>&1; then
      return 0
    fi

    sleep 2
  done

  echo "ERROR: Timed out waiting for ${resource} to be created${namespace:+ in namespace ${namespace}}." >&2
  return 1
}

show_tigera_diagnostics() {
  echo "==> Tigera operator diagnostics" >&2
  kubectl get pods -n tigera-operator -o wide >&2 || true
  kubectl describe deployment tigera-operator -n tigera-operator >&2 || true
  kubectl logs -n tigera-operator deployment/tigera-operator --tail=100 >&2 || true
}

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
for attempt in {1..60}; do
  if kubectl version --request-timeout=5s >/dev/null 2>&1; then
    break
  fi

  if [[ "$attempt" -eq 60 ]]; then
    echo "ERROR: Kubernetes API did not become reachable in time." >&2
    exit 1
  fi

  sleep 2
done

echo "==> Configuring CoreDNS upstream resolvers..."
kubectl apply -f "$REPO_ROOT/kubernetes/bootstrap/coredns-custom.yaml"

CURRENT_OPERATOR_IMAGE="$(kubectl -n tigera-operator get deployment tigera-operator \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="tigera-operator")].image}' \
  2>/dev/null || true)"

if [[ -n "$CURRENT_OPERATOR_IMAGE" ]]; then
  echo "==> Existing Tigera operator detected: ${CURRENT_OPERATOR_IMAGE}"
  if [[ "$CURRENT_OPERATOR_IMAGE" != "$EXPECTED_OPERATOR_IMAGE" ]]; then
    echo "==> Upgrading stale Tigera operator to ${EXPECTED_OPERATOR_IMAGE}..."
  fi
fi

echo "==> Deploying Calico ${CALICO_VERSION} operator (${TIGERA_OPERATOR_VERSION})..."
# Calico v3.32 manages its own CRDs from the operator process. Server-side
# apply with conflict takeover also repairs resources previously managed by
# client-side apply and updates stale RBAC/operator resources.
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"

echo "==> Waiting for Tigera operator deployment to be created..."
if ! wait_for_resource deployment/tigera-operator tigera-operator 60; then
  show_tigera_diagnostics
  exit 1
fi

echo "==> Waiting for Calico CRDs to be created by the operator..."
for crd in installations.operator.tigera.io apiservers.operator.tigera.io; do
  if ! wait_for_resource "crd/${crd}" "" 180; then
    show_tigera_diagnostics
    exit 1
  fi
  kubectl wait --for=condition=Established "crd/${crd}" --timeout=180s
done

echo "==> Waiting for Tigera operator rollout..."
if ! kubectl rollout status deployment/tigera-operator -n tigera-operator --timeout=300s; then
  show_tigera_diagnostics
  exit 1
fi

ACTUAL_OPERATOR_IMAGE="$(kubectl -n tigera-operator get deployment tigera-operator \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="tigera-operator")].image}')"

if [[ "$ACTUAL_OPERATOR_IMAGE" != "$EXPECTED_OPERATOR_IMAGE" ]]; then
  echo "ERROR: Tigera operator version mismatch." >&2
  echo "       Expected: ${EXPECTED_OPERATOR_IMAGE}" >&2
  echo "       Actual:   ${ACTUAL_OPERATOR_IMAGE}" >&2
  exit 1
fi

echo "==> Tigera operator is running the expected image: ${ACTUAL_OPERATOR_IMAGE}"

echo "==> Applying repo-managed Calico installation configuration..."
kubectl apply -f "$REPO_ROOT/kubernetes/bootstrap/calico-installation.yaml"

echo "==> Waiting for Calico workloads to be created..."
for resource in daemonset/calico-node deployment/calico-kube-controllers; do
  if ! wait_for_resource "$resource" calico-system 300; then
    echo "==> Calico diagnostics" >&2
    kubectl get pods -n calico-system -o wide >&2 || true
    kubectl get tigerastatus >&2 || true
    exit 1
  fi
done

echo "==> Waiting for Calico networking to become ready..."
kubectl rollout status daemonset/calico-node -n calico-system --timeout=300s
kubectl rollout status deployment/calico-kube-controllers -n calico-system --timeout=300s

echo "==> Waiting for the K3s node to report Ready..."
kubectl wait --for=condition=Ready node --all --timeout=300s

echo "==> K3s + Calico Bootstrap Complete!"
