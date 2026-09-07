# CI/CD & Kubernetes Manifest Troubleshooting Guide

This document details historical configuration issues, root causes, and technical resolutions for the GitOps CI/CD pipeline defined in `.github/workflows/k3s-ci.yml`.

For the full September 7, 2026 K3s/Calico/Argo CD bootstrap investigation, including the complete timeline, ruled-out hypotheses, kubeconfig recovery, Argo CD startup sequencing, and final verification state, see [Bootstrap-Incident-2026-09-07.md](Bootstrap-Incident-2026-09-07.md).

## Summary of Issues & Fixes

| Incident | Severity | Root Cause | Technical Resolution |
| :--- | :--- | :--- | :--- |
| **Missing CRD Schemas** | High | `kubeconform` lacks built-in schemas for Custom Resources (`IPPool`, `ClusterIssuer`, `Application`). | Integrated `datreeio/CRDs-catalog` schema location and added `-ignore-missing-schemas`. |
| **Helm Values Parsing Failure** | High | `kubeconform` attempted to validate Helm `values.yaml` files as raw Kubernetes API manifests. | Separated repo into `kubernetes/helm/` for values and `kubernetes/manifests/` for raw manifests. |
| **Action Resolution Failure** | Medium | `yannh/kubeconform-action` failed to resolve on GitHub Actions runners. | Replaced action with direct binary download (`curl`) in the runner environment. |
| **Traefik Values Schema Error** | High | Unpinned Helm chart versions evaluated against updated upstream schema breaking `redirectTo` and `tls` keys. | Aligned `values.yaml` syntax and pinned chart versions dynamically via Argo CD `targetRevision` specs. |
| **Script Parsing Failures** | High | `jq` crashed on scalar strings and `yq` passed YAML document separators (`---`) as chart names. | Updated rendering step to `yq eval-all -N` with token filtering inside the Bash loop. |
| **Trivy Security Scan Failures** | High | `unbound.yaml` failed security checks (`KSV-0014`, `KSV-0118`) due to unconfigured security contexts. | Added non-root `securityContext`, dropped capabilities, enabled `readOnlyRootFilesystem`, and added `/tmp` `emptyDir`. |
| **Workflow Execution Filtering** | Low | Pipeline did not trigger on custom feature or test branches. | Updated `on.push.branches` filters and added `workflow_dispatch` for manual UI triggers. |
| **Fresh-cluster DNS startup** | High | Pi-hole and Unbound were reconciled together, and DHCP used the string value `"false"`, which Helm can evaluate as enabled. | Start Pi-hole after Unbound, use a boolean `false`, and expose Unbound readiness/liveness checks. |
| **CoreDNS upstream reset** | High | The CoreDNS ConfigMap was edited manually, so a fresh K3s install did not contain the public upstream resolvers. | Apply the supported CoreDNS configuration during bootstrap instead of relying on manual edits. |
| **Single-node Calico UDP Service failure** | Critical | With VXLAN enabled, the live Calico v3.32.1 iptables dataplane installed a blanket UDP `NOTRACK` rule in raw `cali-PREROUTING`, causing UDP Kubernetes Service traffic to bypass normal conntrack/NAT handling. | For this repository's single-node K3s topology, set Calico `encapsulation: None`, keep `containerIPForwarding: Enabled` and `natOutgoing: Enabled`, restart `calico-node`, and require the network acceptance test to pass before Argo CD/workloads are installed. |
| **Normal-user kubeconfig unavailable** | High | `install-argocd.sh` correctly refused root execution, but the normal user's kubeconfig was missing or stale even though root K3s access worked. | Refresh `~/.kube/config` from `/etc/rancher/k3s/k3s.yaml` with user ownership; keep the Argo CD installer non-root. |
| **Argo CD readiness false failure** | Medium | A broad pod label selector matched no Argo CD pods even though every workload was already running. | Wait explicitly for the six Argo CD Deployments and the application-controller StatefulSet. |
| **root-app temporary `Unknown` state** | Medium | The application-controller reconciled before repo-server and Redis had fully become reachable, producing transient `connection refused` errors. | Wait for Argo CD workloads, inspect conditions/logs, then hard-refresh the root Application after the components are healthy. `root-app` subsequently converged to `Synced/Healthy`. |

---

## Single-Node K3s: Required Calico Encapsulation Setting

### Scope

This section applies to the current homelab topology: **one K3s node using Calico as the CNI and kube-proxy in iptables mode**.

For this single-node deployment, Calico overlay encapsulation is **not required** and must remain disabled in `kubernetes/bootstrap/calico-installation.yaml`:

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

The required single-node setting is:

```yaml
encapsulation: None
```

Do not change the single-node bootstrap back to `VXLANCrossSubnet` without revalidating the generated Calico and kube-proxy dataplane rules.

For a future multi-node cluster, re-evaluate encapsulation based on the node/subnet topology. `None` is a deliberate requirement for the current one-node homelab, not a universal recommendation for every multi-node Calico deployment.

### Incident: September 7, 2026

The cluster initially used:

```yaml
encapsulation: VXLANCrossSubnet
```

K3s was running:

```text
v1.36.3+k3s1
```

Calico was running:

```text
v3.32.1
```

kube-proxy was running in:

```text
--proxy-mode=iptables
```

CoreDNS was healthy and reachable directly at its pod IP, but DNS through the Kubernetes Service IP failed.

Observed acceptance-test behavior:

```text
[1/6] pod -> pod IP                  PASS
[2/6] pod -> ClusterIP               PASS
[3/6] pod -> CoreDNS endpoint IP     PASS
[4/6] pod -> CoreDNS ClusterIP       FAIL
```

Additional tests showed:

