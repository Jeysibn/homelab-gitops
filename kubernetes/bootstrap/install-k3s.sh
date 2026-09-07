#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

K3S_VERSION="v1.36.3+k3s1"
CALICO_VERSION="v3.32.1"
CLUSTER_CIDR="10.42.0.0/16"
SERVICE_CIDR="10.43.0.0/16"
CLUSTER_DNS_IP="10.43.0.10"

CALICO_CONFIG="$SCRIPT_DIR/calico-installation.yaml"
VERIFY_SCRIPT="$SCRIPT_DIR/verify-cluster-network.sh"

if (( EUID == 0 )); then
  echo "ERROR: Run this script as your normal user, not with sudo." >&2
  echo "Use: bash $SCRIPT_DIR/install-k3s.sh" >&2
  exit 1
fi

for cmd in curl sudo python3; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "ERROR: Required command not found: $cmd" >&2
    exit 1
  }
done

for file in "$CALICO_CONFIG" "$VERIFY_SCRIPT"; do
  [[ -f "$file" ]] || {
    echo "ERROR: Required bootstrap file not found: $file" >&2
    exit 1
  }
done

CALICO_CIDR="$(awk '$1 == "cidr:" {print $2; exit}' "$CALICO_CONFIG")"
[[ "$CALICO_CIDR" == "$CLUSTER_CIDR" ]] || {
  echo "ERROR: Calico CIDR $CALICO_CIDR does not match K3s CIDR $CLUSTER_CIDR." >&2
  exit 1
}

if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
  echo "ERROR: UFW is active. Disable or configure it before installing K3s." >&2
  exit 1
fi

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
  echo "ERROR: firewalld is active. Disable or configure it before installing K3s." >&2
  exit 1
fi

if [[ -n "${K3S_RESOLV_CONF:-}" ]]; then
  RESOLV_CONF="$K3S_RESOLV_CONF"
elif [[ -r /run/systemd/resolve/resolv.conf ]]; then
  RESOLV_CONF="/run/systemd/resolve/resolv.conf"
else
  RESOLV_CONF="/etc/resolv.conf"
fi

[[ -r "$RESOLV_CONF" ]] || {
  echo "ERROR: Resolver file is not readable: $RESOLV_CONF" >&2
  exit 1
}

if grep -Eq '^nameserver[[:space:]]+127\.(0\.0\.1|0\.0\.53)([[:space:]]|$)' "$RESOLV_CONF"; then
  echo "ERROR: $RESOLV_CONF uses a loopback DNS stub." >&2
  echo "Set K3S_RESOLV_CONF to a resolver file containing the real DNS servers." >&2
  exit 1
fi

echo "==> Bootstrap directory: $SCRIPT_DIR"
echo "==> Pod CIDR:          $CLUSTER_CIDR"
echo "==> Service CIDR:      $SERVICE_CIDR"
echo "==> Cluster DNS:       $CLUSTER_DNS_IP"
echo "==> Upstream resolver: $RESOLV_CONF"

echo "==> Preparing host networking..."
sudo modprobe br_netfilter
cat <<'EOF' | sudo tee /etc/sysctl.d/99-k3s-calico-network.conf >/dev/null
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
EOF
sudo sysctl -q -w net.ipv4.ip_forward=1
sudo sysctl -q -w net.bridge.bridge-nf-call-iptables=1
sudo sysctl -q -w net.bridge.bridge-nf-call-ip6tables=1

K3S_ALREADY_RUNNING=false
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet k3s; then
  K3S_ALREADY_RUNNING=true
fi

if [[ "$K3S_ALREADY_RUNNING" == true ]]; then
  echo "==> Existing K3s service detected; validating instead of reinstalling..."
  K3S_UNIT="$(systemctl cat k3s 2>/dev/null || true)"
  for expected_arg in \
    "--cluster-cidr=$CLUSTER_CIDR" \
    "--service-cidr=$SERVICE_CIDR" \
    "--cluster-dns=$CLUSTER_DNS_IP" \
    "--flannel-backend=none" \
    "--disable-network-policy"; do
    grep -Fq -- "$expected_arg" <<<"$K3S_UNIT" || {
      echo "ERROR: Existing K3s service is missing expected argument: $expected_arg" >&2
      echo "Refusing to mutate an existing cluster from the bootstrap script." >&2
      exit 1
    }
  done
