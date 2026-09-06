#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

K3S_VERSION="v1.36.3+k3s1"
CALICO_VERSION="v3.32.1"
TIGERA_OPERATOR_VERSION="v1.42.3"
CLUSTER_CIDR="10.42.0.0/16"
SERVICE_CIDR="10.43.0.0/16"
CLUSTER_DNS_IP="10.43.0.10"
EXPECTED_OPERATOR_IMAGE="quay.io/tigera/operator:${TIGERA_OPERATOR_VERSION}"
CALICO_INSTALLATION_FILE="$REPO_ROOT/kubernetes/bootstrap/calico-installation.yaml"
RECOVERY_SCRIPT="$REPO_ROOT/kubernetes/bootstrap/recover-calico-ipam.sh"
NETWORK_VERIFY_SCRIPT="$REPO_ROOT/kubernetes/bootstrap/verify-cluster-network.sh"

wait_for_resource() {
  local resource="$1"
  local namespace="${2:-}"
  local timeout_seconds="${3:-180}"
  local deadline=$((SECONDS + timeout_seconds))

  while (( SECONDS < deadline )); do
    if [[ -n "$namespace" ]]; then
      kubectl get "$resource" -n "$namespace" >/dev/null 2>&1 && return 0
    else
      kubectl get "$resource" >/dev/null 2>&1 && return 0
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
    if spec.get("hostNetwork") or status.get("phase") in ("Succeeded", "Failed"):
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
        print("\t".join([meta.get("namespace", "default"), meta.get("name", ""), ip, spec.get("nodeName", "")]))
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

choose_resolv_conf() {
  if [[ -n "${K3S_RESOLV_CONF:-}" ]]; then
    printf '%s\n' "$K3S_RESOLV_CONF"
    return
  fi

  if [[ -r /run/systemd/resolve/resolv.conf ]] && \
     grep -Eq '^nameserver[[:space:]]+[^[:space:]]+' /run/systemd/resolve/resolv.conf; then
    printf '%s\n' /run/systemd/resolve/resolv.conf
  else
    printf '%s\n' /etc/resolv.conf
  fi
}

for cmd in curl kubectl python3 sudo; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  }
done

[[ -f "$NETWORK_VERIFY_SCRIPT" ]] || {
  echo "ERROR: missing network acceptance script: $NETWORK_VERIFY_SCRIPT" >&2
  exit 1
}

CONFIGURED_CALICO_CIDR="$(awk '$1 == "cidr:" {print $2; exit}' "$CALICO_INSTALLATION_FILE")"
if [[ "$CONFIGURED_CALICO_CIDR" != "$CLUSTER_CIDR" ]]; then
  echo "ERROR: K3s pod CIDR ${CLUSTER_CIDR} does not match Calico ${CONFIGURED_CALICO_CIDR:-<missing>}." >&2
  exit 1
fi

python3 - "$CLUSTER_CIDR" "$SERVICE_CIDR" "$CLUSTER_DNS_IP" <<'PY'
import ipaddress
import sys
pod = ipaddress.ip_network(sys.argv[1])
svc = ipaddress.ip_network(sys.argv[2])
dns = ipaddress.ip_address(sys.argv[3])
if pod.overlaps(svc):
    raise SystemExit(f"pod CIDR {pod} overlaps service CIDR {svc}")
if dns not in svc:
    raise SystemExit(f"cluster DNS {dns} is outside service CIDR {svc}")
PY

if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
  echo "ERROR: UFW is active. A host firewall can silently break K3s/Calico pod and Service traffic." >&2
  echo "Disable it for this homelab node or explicitly configure all K3s/Calico rules before bootstrap." >&2
  exit 1
fi
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
  echo "ERROR: firewalld is active. Configure or disable it before K3s bootstrap." >&2
  exit 1
fi

RESOLV_CONF="$(choose_resolv_conf)"
[[ -r "$RESOLV_CONF" ]] || {
  echo "ERROR: resolver file is not readable: $RESOLV_CONF" >&2
  exit 1
}
if grep -Eq '^nameserver[[:space:]]+127\.(0\.0\.1|0\.0\.53)([[:space:]]|$)' "$RESOLV_CONF"; then
  echo "ERROR: resolver file $RESOLV_CONF points at a loopback DNS stub." >&2
  echo "Set K3S_RESOLV_CONF to a resolver file containing the real LAN/upstream DNS servers." >&2
  exit 1
fi

echo "==> Preparing host networking prerequisites..."
sudo modprobe br_netfilter
cat <<'EOF' | sudo tee /etc/sysctl.d/99-k3s-calico-network.conf >/dev/null
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
EOF
sudo sysctl --system >/dev/null

echo "==> Repository network invariants verified"
echo "    Pod CIDR:      ${CLUSTER_CIDR}"
echo "    Service CIDR:  ${SERVICE_CIDR}"
echo "    Cluster DNS:   ${CLUSTER_DNS_IP}"
echo "    Upstream DNS:  ${RESOLV_CONF}"

echo "==> Installing K3s ${K3S_VERSION} with explicit pod/service/DNS networking..."
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" sh -s - server \
  --cluster-cidr="${CLUSTER_CIDR}" \
  --service-cidr="${SERVICE_CIDR}" \
  --cluster-dns="${CLUSTER_DNS_IP}" \
  --resolv-conf="${RESOLV_CONF}" \
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
  kubectl version --request-timeout=5s >/dev/null 2>&1 && break
  if [[ "$attempt" -eq 60 ]]; then
    echo "ERROR: Kubernetes API did not become reachable in time." >&2
    exit 1
  fi
  sleep 2
