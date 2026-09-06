#!/usr/bin/env bash
set -euo pipefail

CALICO_VERSION="${CALICO_VERSION:-v3.32.1}"
EXPECTED_CIDR="${EXPECTED_CIDR:-10.42.0.0/16}"
STALE_BLOCK="${STALE_BLOCK:-192.168.243.192/26}"
MODE="${1:---plan}"
TAINT_KEY="homelab.local/ipam-recovery"
CALICOCTL="${CALICOCTL:-/tmp/calicoctl-${CALICO_VERSION#v}}"
LOCKED=false
TAINTED_NODES=()

usage() {
  cat <<'EOF'
Usage:
  bash ./recover-calico-ipam.sh --plan
  bash ./recover-calico-ipam.sh --apply

Environment overrides:
  STALE_BLOCK=192.168.243.192/26
  EXPECTED_CIDR=10.42.0.0/16
  CALICO_VERSION=v3.32.1

--plan is read-only.
--apply causes a temporary scheduling outage on affected nodes while stale
Calico allocations are removed and affected pods are recreated.
EOF
}

if [[ "$MODE" != "--plan" && "$MODE" != "--apply" ]]; then
  usage
  exit 2
fi

for cmd in kubectl curl python3; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  }
done

if [[ -z "${KUBECONFIG:-}" ]]; then
  if [[ -f "$HOME/.kube/config" ]]; then
    export KUBECONFIG="$HOME/.kube/config"
  elif [[ -r /etc/rancher/k3s/k3s.yaml ]]; then
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  else
    echo "ERROR: KUBECONFIG is not set and no K3s kubeconfig was found." >&2
    exit 1
  fi
fi

pods_in_cidr() {
  local cidr="$1"
  kubectl get pods -A -o json | python3 -c '
import ipaddress, json, sys
net = ipaddress.ip_network(sys.argv[1])
data = json.load(sys.stdin)
for pod in data.get("items", []):
    ip = pod.get("status", {}).get("podIP")
    if not ip:
        continue
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        continue
    if addr in net:
        meta = pod.get("metadata", {})
        spec = pod.get("spec", {})
        print("\t".join([
            meta.get("namespace", "default"),
            meta.get("name", ""),
            ip,
            spec.get("nodeName", "")
        ]))
' "$cidr"
}

non_hostnetwork_pods_outside_expected_cidr() {
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

block_resource_names() {
  local cidr="$1"
  kubectl get ipamblocks.crd.projectcalico.org -o json 2>/dev/null | python3 -c '
import json, sys
cidr = sys.argv[1]
data = json.load(sys.stdin)
for item in data.get("items", []):
    if item.get("spec", {}).get("cidr") == cidr:
        print(item.get("metadata", {}).get("name", ""))
' "$cidr"
}

block_affinity_names() {
  local cidr="$1"
  kubectl get blockaffinities.crd.projectcalico.org -o json 2>/dev/null | python3 -c '
import json, sys
cidr = sys.argv[1]
data = json.load(sys.stdin)
for item in data.get("items", []):
    if item.get("spec", {}).get("cidr") == cidr:
        print(item.get("metadata", {}).get("name", ""))
' "$cidr"
}

allocated_ips_in_block() {
  local cidr="$1"
  kubectl get ipamblocks.crd.projectcalico.org -o json 2>/dev/null | python3 -c '
import ipaddress, json, sys
wanted = sys.argv[1]
data = json.load(sys.stdin)
for item in data.get("items", []):
    spec = item.get("spec", {})
    if spec.get("cidr") != wanted:
        continue
    net = ipaddress.ip_network(wanted)
    for idx, allocation in enumerate(spec.get("allocations", [])):
        if allocation is not None:
            print(str(net.network_address + idx))
' "$cidr"
}

ensure_calicoctl() {
  if [[ -x "$CALICOCTL" ]]; then
    return 0
  fi

  local arch
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *)
      echo "ERROR: unsupported architecture for calicoctl: $(uname -m)" >&2
      exit 1
      ;;
  esac

  echo "==> Downloading calicoctl ${CALICO_VERSION}..."
  curl -fsSL \
    "https://github.com/projectcalico/calico/releases/download/${CALICO_VERSION}/calicoctl-linux-${arch}" \
    -o "$CALICOCTL"
  chmod +x "$CALICOCTL"
}

