# K3s + Calico + Argo CD Bootstrap Incident — 2026-09-07

This document records the complete investigation, fixes, validation steps, and design decisions made while rebuilding the single-node K3s homelab on September 7, 2026.

It is both an incident record and an operational runbook for future fresh deployments.

## Scope and environment

The affected environment was the current single-node homelab:

- Host OS: Ubuntu 22.04.5 LTS
- Kernel: 5.15.0-191-generic
- K3s: `v1.36.3+k3s1`
- Calico: `v3.32.1`
- K3s node: `ubuntu`
- Node IP: `192.168.86.16`
- Pod CIDR: `10.42.0.0/16`
- Service CIDR: `10.43.0.0/16`
- CoreDNS Service IP: `10.43.0.10`
- kube-proxy mode: `iptables`
- Calico dataplane: iptables using the nft-backed iptables frontend
- Calico BPF dataplane: disabled
- Cluster topology: one K3s node

Argo CD is bootstrap-owned and points its root App-of-Apps at the `main` branch under `kubernetes/argocd-apps`.

## Bootstrap design principles

The bootstrap is intentionally prevention-first:

1. K3s and Calico must become healthy before Argo CD is installed.
2. The cluster network acceptance test must pass before GitOps workloads are allowed to start.
3. Existing K3s and Calico installations are validated instead of blindly re-rendered or reinstalled.
4. Recovery logic is kept separate from the normal fresh-install path.
5. Scripts are run as the normal Ubuntu user. They use `sudo` only for host operations that require root.
6. The normal user's kubeconfig is created from `/etc/rancher/k3s/k3s.yaml` so non-root bootstrap stages can access the API.

## Bootstrap reliability fixes added during the investigation

### Calico operator CRD ordering

On a fresh cluster, the Tigera operator Deployment can exist before its operator-managed CRDs are ready.

The K3s bootstrap was updated to:

1. Apply the Tigera operator.
2. Wait for `deployment/tigera-operator` to roll out.
3. Wait for the `installations.operator.tigera.io` and `apiservers.operator.tigera.io` CRDs to be created.
4. Wait for both CRDs to become `Established`.
5. Only then create or validate the Calico `Installation` and `APIServer` resources.

This prevents fresh-cluster races where Calico custom resources are applied before their CRDs exist.

### Network verifier fail-fast behavior

The network acceptance test was changed so a failed test pod is detected immediately instead of waiting for a long timeout.

The required checks are:

```text
[1/6] pod -> pod IP
[2/6] pod -> ClusterIP
[3/6] pod -> CoreDNS endpoint IP
[4/6] pod -> CoreDNS ClusterIP
[5/6] Kubernetes service discovery
[6/6] external DNS required by GitOps
```

Argo CD must not be installed unless all six checks pass.

### Bootstrap idempotency

The K3s bootstrap was updated to avoid unnecessary network dataplane re-renders on reruns.

If K3s is already active, the script validates that the service still contains the required arguments:

```text
--cluster-cidr=10.42.0.0/16
--service-cidr=10.43.0.0/16
--cluster-dns=10.43.0.10
--flannel-backend=none
--disable-network-policy
```

If the existing cluster is incompatible, the bootstrap fails instead of mutating it in place.

For Calico:

- an existing Tigera operator is not reapplied unnecessarily;
- an existing `Installation` is validated instead of blindly re-rendered;
- the existing Calico CIDR must match `10.42.0.0/16`;
- the API server custom resource is created separately if missing.

## Initial network failure

Calico components and CoreDNS appeared healthy, but application traffic still failed.

The most useful acceptance-test breakpoint was:

```text
[1/6] pod -> pod IP                PASS
[2/6] pod -> ClusterIP             PASS
[3/6] pod -> CoreDNS endpoint IP   PASS
[4/6] pod -> CoreDNS ClusterIP     FAIL
```

At the time:

- CoreDNS endpoint: `10.42.243.198`
- CoreDNS Service: `10.43.0.10`
- kube-proxy correctly DNATed both UDP/53 and TCP/53 to `10.42.243.198:53`
- conntrack usage was only about `210 / 131072`
- kube-proxy reported no synchronization errors