else
  echo "==> Installing K3s ${K3S_VERSION}..."
  curl -sfL https://get.k3s.io | sudo env INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server \
    --cluster-cidr="$CLUSTER_CIDR" \
    --service-cidr="$SERVICE_CIDR" \
    --cluster-dns="$CLUSTER_DNS_IP" \
    --resolv-conf="$RESOLV_CONF" \
    --flannel-backend=none \
    --disable-network-policy \
    --disable=servicelb \
    --disable=traefik \
    --disable=local-storage
fi

command -v kubectl >/dev/null 2>&1 || {
  echo "ERROR: K3s is present but kubectl is not available in PATH." >&2
  exit 1
}

export KUBECONFIG="$HOME/.kube/config"
mkdir -p "$HOME/.kube"
sudo install -m 600 -o "$(id -u)" -g "$(id -g)" \
  /etc/rancher/k3s/k3s.yaml "$KUBECONFIG"

echo "==> Waiting for Kubernetes API..."
for attempt in {1..60}; do
  kubectl get --raw=/readyz >/dev/null 2>&1 && break
  if [[ "$attempt" -eq 60 ]]; then
    echo "ERROR: Kubernetes API did not become ready." >&2
    exit 1
  fi
  sleep 2
done

if kubectl get deployment/tigera-operator -n tigera-operator >/dev/null 2>&1; then
  echo "==> Existing Tigera operator detected; skipping operator re-apply."
else
  echo "==> Installing Calico ${CALICO_VERSION} operator..."
  kubectl apply --server-side --force-conflicts \
    -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"
fi

# The Tigera operator creates its CRDs asynchronously when it starts.
kubectl rollout status deployment/tigera-operator \
  -n tigera-operator --timeout=300s

kubectl wait --for=create \
  crd/installations.operator.tigera.io \
  crd/apiservers.operator.tigera.io \
  --timeout=180s

kubectl wait --for=condition=Established \
  crd/installations.operator.tigera.io \
  crd/apiservers.operator.tigera.io \
  --timeout=180s

if kubectl get installation.operator.tigera.io/default >/dev/null 2>&1; then
  EXISTING_CALICO_CIDR="$(kubectl get installation.operator.tigera.io/default \
    -o jsonpath='{.spec.calicoNetwork.ipPools[0].cidr}')"
  [[ "$EXISTING_CALICO_CIDR" == "$CLUSTER_CIDR" ]] || {
    echo "ERROR: Existing Calico CIDR $EXISTING_CALICO_CIDR does not match $CLUSTER_CIDR." >&2
    echo "Refusing to re-render an existing Calico installation from bootstrap." >&2
    exit 1
  }
  echo "==> Existing Calico Installation is compatible; skipping re-apply."
else
  echo "==> Creating Calico Installation..."
  kubectl apply -f "$CALICO_CONFIG"
fi

if ! kubectl get apiserver.operator.tigera.io/default >/dev/null 2>&1; then
  echo "==> Creating Calico API server resource..."
  cat <<'EOF' | kubectl apply -f -
apiVersion: operator.tigera.io/v1
kind: APIServer
metadata:
  name: default
spec: {}
EOF
fi

kubectl wait --for=create daemonset/calico-node \
  -n calico-system --timeout=300s
kubectl wait --for=create deployment/calico-kube-controllers \
  -n calico-system --timeout=300s

kubectl rollout status daemonset/calico-node \
  -n calico-system --timeout=300s
kubectl rollout status deployment/calico-kube-controllers \
  -n calico-system --timeout=300s
kubectl wait --for=condition=Ready node --all --timeout=300s

kubectl rollout status deployment/coredns \
  -n kube-system --timeout=180s

echo "==> Verifying cluster networking..."
CLUSTER_CIDR="$CLUSTER_CIDR" \
SERVICE_CIDR="$SERVICE_CIDR" \
CLUSTER_DNS_IP="$CLUSTER_DNS_IP" \
  bash "$VERIFY_SCRIPT"

echo "==> K3s + Calico bootstrap complete"
