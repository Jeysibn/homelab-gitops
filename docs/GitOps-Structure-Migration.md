# GitOps Structure Migration

## Audit baseline

The structural refactor was merged while the live root still read
`kubernetes/argocd-apps`. The desired `root-app.yaml` now points to
`kubernetes/applications`; an existing cluster must be updated explicitly
because Argo CD does not manage the root Application from its own source.
Legacy files remain in place for rollback.

At audit time the live cluster had 13 Argo CD Applications: `root-app` and
these 12 children:

| Application | Kind of source | Namespace | Chart/version or source path | Wave | Audit status |
| --- | --- | --- | --- | --- | --- |
| `longhorn` | Helm | `longhorn-system` | `longhorn` 1.6.2 | 1 | Healthy, OutOfSync |
| `metallb` | Helm | `metallb-system` | `metallb` 0.14.8 | 1 | Healthy, OutOfSync |
| `metallb-config` | Git manifests | `metallb-system` | `kubernetes/manifests/metallb` | 2 | Healthy, Synced |
| `cert-manager` | Helm | `cert-manager` | `cert-manager` v1.14.4 | 2 | Healthy, Synced |
| `cluster-issuer` | Git manifests | `cert-manager` | `kubernetes/manifests/cert-manager` | 2 | Healthy, Synced |
| `traefik` | Helm | `traefik-system` | `traefik` 28.0.0 | 2 | Healthy, Synced |
| `unbound-dns` | Git manifests | `dns` | `kubernetes/manifests/dns` | 3 | Healthy, Synced |
| `prometheus` | Helm | `monitoring` | `prometheus` 25.19.1 | 3 | Healthy, OutOfSync |
| `loki` | Helm | `monitoring` | `loki-stack` 2.10.1 | 3 | Healthy, Synced |
| `grafana` | Helm | `monitoring` | `grafana` 7.3.7 | 3 | Healthy, Synced |
| `alloy` | Helm | `monitoring` | `alloy` 0.2.0 | 3 | Healthy, Synced |
| `pihole-dns` | Helm | `dns` | `pihole` 2.27.0 | 4 | Healthy, Synced |

The root was Healthy but OutOfSync. Prometheus had a recorded sync error because
the desired `prometheus-server` PVC request is below its reported capacity.
Longhorn reported OutOfSync CRDs, and MetalLB reported an OutOfSync CRD. These
are pre-existing conditions; this migration does not change those chart values
or attempt to repair those differences. The root's last compared Git revision
matched the audited `main` commit (`adc0ca4`), and its 12 child Application
resources were individually reported Synced even though the root was
OutOfSync. A server-side `kubectl diff` against the legacy-rendered child
Applications was empty. Comparing the new explicit files with the live child
Applications showed only the expected 12 source-path changes plus Argo CD's
controller-managed Longhorn pre-delete-hook finalizer state; there were no
Application additions or deletions. The root's OutOfSync status reason is
still unexplained, so inspect the refreshed Argo status before allowing the
root sync.