This immediately showed that CoreDNS itself was not the main problem: direct DNS to the pod IP worked, but UDP DNS through the Service VIP did not.

## Hypotheses that were investigated and ruled out

### Stale kube-proxy endpoint rules

An earlier CoreDNS pod had used a different IP, so a stale DNAT rule was suspected.

The live kube-proxy rules were inspected and were correct:

```text
10.43.0.10:53/udp -> 10.42.243.198:53
10.43.0.10:53/tcp -> 10.42.243.198:53
```

The EndpointSlice also pointed to `10.42.243.198`.

Therefore the failure was not caused by stale kube-dns Service rules.

### Conntrack exhaustion

The host reported approximately:

```text
net.netfilter.nf_conntrack_count = 210
net.netfilter.nf_conntrack_max   = 131072
```

Conntrack exhaustion was ruled out.

### Calico stale Felix state

A known upstream Calico class of issues can sometimes be healed by restarting `calico-node`.

The DaemonSet was restarted, but the CoreDNS Service UDP failure remained.

Therefore stale Felix process state was not sufficient to explain the failure.

### Host iptables backend mismatch

The host used:

```text
iptables v1.8.7 (nf_tables)
```

Felix auto-detected the same nft-backed iptables commands:

```text
backendMode="nft"
```

The Calico BPF dataplane was disabled, and no active Calico rules were present in the legacy iptables backend.

Therefore this was not a legacy-vs-nft split between kube-proxy and Calico.

### CoreDNS upstream resolver configuration

The host itself could resolve through `1.1.1.1`, and the node could query the CoreDNS pod IP directly.

This proved the immediate stage-4 failure was not caused by `/etc/resolv.conf`, CoreDNS RBAC, missing endpoints, or CoreDNS process health.

## Protocol-specific evidence

Further testing showed:

- node -> `10.43.0.10:53/UDP`: failed
- pod -> `10.43.0.10:53/TCP`: succeeded
- pod -> `1.1.1.1:53/UDP`: failed
- node -> `1.1.1.1:53/UDP`: succeeded
- node -> CoreDNS pod IP: succeeded

This narrowed the problem to UDP tracking/NAT behavior on the host rather than DNS configuration itself.

## Confirmed root cause: blanket Calico UDP NOTRACK rule

The live Calico raw table contained:

```text
-A cali-PREROUTING -p udp -j NOTRACK
```

The nftables view showed the same behavior:

```text
meta l4proto udp ... notrack
```

The rule had no destination-port restriction and therefore matched all UDP traffic.

This was not the intended Calico VXLAN behavior. Calico v3.32.1's rule renderer intends VXLAN NOTRACK to match only UDP destination port 4789.

The live cluster had instead programmed a blanket UDP NOTRACK rule.

Because the raw table is processed before the normal Kubernetes Service NAT path, UDP packets were marked untracked before kube-proxy could use normal conntrack-backed Service NAT.

The failure path was:

```text
UDP packet
  -> Calico raw PREROUTING
  -> blanket UDP NOTRACK
  -> packet bypasses normal conntrack handling
  -> kube-proxy UDP Service NAT cannot operate normally
  -> UDP Service traffic fails
```

This precisely explained why:

- direct CoreDNS pod-IP traffic worked;
- TCP through the kube-dns Service worked;
- UDP through the kube-dns Service failed;
- pod external UDP DNS failed.

## Required single-node Calico configuration

The current homelab is a one-node K3s cluster. Overlay encapsulation provides no benefit in this topology.

For this repository's current single-node deployment, Calico must use:

```yaml
spec:
  calicoNetwork:
    containerIPForwarding: Enabled
    ipPools:
      - name: default-ipv4-ippool
        blockSize: 26
        cidr: 10.42.0.0/16
        encapsulation: None
        natOutgoing: Enabled
        nodeSelector: all()
```

The key requirement is:

```yaml
encapsulation: None
```

`containerIPForwarding: Enabled` is also retained so the generated Calico CNI configuration enables container IP forwarding as expected for this K3s/Calico deployment.

For a future multi-node deployment, re-evaluate encapsulation based on the node/subnet topology. Do not blindly copy the single-node assumption to a topology that actually requires an overlay.

## Live remediation performed

The running Calico `Installation` was patched to disable encapsulation.