done

# K3s already ships a CoreDNS Corefile with `forward . /etc/resolv.conf`.
# Do not inject a second `forward .` through coredns-custom *.override files.
# Delete the legacy repo-created ConfigMap on reruns so the bootstrap contract
# is deterministic and CoreDNS uses only the K3s-managed Corefile.
echo "==> Enforcing stock K3s CoreDNS configuration..."
kubectl delete configmap coredns-custom -n kube-system --ignore-not-found >/dev/null

CURRENT_OPERATOR_IMAGE="$(kubectl -n tigera-operator get deployment tigera-operator \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="tigera-operator")].image}' 2>/dev/null || true)"
if [[ -n "$CURRENT_OPERATOR_IMAGE" ]]; then
  echo "==> Existing Tigera operator detected: ${CURRENT_OPERATOR_IMAGE}"
fi

echo "==> Deploying Calico ${CALICO_VERSION} operator (${TIGERA_OPERATOR_VERSION})..."
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"

if ! wait_for_resource deployment/tigera-operator tigera-operator 60; then
  show_tigera_diagnostics
  exit 1
fi

for crd in installations.operator.tigera.io apiservers.operator.tigera.io; do
  if ! wait_for_resource "crd/${crd}" "" 180; then
    show_tigera_diagnostics
    exit 1
  fi
  kubectl wait --for=condition=Established "crd/${crd}" --timeout=180s
done

if ! kubectl rollout status deployment/tigera-operator -n tigera-operator --timeout=300s; then
  show_tigera_diagnostics
  exit 1
fi

ACTUAL_OPERATOR_IMAGE="$(kubectl -n tigera-operator get deployment tigera-operator \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="tigera-operator")].image}')"
if [[ "$ACTUAL_OPERATOR_IMAGE" != "$EXPECTED_OPERATOR_IMAGE" ]]; then
  echo "ERROR: Tigera operator version mismatch. Expected ${EXPECTED_OPERATOR_IMAGE}, got ${ACTUAL_OPERATOR_IMAGE}." >&2
  exit 1
fi

echo "==> Checking for incompatible pre-existing Calico state..."
MISMATCHED_POOLS="$(find_enabled_ippools_outside_cluster_cidr "$CLUSTER_CIDR" || true)"
STALE_BLOCKS="$(find_ipam_blocks_outside_cluster_cidr "$CLUSTER_CIDR" || true)"
if [[ -n "$MISMATCHED_POOLS" || -n "$STALE_BLOCKS" ]]; then
  echo "ERROR: Existing Calico IPAM state is outside ${CLUSTER_CIDR}." >&2
  [[ -z "$MISMATCHED_POOLS" ]] || printf 'IPPools:\nNAME\tCIDR\n%s\n' "$MISMATCHED_POOLS" >&2
  [[ -z "$STALE_BLOCKS" ]] || printf 'IPAMBlocks:\nNAME\tCIDR\n%s\n' "$STALE_BLOCKS" >&2
  echo "Bootstrap refuses to mix incompatible IPAM state. Inspect with:" >&2
  echo "  bash $RECOVERY_SCRIPT --plan" >&2
  exit 1
fi

echo "==> Applying repo-managed Calico installation..."
kubectl apply -f "$CALICO_INSTALLATION_FILE"

for resource in daemonset/calico-node deployment/calico-kube-controllers; do
  if ! wait_for_resource "$resource" calico-system 300; then
    kubectl get pods -n calico-system -o wide >&2 || true
    kubectl get tigerastatus >&2 || true
    exit 1
  fi
done

kubectl rollout status daemonset/calico-node -n calico-system --timeout=300s
kubectl rollout status deployment/calico-kube-controllers -n calico-system --timeout=300s
kubectl wait --for=condition=Ready node --all --timeout=300s

OUTSIDE_PODS="$(find_pods_outside_cluster_cidr "$CLUSTER_CIDR")"
if [[ -n "$OUTSIDE_PODS" ]]; then
  echo "ERROR: active pods have addresses outside ${CLUSTER_CIDR}:" >&2
  printf 'NAMESPACE\tPOD\tIP\tNODE\n%s\n' "$OUTSIDE_PODS" >&2
  exit 1
fi

# Force CoreDNS to restart after Calico is healthy so it receives a valid pod
# address and runs without any legacy custom override mounted.
echo "==> Restarting CoreDNS on the verified Calico network..."
kubectl rollout restart deployment/coredns -n kube-system
kubectl rollout status deployment/coredns -n kube-system --timeout=180s

echo "==> Verifying final Calico IPPool configuration..."
kubectl get ippools.crd.projectcalico.org \
  -o custom-columns='NAME:.metadata.name,CIDR:.spec.cidr,DISABLED:.spec.disabled'

echo "==> Running mandatory cluster network acceptance test..."
CLUSTER_CIDR="$CLUSTER_CIDR" \
SERVICE_CIDR="$SERVICE_CIDR" \
CLUSTER_DNS_IP="$CLUSTER_DNS_IP" \
  bash "$NETWORK_VERIFY_SCRIPT"

echo "==> K3s + Calico Bootstrap Complete!"
echo "    Pod routing, ClusterIP routing, service discovery, and external GitOps DNS all passed."