The live root has automated sync, prune, and self-heal enabled. Every child
Application also has automated prune/self-heal and the
`resources-finalizer.argocd.argoproj.io` finalizer. The initial audit also
was `pre-delete-finalizer.argocd.argoproj.io` and
`pre-delete-finalizer.argocd.argoproj.io/cleanup` on `longhorn`. These are
controller-managed: Argo CD 3.5.1 adds or removes them based on whether the
child's rendered resources contain pre-delete hooks, and runs their cleanup
stage ([upstream controller](https://github.com/argoproj/argo-cd/blob/v3.5.1/controller/appcontroller.go#L1986-L2008)).
They are not declared in the Application manifest. The explicit files retain
the standard resource finalizer and leave these hook finalizers to Argo CD.
Re-check their state immediately before cutover and do not manually remove or
hard-code them. Argo CD uses annotation tracking; live child tracking IDs
identify the owner as
`root-app:argoproj.io/Application:argocd/<application-name>`. No custom
Application health check is configured in `argocd-cm`.

The cluster is a single K3s node (`v1.36.3+k3s1`) with pod CIDR
`10.42.0.0/24` assigned to the node from the configured `10.42.0.0/16` cluster
range. The service CIDR is `10.43.0.0/16`. The audit found four Bound Longhorn
PVCs: Pi-hole (5 Gi), Grafana (5 Gi), Prometheus (10 Gi), and Loki (10 Gi).
Their PV reclaim policy is `Delete`. Existing namespaces and application
workloads were present and ready. The host's normal-user kubeconfig points at
the root-only K3s config and cannot be read by `ubuntu`; read-only inspection
used `sudo kubectl`. No cluster changes were made.

The repository does not have Helm CLI on the cluster host; workloads are
managed by Argo CD from Helm charts. Therefore `helm list` was not available as
a live inventory source. Argo CD Application sources and live Kubernetes
resources were used instead.

## File mapping

| Current file or directory | Prepared location | Notes |
| --- | --- | --- |
| `kubernetes/argocd-apps` Helm registry | `kubernetes/applications/*.yaml` | 12 explicit resources; legacy registry retained |
| `kubernetes/helm/longhorn/values.yaml` | `kubernetes/platform/longhorn/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/metallb/values.yaml` | `kubernetes/platform/metallb/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/cert-manager/values.yaml` | `kubernetes/platform/cert-manager/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/traefik/values.yaml` | `kubernetes/platform/traefik/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/pihole/values.yaml` | `kubernetes/platform/dns/pihole/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/prometheus/values.yaml` | `kubernetes/platform/monitoring/prometheus/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/loki/values.yaml` | `kubernetes/platform/monitoring/loki/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/grafana/values.yaml` | `kubernetes/platform/monitoring/grafana/values.yaml` | Byte-for-byte copy |
| `kubernetes/helm/alloy/values.yaml` | `kubernetes/platform/monitoring/alloy/values.yaml` | Byte-for-byte copy |
| `kubernetes/manifests/metallb/config.yaml` | `kubernetes/platform/metallb/manifests/config.yaml` | Byte-for-byte copy |
| `kubernetes/manifests/cert-manager/cluster-issuer.yaml` | `kubernetes/platform/cert-manager/manifests/cluster-issuer.yaml` | Byte-for-byte copy |
| `kubernetes/manifests/dns/unbound.yaml` | `kubernetes/platform/dns/unbound/manifests/unbound.yaml` | Byte-for-byte copy |
| `kubernetes/bootstrap/root-app.yaml` | unchanged during preparation | Still points at `kubernetes/argocd-apps` |

Each Helm Application uses the same upstream repository, chart, and pinned
version as the existing registry, plus a Git source with `ref: values`. Only
the Git values-file path changes. The three manifest Applications retain their
names, namespaces, sync policy, and waves; only their Git `path` changes.

## Dependency and health behavior

The existing waves are preserved: Longhorn and MetalLB wave 1; cert-manager,
Traefik, MetalLB config, and cluster issuer wave 2; Unbound and monitoring
wave 3; Pi-hole wave 4. These are sync-wave annotations on child Application
objects. The live Argo CD installation has no custom health check for an
Application resource, so waves alone do not prove that a child workload is
Healthy before a later child starts. In particular, the Pi-hole wave comment
expresses intent but is not evidence that Unbound readiness gates Pi-hole.
Adding health customization or another sequencing mechanism is outside this
structural refactor and should be reviewed separately if strict readiness
gates are required.

## Safe cutover procedure

The source-path switch is a production change. Do not run it without explicit
approval and a reviewed diff. Because root auto-pruning is enabled, pause root
automated sync before changing its path so the diff can be inspected before
any root sync.

### Preconditions and backups

1. Confirm the intended machine, K3s context, current `main` revision, and that
   the new files have been merged to `main` only after approval.
2. Capture a private, access-controlled copy of `root-app` and all child
   Application definitions, including source, sync policy, finalizers, and
   resource tracking annotations. Do not publish this capture; inspect it for
   embedded values before storing it.
3. Capture the current Application sync/health statuses, namespaces, workload
   readiness, Services, PV/PVC names, sizes, and reclaim policies. Keep the
   capture with the change record and do not include Secret data.
4. The operator has accepted possible data loss for this cutover. No
   independent data backups were verified during this task. The four current
   PVs use reclaim policy `Delete`; retain the recorded PVC identities and
   stop if the reviewed diff proposes their removal or replacement.
5. Record or resolve the pre-existing OutOfSync state of `root-app`,
   `longhorn`, `metallb`, and `prometheus`. Prometheus has a PVC shrink
   validation error; do not make a storage change as part of this cutover.
6. Confirm there are exactly 12 child Application YAML files and they match the
   live names and namespaces. Confirm the new value files and manifest
   directories are present on `main` and match the retained legacy files.

### Changes to merge to `main`

The structural files and CI/documentation changes were merged while
`root-app.yaml` still pointed at the legacy registry. This cutover change sets
`spec.source.path` to `kubernetes/applications`. Keep all legacy registry,
values, and manifest files in place; do not combine cleanup with the cutover.

### Expected Application diff

The root desired set must contain the same 12 `Application` objects in the
`argocd` namespace with the same names, project, destination namespaces,
automated prune/self-heal settings, sync options, waves, and deletion
finalizers. The explicit manifests declare the standard resource finalizer;
Argo CD continues to manage hook finalizers dynamically. There must be **no
Application resource deletion or addition** in the root diff. The expected
spec changes are limited to:

- Nine Helm Applications: Git values path changes from `kubernetes/helm/...`
  to its byte-identical path under `kubernetes/platform/...`.
- Three manifest Applications: source path changes from
  `kubernetes/manifests/...` to its byte-identical directory under
  `kubernetes/platform/...`.
- No Helm chart, chart version, release name, namespace, or values content
  change. Argo CD must retain or re-create the same hook finalizers from the
  identical rendered resources.

Updating a child Application's source path can trigger that child's own
automated reconciliation. Since every moved values file and raw manifest is
byte-for-byte identical, rendered workload resources should remain unchanged;
no workload deletion, replacement, or PVC change is expected. Inspect the
child Application diffs as well as the root diff, and stop if a workload
resource appears as a deletion, replacement, or unexpected spec change.

Argo CD's live tracking annotation remains rooted at
`root-app:argoproj.io/Application:argocd/<name>`. Matching resource identity
and tracking owner is essential to retaining ownership. If any child name,
namespace, tracking owner, finalizer, chart version, or other source changes,
stop and investigate.

### Live root update and verification

Use the approved `main` revision and the cluster's normal administrative
procedure. Keep a second operator available to observe the Applications and
workloads.

1. Pause automated sync on `root-app` only; do not remove any finalizer. Verify
   the pause took effect before proceeding.
2. Update the live root source path to `kubernetes/applications` while leaving
   the root source revision as `main` and preserving its project, destination,
   finalizer, and automated prune/self-heal settings for later restoration.
3. Refresh and inspect the root diff. Confirm it shows 12 matching child
   Application identities and only the source-path updates listed above. Stop
   if the diff shows a delete, a new Application, changed tracking ownership,
   or an unexpected Application spec change.
4. If the diff is exact, manually sync `root-app` with pruning disabled. Do not
   use a sync mode that deletes or replaces Application resources.
5. Watch all 12 child Application specs and statuses. Confirm their source
   paths and values resolve, names/namespaces/finalizers remain unchanged,
   and there are no deleted child Applications. Check workload readiness,
   Services, and all four PVC identities and Bound states.
6. Compare results with the pre-cutover baseline. Do not treat the existing
   Longhorn, MetalLB, Prometheus, and root OutOfSync conditions as new
   migration regressions; investigate any new status or resource change.
7. Once stable, restore root automated sync, prune, and self-heal to the
   approved configuration and confirm the root source path remains
   `kubernetes/applications` after reconciliation.

The cutover is complete only after all child Applications are verified
Synced/Healthy, or the previously recorded health exceptions are explicitly
accounted for, and all persistent resources retain their identities. The
current Prometheus PVC sync error and the current Longhorn/MetalLB drift need
to be rechecked at cutover; do not silently classify a changed condition as
pre-existing.

### Rollback

If the root diff is unexpected, do not sync it. If an approved sync has already
started, stop further changes and inspect the operation and Application
resources; do not force-delete Applications or remove finalizers. While root
automation is paused, restore `root-app` source path to
`kubernetes/argocd-apps`, refresh, review the diff, and manually sync with
pruning disabled only if the 12 child identities still match. Restore root
automated sync after the legacy registry is again the desired source. Keep the
new files and old directories until the cluster has returned to the recorded
baseline and rollback is verified.

Stop and request operator review if the root diff includes a deletion, any
Application disappears, a child finalizer changes, a PersistentVolumeClaim or
PersistentVolume changes identity or state, any workload becomes unavailable,
or Argo CD reports a source-resolution or tracking-owner error.

## Cleanup after cutover

Only after the approved cutover has converged and rollback is no longer
needed, remove the Helm-based registry and duplicate legacy values/manifests,
remove CI steps used only by the registry renderer, and update references in
documentation. Preserve incident reports and troubleshooting history. Cleanup
is a separate reviewed change; it is not part of the initial transition.