After operator reconciliation, the active IPPool reported:

```text
ipipMode: Never
natOutgoing: true
vxlanMode: Never
```

The `calico-node` DaemonSet was then restarted so Felix rebuilt its dataplane rules.

After the restart, the blanket UDP `NOTRACK` rule disappeared from `cali-PREROUTING`.

The network acceptance test then passed all six checks:

```text
==> Cluster network acceptance test passed
    Pod CIDR routing:       OK
    Service ClusterIP:      OK
    CoreDNS endpoint:       OK
    CoreDNS Service:        OK
    Kubernetes DNS:         OK
    External GitOps DNS:    OK
```

This confirmed both the root cause and the durable single-node fix.

## Kubeconfig issue before Argo CD

After networking was fixed, `install-argocd.sh` was first run with `sudo` and correctly refused:

```text
ERROR: Run this script as your normal user, not with sudo.
```

Running it as the normal user then failed with:

```text
ERROR: Kubernetes API is not reachable with the current kubeconfig.
```

The cluster itself was healthy because `sudo kubectl` access worked. The problem was the normal user's kubeconfig.

The correct repair was to refresh the user kubeconfig from K3s:

```bash
mkdir -p "$HOME/.kube"

sudo install \
  -m 600 \
  -o "$(id -u)" \
  -g "$(id -g)" \
  /etc/rancher/k3s/k3s.yaml \
  "$HOME/.kube/config"

export KUBECONFIG="$HOME/.kube/config"
```

The design decision is to keep the bootstrap scripts non-root. Do not solve this by running the whole Argo CD installer with `sudo`.

## Argo CD installation

Argo CD `v3.5.1` was installed successfully after the normal user's kubeconfig was repaired.

The bootstrap sequence was:

1. Create namespace `argocd`.
2. Server-side apply the Argo CD install manifest.
3. Wait for the `Application` CRD to become established.
4. Apply `root-app.yaml`.
5. Patch `argocd-cmd-params-cm` so repo-server gRPC TXT service config resolution is disabled.
6. Restart `argocd-repo-server`.
7. Wait for all Argo CD Deployments and the application-controller StatefulSet.

The root application is intentionally configured as:

```text
Repo:     https://github.com/jeysibn/homelab-gitops.git
Revision: main
Path:     kubernetes/argocd-apps
```

## Argo CD readiness-selector bug

The original final readiness check used a pod selector equivalent to:

```text
app.kubernetes.io/part-of=argocd
```

On the Argo CD v3.5.1 manifest used here, that selector could match no pods even though all Argo workloads were already `1/1 Running`.

The script therefore ended with:

```text
error: no matching resources found
```

This was a false bootstrap failure, not an Argo CD installation failure.

The script was corrected to wait directly for the known Deployments:

- `argocd-applicationset-controller`
- `argocd-dex-server`
- `argocd-notifications-controller`
- `argocd-redis`
- `argocd-repo-server`
- `argocd-server`

and for:

- `statefulset/argocd-application-controller`

This makes the final readiness gate explicit and version-stable for the pinned manifest.

## Temporary root-app `Unknown` state

Immediately after Argo CD came up, `root-app` showed:

```text
SYNC STATUS:   Unknown
HEALTH STATUS: Healthy
```

The Application condition was:

```text
ComparisonError: Failed to load target state: failed to generate manifest for source 1 of 1: rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing: dial tcp 10.43.91.219:8081: connect: connection refused"
```

The application-controller logs also briefly showed Redis connection failures such as:

```text
dial tcp 10.43.119.207:6379: connect: connection refused
```

and later an `i/o timeout` while Redis was still converging.

The timing showed this was startup sequencing:

- application-controller began reconciling `root-app`;
- repo-server was still starting and had not yet begun listening on port 8081;
- Redis was also still becoming reachable;
- repo-server subsequently logged that it was listening on `[::]:8081` and its gRPC health checks returned `OK`.

This was not evidence of the earlier Calico UDP failure returning.

## Root-app recovery and GitOps convergence

Once Argo CD components were healthy, a hard refresh was requested:

```bash
sudo kubectl annotate application root-app \
  -n argocd \
  argocd.argoproj.io/refresh=hard \
  --overwrite
```

