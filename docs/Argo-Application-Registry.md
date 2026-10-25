# Legacy Argo CD Application Registry

`kubernetes/argocd-apps` is the legacy Helm-based registry that was previously
referenced by the live `root-app`. It generates nine Helm Applications from
`values.yaml` and declares the three raw-manifest Applications in
`templates/explicit-applications.yaml`.

The live root now points to `kubernetes/applications/`, with service values and
manifests under `kubernetes/platform/`. The old registry remains available
during the rollback window. Remove it only after the new source has converged,
remaining health issues are understood, and rollback no longer depends on the
old paths. See [GitOps Structure Migration](GitOps-Structure-Migration.md) for
the audit, path mapping, validation, cutover record, and rollback procedure.

## Existing ownership boundary

K3s and Calico are bootstrap infrastructure, not Applications. Argo CD
depends on a working pod network and DNS to fetch Git repositories and
reconcile services, so it must not own the Calico CNI it depends on.

The bootstrap path owns K3s server configuration, the Tigera operator, Calico
`Installation/default`, the Calico IPPool (`10.42.0.0/16`), CoreDNS bootstrap
configuration, and network acceptance checks. Argo CD manages Longhorn,
MetalLB, cert-manager, Traefik, DNS, and monitoring.

## Legacy registry layout

| Path | Legacy responsibility |
| --- | --- |
| `Chart.yaml` | Registry chart metadata |
| `values.yaml` | Helm Application names, repositories, chart versions, values paths, namespaces, and sync waves |
| `values.schema.json` | Registry values validation |
| `templates/helm-applications.yaml` | Generated Helm-chart Applications |
| `templates/explicit-applications.yaml` | `metallb-config`, `cluster-issuer`, and `unbound-dns` |
| `tests/expected-applications.yaml` | Historical expected Application facts |

The Kubernetes workflow continues to lint-render this registry while it is
retained for rollback. The explicit files are checked for YAML structure and
source paths, and their manifests are included in Kubernetes validation. The
Application CRD schema was additionally checked with server-side dry-runs.

Do not edit the live root path or remove these legacy files as part of routine
application updates until the cutover is approved and complete.
