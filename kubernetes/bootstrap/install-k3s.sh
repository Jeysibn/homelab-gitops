#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

K3S_VERSION="v1.36.3+k3s1"
CALICO_VERSION="v3.32.1"
TIGERA_OPERATOR_VERSION="v1.42.3"
CLUSTER_CIDR="10.42.0.0/16"
EXPECTED_OPERATOR_IMAGE="quay.io/tigera/operator:${TIGERA_OPERATOR_VERSION}"
CALICO_INSTALLATION_FILE="$REPO_ROOT/kubernetes/bootstrap/calico-installation.yaml"
RECOVERY_SCRIPT="$REPO_ROOT/kubernetes/bootstrap/recover-calico-ipam.sh"

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

find_pods_outside_cluster_cidr() {
  local cidr="$1"

  kubectl get pods -A -o json | python3 -c '
import ipaddress, json, sys
net = ipaddress.ip_network(sys.argv[1])
data = json.load(sys.stdin)
for pod in data.get("items", []):
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    if spec.get("hostNetwork"):
        continue
    if status.get("phase") in ("Succeeded", "Failed"):
        continue
    ip = status.get("podIP")
    if not ip:
        continue
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        continue
    if addr.version == net.version and addr not in net:
        meta = pod.get("metadata", {})
        print("\t".join([
            meta.get("namespace", "default"),
            meta.get("name", ""),
            ip,
            spec.get("nodeName", "")
        ]))
' "$cidr"
}

find_enabled_ippools_outside_cluster_cidr() {
  local cidr="$1"

  kubectl get ippools.crd.projectcalico.org -o json 2>/dev/null | python3 -c '
import ipaddress, json, sys
expected = ipaddress.ip_network(sys.argv[1])
data = json.load(sys.stdin)
for item in data.get("items", []):
    spec = item.get("spec", {})
    if spec.get("disabled", False):
        continue
    raw = spec.get("cidr")
    if not raw:
        continue
    try:
        pool = ipaddress.ip_network(raw)
    except ValueError:
        continue
    if pool.version == expected.version and not pool.subnet_of(expected):
        print("\t".join([item.get("metadata", {}).get("name", ""), raw]))
' "$cidr"
}

find_ipam_blocks_outside_cluster_cidr() {
  local cidr="$1"

  kubectl get ipamblocks.crd.projectcalico.org -o json 2>/dev/null | python3 -c '
import ipaddress, json, sys
expected = ipaddress.ip_network(sys.argv[1])
data = json.load(sys.stdin)
for item in data.get("items", []):
    raw = item.get("spec", {}).get("cidr")
    if not raw:
        continue
    try:
        block = ipaddress.ip_network(raw)
    except ValueError:
        continue
    if block.version == expected.version and not block.subnet_of(expected):
        print("\t".join([item.get("metadata", {}).get("name", ""), raw]))
' "$cidr"
}

command -v python3 >/dev/null 2>&1 || {
  echo "ERROR: python3 is required for bootstrap network validation." >&2
  exit 1
}

CONFIGURED_CALICO_CIDR="$(awk '$1 == "cidr:" {print $2; exit}' "$CALICO_INSTALLATION_FILE")"
if [[ -z "$CONFIGURED_CALICO_CIDR" || "$CONFIGURED_CALICO_CIDR" != "$CLUSTER_CIDR" ]]; then
  echo "ERROR: K3s and Calico pod CIDRs do not match in the repository." >&2
  echo "       K3s CLUSTER_CIDR: ${CLUSTER_CIDR}" >&2
  echo "       Calico IPPool:    ${CONFIGURED_CALICO_CIDR:-<missing>}" >&2
  echo "Refusing to install a cluster with inconsistent pod networking." >&2
  exit 1
fi

echo "==> Repository network invariant verified: K3s and Calico both use ${CLUSTER_CIDR}"

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
# Bootstrap is the sole owner of Calico. Argo CD intentionally does not manage
# the Tigera operator or Installation because Argo itself depends on a working
# CNI. Calico v3.32 manages its CRDs from the operator process.
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

echo "==> Checking for pre-existing Calico state outside ${CLUSTER_CIDR}..."
MISMATCHED_POOLS="$(find_enabled_ippools_outside_cluster_cidr "$CLUSTER_CIDR" || true)"
STALE_BLOCKS="$(find_ipam_blocks_outside_cluster_cidr "$CLUSTER_CIDR" || true)"
if [[ -n "$MISMATCHED_POOLS" || -n "$STALE_BLOCKS" ]]; then
  echo "ERROR: Existing Calico state is incompatible with the configured K3s pod CIDR." >&2
  if [[ -n "$MISMATCHED_POOLS" ]]; then
    echo "Enabled IPPools outside ${CLUSTER_CIDR}:" >&2
    printf 'NAME\tCIDR\n%s\n' "$MISMATCHED_POOLS" >&2
  fi
  if [[ -n "$STALE_BLOCKS" ]]; then
    echo "IPAM blocks outside ${CLUSTER_CIDR}:" >&2
    printf 'NAME\tCIDR\n%s\n' "$STALE_BLOCKS" >&2
  fi
  echo >&2
  echo "Bootstrap will not mutate or mix incompatible IPAM state." >&2
  echo "Inspect it first with:" >&2
  echo "  bash $RECOVERY_SCRIPT --plan" >&2
  exit 1
fi

echo "==> Applying repo-managed Calico installation configuration..."
kubectl apply -f "$CALICO_INSTALLATION_FILE"

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

echo "==> Verifying active pod addresses are inside ${CLUSTER_CIDR}..."
OUTSIDE_PODS="$(find_pods_outside_cluster_cidr "$CLUSTER_CIDR")"
if [[ -n "$OUTSIDE_PODS" ]]; then
  echo "ERROR: Active pods still have addresses outside ${CLUSTER_CIDR}." >&2
  echo "       This usually means stale Calico IPAM state survived a CIDR migration." >&2
  printf 'NAMESPACE\tPOD\tIP\tNODE\n%s\n' "$OUTSIDE_PODS" >&2
  echo >&2
  echo "Run the recovery tool in plan mode first:" >&2
  echo "  bash $RECOVERY_SCRIPT --plan" >&2
  exit 1
fi

echo "==> Verifying final Calico IPPool configuration..."
kubectl get ippools.crd.projectcalico.org \
  -o custom-columns='NAME:.metadata.name,CIDR:.spec.cidr,DISABLED:.spec.disabled'

echo "==> K3s + Calico Bootstrap Complete!"