- Direct DNS to the CoreDNS pod IP worked.
- TCP to `10.43.0.10:53` worked.
- UDP to `10.43.0.10:53` failed.
- External UDP DNS from pods to `1.1.1.1:53` failed.
- Host DNS to `1.1.1.1:53` worked.
- kube-dns EndpointSlice and kube-proxy DNAT destination were correct.
- Conntrack usage was far below its maximum.

### Root cause

The live Calico raw-table rules contained:

```text
-A cali-PREROUTING -p udp -j NOTRACK
```

The nftables view showed the same behavior:

```text
meta l4proto udp ... notrack
```

There was no destination-port restriction on the live rule, so it matched all UDP traffic.

This was important because the intended Calico VXLAN NOTRACK behavior is supposed to apply only to VXLAN UDP traffic (UDP/4789). On the affected cluster, the live rendered/programmed rule was broader than intended.

Because raw-table processing happens before the normal Kubernetes Service NAT path, UDP packets were marked untracked before kube-proxy could use normal conntrack-based NAT for the Service. This specifically broke UDP Service traffic while TCP Service traffic continued to work.

The resulting failure path was:

```text
Pod UDP packet
  -> Calico raw PREROUTING
  -> blanket UDP NOTRACK
  -> packet bypasses normal conntrack handling
  -> kube-proxy UDP Service NAT fails
  -> CoreDNS ClusterIP / external UDP DNS time out
```

This incident was **not** caused by:

- `/etc/resolv.conf`
- CoreDNS health
- CoreDNS RBAC
- missing kube-dns endpoints
- kube-proxy pointing to a stale CoreDNS endpoint
- conntrack exhaustion
- an iptables legacy/nft backend mismatch

### Durable fix

The Calico pool was changed to:

```yaml
encapsulation: None
```

The live operator configuration was reconciled, which produced:

```text
ipipMode: Never
vxlanMode: Never
natOutgoing: true
```

After restarting `calico-node`, the blanket UDP `NOTRACK` rule disappeared from `cali-PREROUTING`.

The network acceptance test then passed all checks:

```text
[1/6] pod -> pod IP
[2/6] pod -> ClusterIP
[3/6] pod -> CoreDNS endpoint IP
[4/6] pod -> CoreDNS ClusterIP
[5/6] Kubernetes service discovery
[6/6] external DNS required by GitOps

==> Cluster network acceptance test passed
    Pod CIDR routing:       OK
    Service ClusterIP:      OK
    CoreDNS endpoint:       OK
    CoreDNS Service:        OK
    Kubernetes DNS:         OK
    External GitOps DNS:    OK
```

### Verification commands

Verify the live Calico pool configuration:

```bash
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

Check that the blanket UDP NOTRACK rule is absent:

```bash
sudo iptables-nft -t raw \
  -L cali-PREROUTING \
  -n -v --line-numbers

sudo iptables-nft-save -t raw | \
  grep -Ei 'NOTRACK|cali-PREROUTING'
```

Then run the mandatory network acceptance test:

```bash
cd kubernetes/bootstrap
sudo bash verify-cluster-network.sh
```

Do not continue to Argo CD or application workloads unless all six checks pass.

---

## Argo CD Bootstrap Notes

### Run as the normal user

`install-argocd.sh` intentionally refuses root execution. If normal-user API access fails while `sudo kubectl` works, refresh the user kubeconfig instead of running the whole installer with `sudo`:

```bash
mkdir -p "$HOME/.kube"
sudo install -m 600 -o "$(id -u)" -g "$(id -g)" \
  /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"
```

Then run:

```bash
bash kubernetes/bootstrap/install-argocd.sh
```

### Temporary startup-time `ComparisonError`

A freshly installed Argo CD may briefly show:

```text
root-app   Unknown   Healthy
```

If the Application condition shows `connection refused` to repo-server port `8081`, and application-controller logs simultaneously show Redis connection failures, first verify that the Argo CD workloads are all ready. This can be a startup-order race rather than a persistent network problem.

Once repo-server and Redis are healthy, request a hard refresh:

```bash
sudo kubectl annotate application root-app \
  -n argocd \
  argocd.argoproj.io/refresh=hard \
  --overwrite
```

A transition from `Unknown` to `OutOfSync` is positive: Git comparison is working. The desired final root state is:

```text
root-app   Synced   Healthy
```

The App-of-Apps children may remain `Progressing`, `OutOfSync`, or temporarily `Unknown` while each application converges independently.

---

## Architecture Guidelines & Guardrails

### 1. Directory Structure Separation

To prevent linter collisions across tools, keep files partitioned by role:

- **`kubernetes/helm/`**: Contains key-value Helm override files. Checked via `yamllint` and `helm template`.
- **`kubernetes/manifests/`**: Contains raw Kubernetes API objects. Checked via `kubeconform` with `-strict`.
- **`kubernetes/argocd-apps/`**: Contains Argo CD application manifests. Checked via `kubeconform` and auto-parsed for Helm rendering checks.

### 2. Container Security Requirements

All workloads in `kubernetes/manifests/` must pass Trivy security checks by defining explicit security contexts:

- **Pod Level**: Set `runAsNonRoot: true`, non-zero `runAsUser`/`runAsGroup` IDs, and `seccompProfile.type: RuntimeDefault`.
- **Container Level**: Set `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, and drop `ALL` capabilities.
- **Storage Handling**: Mount an `emptyDir` volume to temporary write directories such as `/tmp` when `readOnlyRootFilesystem: true` is active.

### 3. Automated Helm Render Validation

The CI pipeline automatically parses Argo CD manifests under `kubernetes/argocd-apps/` using `yq` to extract chart names, repositories, and versions. Any newly added Argo CD application will be automatically rendered and validated without modifying `.github/workflows/k3s-ci.yml`.