The state then progressed from:

```text
root-app   Unknown   Healthy
```

to:

```text
root-app   OutOfSync   Healthy
```

This was a good sign: Argo CD could now read the Git repository and compare the desired state.

The App-of-Apps then created child Applications including:

- Alloy
- cert-manager
- cluster-issuer
- Grafana
- Loki
- Longhorn
- MetalLB
- MetalLB config
- MoniKey
- Pi-hole DNS
- Prometheus
- Traefik
- Unbound DNS

As reconciliation continued, `root-app` reached:

```text
root-app   Synced   Healthy
```

Several child Applications also reached `Synced/Healthy`, while others were still `Progressing`, `OutOfSync`, or temporarily `Unknown` during their own startup.

This is expected App-of-Apps convergence behavior. `root-app Synced/Healthy` means the root registry is successfully read and applied; each child Application should then be evaluated independently until the whole platform converges.

## Verification commands

### Verify K3s and Calico network state

```bash
sudo kubectl get nodes
sudo kubectl get pods -A -o wide
sudo kubectl get installation.operator.tigera.io default \
  -o jsonpath='{.spec.calicoNetwork.ipPools[0].encapsulation}{"\n"}'
sudo kubectl get ippool default-ipv4-ippool \
  -o yaml | grep -E 'vxlanMode|ipipMode|natOutgoing'
```

Expected for the current single-node topology:

```text
None
ipipMode: Never
natOutgoing: true
vxlanMode: Never
```

### Verify the bad NOTRACK rule is absent

```bash
sudo iptables-nft -t raw \
  -L cali-PREROUTING \
  -n -v --line-numbers

sudo iptables-nft-save -t raw | \
  grep -Ei 'NOTRACK|cali-PREROUTING'
```

There must not be a blanket rule equivalent to:

```text
-p udp -j NOTRACK
```

### Run the mandatory cluster network acceptance test

```bash
cd ~/homelab-gitops/kubernetes/bootstrap
sudo bash verify-cluster-network.sh
```

All six checks must pass.

### Verify Argo CD

```bash
sudo kubectl get pods -n argocd -o wide
sudo kubectl get applications -n argocd
```

For the root application, the desired final state is:

```text
root-app   Synced   Healthy
```

If the root application is temporarily `Unknown`, inspect:

```bash
sudo kubectl get application root-app -n argocd \
  -o jsonpath='{range .status.conditions[*]}{.type}{": "}{.message}{"\n"}{end}'

sudo kubectl logs -n argocd \
  deployment/argocd-repo-server \
  --tail=100

sudo kubectl logs -n argocd \
  argocd-application-controller-0 \
  --tail=100
```

If all Argo CD components are healthy and the only failure was a startup-time repo-server/Redis connection refusal, trigger a hard refresh and re-check the Application status.

## Preventive guardrails for future fresh deployments

1. **Single-node Calico must keep `encapsulation: None`.**
2. Keep `containerIPForwarding: Enabled` and `natOutgoing: Enabled`.
3. Do not enable VXLAN on this one-node topology without re-running the complete network acceptance suite and inspecting raw-table NOTRACK rules.
4. Do not proceed to Argo CD unless the six network checks pass.
5. Run bootstrap scripts as the normal user, not with `sudo`.
6. If normal-user API access fails, repair `~/.kube/config`; do not weaken the script's root guard.
7. Keep Argo CD pinned and use explicit workload rollout checks instead of broad pod-label assumptions.
8. Treat initial `root-app Unknown` as a condition to inspect, not proof of a broken cluster. Check repo-server and Redis readiness first.
9. `root-app OutOfSync` immediately after recovery is normal and means Git comparison is working.
10. The root application should ultimately reach `Synced/Healthy`; child Applications may converge at different rates.
11. Continue editing through `dev`; Argo CD remains configured to reconcile production state from `main`.

## Git workflow note

All repository fixes from this incident were made on the existing `dev` branch and collected in PR #56 targeting `main`.

Do not merge the PR until explicitly approved and until CI is green.

If a local `dev` branch has unpublished commits and a pull from `origin/dev` creates a local merge commit, inspect the local-only commits before pushing so unrelated local history is not accidentally published.