calicoctl_cmd() {
  DATASTORE_TYPE=kubernetes KUBECONFIG="$KUBECONFIG" "$CALICOCTL" "$@"
}

cleanup() {
  set +e
  if [[ "$LOCKED" == true ]]; then
    calicoctl_cmd datastore migrate unlock >/dev/null 2>&1 || true
    LOCKED=false
  fi

  for node in "${TAINTED_NODES[@]:-}"; do
    [[ -n "$node" ]] || continue
    kubectl taint node "$node" "${TAINT_KEY}-" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

echo "==> Current Calico pools"
kubectl get ippools.crd.projectcalico.org \
  -o custom-columns='NAME:.metadata.name,CIDR:.spec.cidr,DISABLED:.spec.disabled' || true

echo
echo "==> Stale block target: ${STALE_BLOCK}"
echo "==> Expected pod CIDR: ${EXPECTED_CIDR}"

if ! kubectl get ippools.crd.projectcalico.org -o json | python3 -c '
import json, sys
wanted = sys.argv[1]
data = json.load(sys.stdin)
ok = any(i.get("spec", {}).get("cidr") == wanted and not i.get("spec", {}).get("disabled", False) for i in data.get("items", []))
sys.exit(0 if ok else 1)
' "$EXPECTED_CIDR"; then
  echo "ERROR: no enabled Calico IPPool matches ${EXPECTED_CIDR}. Refusing recovery." >&2
  exit 1
fi

AFFECTED="$(pods_in_cidr "$STALE_BLOCK")"
BLOCKS="$(block_resource_names "$STALE_BLOCK" || true)"
AFFINITIES="$(block_affinity_names "$STALE_BLOCK" || true)"

echo
echo "==> Pods currently using ${STALE_BLOCK}"
if [[ -n "$AFFECTED" ]]; then
  printf 'NAMESPACE\tPOD\tIP\tNODE\n%s\n' "$AFFECTED"
else
  echo "none"
fi

echo
echo "==> Matching Calico IPAMBlock resources"
[[ -n "$BLOCKS" ]] && printf '%s\n' "$BLOCKS" || echo "none"

echo "==> Matching Calico BlockAffinity resources"
[[ -n "$AFFINITIES" ]] && printf '%s\n' "$AFFINITIES" || echo "none"

if [[ "$MODE" == "--plan" ]]; then
  echo
  echo "PLAN ONLY: no changes were made."
  echo "Run with --apply after reviewing the affected pods and stale block above."
  exit 0
fi

if [[ -z "$AFFECTED" && -z "$BLOCKS" && -z "$AFFINITIES" ]]; then
  echo "==> No stale Calico state found for ${STALE_BLOCK}."
  exit 0
fi

ensure_calicoctl

echo "==> Tainting affected node(s) to prevent immediate pod rescheduling..."
mapfile -t TAINTED_NODES < <(printf '%s\n' "$AFFECTED" | awk -F '\t' 'NF >= 4 && $4 != "" {print $4}' | sort -u)
if [[ ${#TAINTED_NODES[@]} -eq 0 ]]; then
  mapfile -t TAINTED_NODES < <(kubectl get nodes -o name | sed 's#node/##')
fi
for node in "${TAINTED_NODES[@]}"; do
  kubectl taint node "$node" "${TAINT_KEY}=true:NoSchedule" --overwrite
done

echo "==> Deleting pods that still use the stale block..."
if [[ -n "$AFFECTED" ]]; then
  while IFS=$'\t' read -r namespace pod _ip _node; do
    [[ -n "$namespace" && -n "$pod" ]] || continue
    kubectl delete pod -n "$namespace" "$pod" --wait=true --timeout=60s
  done <<< "$AFFECTED"
fi

sleep 3
REMAINING="$(pods_in_cidr "$STALE_BLOCK")"
if [[ -n "$REMAINING" ]]; then
  echo "ERROR: pods were recreated inside ${STALE_BLOCK} while recovery taint is active:" >&2
  printf '%s\n' "$REMAINING" >&2
  echo "Refusing to alter IPAM state." >&2
  exit 1
fi

echo "==> Locking Calico datastore while stale allocations are released..."
calicoctl_cmd datastore migrate lock
LOCKED=true

echo "==> Releasing allocated IPs that belong only to ${STALE_BLOCK}..."
mapfile -t STALE_IPS < <(allocated_ips_in_block "$STALE_BLOCK")
for ip in "${STALE_IPS[@]:-}"; do
  [[ -n "$ip" ]] || continue
  echo "    releasing ${ip}"
  calicoctl_cmd ipam release --ip="$ip"
done

# Once all workload allocations are released, an empty block may be removed
# automatically. If it remains, remove only the exact stale CIDR resource.
mapfile -t BLOCK_NAMES < <(block_resource_names "$STALE_BLOCK" || true)
if [[ ${#BLOCK_NAMES[@]} -gt 0 ]]; then
  LEFT="$(allocated_ips_in_block "$STALE_BLOCK" | wc -l | tr -d ' ')"
  if [[ "$LEFT" != "0" ]]; then
    echo "ERROR: ${LEFT} allocations still remain in ${STALE_BLOCK}; refusing block deletion." >&2
    exit 1
  fi

  for name in "${BLOCK_NAMES[@]}"; do
    kubectl delete ipamblocks.crd.projectcalico.org "$name"
  done
fi

mapfile -t AFFINITY_NAMES < <(block_affinity_names "$STALE_BLOCK" || true)
for name in "${AFFINITY_NAMES[@]}"; do
  kubectl delete blockaffinities.crd.projectcalico.org "$name"
done

echo "==> Running Calico IPAM consistency check..."
calicoctl_cmd ipam check --show-problem-ips || true

echo "==> Unlocking Calico datastore..."
calicoctl_cmd datastore migrate unlock
LOCKED=false

echo "==> Removing recovery taint so workloads can reschedule..."
for node in "${TAINTED_NODES[@]}"; do
  kubectl taint node "$node" "${TAINT_KEY}-" || true
done
TAINTED_NODES=()

echo "==> Waiting for CoreDNS to recover..."
if kubectl get deployment/coredns -n kube-system >/dev/null 2>&1; then
  kubectl rollout status deployment/coredns -n kube-system --timeout=180s
fi

echo "==> Waiting for pods to receive addresses from ${EXPECTED_CIDR}..."
for attempt in {1..60}; do
  OUTSIDE="$(non_hostnetwork_pods_outside_expected_cidr "$EXPECTED_CIDR")"
  STALE_NOW="$(pods_in_cidr "$STALE_BLOCK")"
  if [[ -z "$OUTSIDE" && -z "$STALE_NOW" ]]; then
    break
  fi

  if [[ "$attempt" -eq 60 ]]; then
    echo "ERROR: pods still have addresses outside ${EXPECTED_CIDR}:" >&2
    printf '%s\n' "$OUTSIDE" >&2
    exit 1
  fi
  sleep 3
done

echo "==> Verifying in-cluster DNS..."
kubectl delete pod calico-ipam-dns-test -n default --ignore-not-found >/dev/null 2>&1 || true
kubectl run calico-ipam-dns-test -n default \
  --image=busybox:1.36 \
  --restart=Never \
  --command -- sh -c \
  'nslookup kubernetes.default.svc.cluster.local 10.43.0.10 && nslookup github.com 10.43.0.10'

if ! kubectl wait -n default pod/calico-ipam-dns-test \
  --for=jsonpath='{.status.phase}'=Succeeded --timeout=120s; then
  kubectl logs -n default calico-ipam-dns-test || true
  kubectl describe pod -n default calico-ipam-dns-test || true
  exit 1
fi
kubectl logs -n default calico-ipam-dns-test
kubectl delete pod -n default calico-ipam-dns-test --wait=false >/dev/null

echo "==> Refreshing Argo CD after DNS recovery..."
if kubectl get namespace argocd >/dev/null 2>&1; then
  if kubectl get deployment/argocd-repo-server -n argocd >/dev/null 2>&1; then
    kubectl rollout restart deployment/argocd-repo-server -n argocd
    kubectl rollout status deployment/argocd-repo-server -n argocd --timeout=180s
  fi

  for app in root-app monikey; do
    if kubectl get application "$app" -n argocd >/dev/null 2>&1; then
      kubectl annotate application "$app" -n argocd \
        argocd.argoproj.io/refresh=hard --overwrite
    fi
  done
  kubectl get applications -n argocd || true
fi

echo "==> Final pod addresses"
kubectl get pods -A -o wide

echo "==> Calico stale IPAM recovery complete."
