#!/usr/bin/env bash
set -euo pipefail

CLUSTER_CIDR="${CLUSTER_CIDR:-10.42.0.0/16}"
SERVICE_CIDR="${SERVICE_CIDR:-10.43.0.0/16}"
CLUSTER_DNS_IP="${CLUSTER_DNS_IP:-10.43.0.10}"
TEST_NAMESPACE="${TEST_NAMESPACE:-k3s-network-acceptance}"
TEST_IMAGE="${TEST_IMAGE:-busybox:1.36}"

for cmd in kubectl python3; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  }
done

cleanup() {
  kubectl delete namespace "$TEST_NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

ip_in_cidr() {
  python3 - "$1" "$2" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
network = ipaddress.ip_network(sys.argv[2])
raise SystemExit(0 if address in network else 1)
PY
}

show_diagnostics() {
  echo "==> Cluster network diagnostics" >&2
  kubectl get nodes -o wide >&2 || true
  kubectl get pods -A -o wide >&2 || true
  kubectl get service kube-dns -n kube-system -o wide >&2 || true
  kubectl get endpoints kube-dns -n kube-system -o wide >&2 || true
  kubectl get endpointslices -n kube-system -l k8s-app=kube-dns -o wide >&2 || true
  kubectl get ippools.crd.projectcalico.org -o wide >&2 || true
  kubectl get tigerastatus >&2 || true
  kubectl logs -n kube-system deployment/coredns --tail=100 >&2 || true
}

fail() {
  echo "ERROR: $*" >&2
  show_diagnostics
  exit 1
}

echo "==> Verifying CoreDNS deployment and Service contract..."
kubectl rollout status deployment/coredns -n kube-system --timeout=180s || fail "CoreDNS deployment is not ready."

ACTUAL_DNS_SERVICE_IP="$(kubectl get service kube-dns -n kube-system -o jsonpath='{.spec.clusterIP}')"
[[ "$ACTUAL_DNS_SERVICE_IP" == "$CLUSTER_DNS_IP" ]] || \
  fail "kube-dns ClusterIP is ${ACTUAL_DNS_SERVICE_IP:-<missing>}, expected ${CLUSTER_DNS_IP}."

DNS_ENDPOINT_IP="$(kubectl get endpoints kube-dns -n kube-system -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null || true)"
[[ -n "$DNS_ENDPOINT_IP" ]] || fail "kube-dns has no ready endpoint."
ip_in_cidr "$DNS_ENDPOINT_IP" "$CLUSTER_CIDR" || \
  fail "CoreDNS endpoint ${DNS_ENDPOINT_IP} is outside pod CIDR ${CLUSTER_CIDR}."

echo "    CoreDNS Service:  ${CLUSTER_DNS_IP}"
echo "    CoreDNS Endpoint: ${DNS_ENDPOINT_IP}"

kubectl delete namespace "$TEST_NAMESPACE" --ignore-not-found --wait=true >/dev/null 2>&1 || true
kubectl create namespace "$TEST_NAMESPACE" >/dev/null

cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: echo
  namespace: ${TEST_NAMESPACE}
  labels:
    app: k3s-network-echo
spec:
  restartPolicy: Never
  containers:
    - name: echo
      image: ${TEST_IMAGE}
      command:
        - sh
        - -ec
        - |
          mkdir -p /www
          printf 'network-ok\n' > /www/index.html
          exec httpd -f -p 8080 -h /www
      readinessProbe:
        tcpSocket:
          port: 8080
        periodSeconds: 2
        timeoutSeconds: 1
        failureThreshold: 30
---
apiVersion: v1
kind: Service
metadata:
  name: echo
  namespace: ${TEST_NAMESPACE}
spec:
  selector:
    app: k3s-network-echo
  ports:
    - name: http
      port: 8080
      targetPort: 8080
EOF

kubectl wait pod/echo -n "$TEST_NAMESPACE" --for=condition=Ready --timeout=120s || \
  fail "acceptance-test echo pod did not become Ready."

ECHO_POD_IP="$(kubectl get pod echo -n "$TEST_NAMESPACE" -o jsonpath='{.status.podIP}')"
ECHO_SERVICE_IP="$(kubectl get service echo -n "$TEST_NAMESPACE" -o jsonpath='{.spec.clusterIP}')"

[[ -n "$ECHO_POD_IP" ]] || fail "acceptance-test pod has no IP."
[[ -n "$ECHO_SERVICE_IP" ]] || fail "acceptance-test Service has no ClusterIP."
ip_in_cidr "$ECHO_POD_IP" "$CLUSTER_CIDR" || \
  fail "acceptance-test pod IP ${ECHO_POD_IP} is outside ${CLUSTER_CIDR}."
ip_in_cidr "$ECHO_SERVICE_IP" "$SERVICE_CIDR" || \
  fail "acceptance-test Service IP ${ECHO_SERVICE_IP} is outside ${SERVICE_CIDR}."

echo "==> Running pod, Service, and DNS acceptance tests..."
kubectl run network-client -n "$TEST_NAMESPACE" \
  --image="$TEST_IMAGE" \
  --restart=Never \
  --env="ECHO_POD_IP=${ECHO_POD_IP}" \
  --env="ECHO_SERVICE_IP=${ECHO_SERVICE_IP}" \
  --env="DNS_ENDPOINT_IP=${DNS_ENDPOINT_IP}" \
  --env="DNS_SERVICE_IP=${CLUSTER_DNS_IP}" \
  --command -- sh -ec '
    echo "[1/6] pod -> pod IP"
    wget -T 5 -qO- "http://${ECHO_POD_IP}:8080" | grep -q "network-ok"

    echo "[2/6] pod -> ClusterIP"
    wget -T 5 -qO- "http://${ECHO_SERVICE_IP}:8080" | grep -q "network-ok"

    echo "[3/6] pod -> CoreDNS endpoint IP"
    nslookup echo.k3s-network-acceptance.svc.cluster.local "$DNS_ENDPOINT_IP" >/dev/null

    echo "[4/6] pod -> CoreDNS ClusterIP"
    nslookup echo.k3s-network-acceptance.svc.cluster.local "$DNS_SERVICE_IP" >/dev/null

    echo "[5/6] Kubernetes service discovery"
    nslookup kubernetes.default.svc.cluster.local "$DNS_SERVICE_IP" >/dev/null

    echo "[6/6] external DNS required by GitOps"
    nslookup github.com "$DNS_SERVICE_IP" >/dev/null
  '

for attempt in {1..60}; do
  PHASE="$(kubectl get pod network-client -n "$TEST_NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  case "$PHASE" in
    Succeeded)
      break
      ;;
    Failed)
      kubectl logs -n "$TEST_NAMESPACE" network-client >&2 || true
      kubectl describe pod -n "$TEST_NAMESPACE" network-client >&2 || true
      fail "cluster network acceptance test failed. Do not install Argo CD or workloads."
      ;;
  esac
  sleep 2
done

PHASE="$(kubectl get pod network-client -n "$TEST_NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
if [[ "$PHASE" != "Succeeded" ]]; then
  kubectl logs -n "$TEST_NAMESPACE" network-client >&2 || true
  kubectl describe pod -n "$TEST_NAMESPACE" network-client >&2 || true
  fail "cluster network acceptance test timed out. Do not install Argo CD or workloads."
fi

kubectl logs -n "$TEST_NAMESPACE" network-client

echo "==> Cluster network acceptance test passed"
echo "    Pod CIDR routing:       OK"
echo "    Service ClusterIP:      OK"
echo "    CoreDNS endpoint:       OK"
echo "    CoreDNS Service:        OK"
echo "    Kubernetes DNS:         OK"
echo "    External GitOps DNS:    OK"
