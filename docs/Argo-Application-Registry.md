# Legacy Argo CD Application Registry

`kubernetes/argocd-apps` is the Helm-based registry currently referenced by
the live `root-app`. It generates nine Helm Applications from
`values.yaml` and declares the three raw-manifest Applications in
`templates/explicit-applications.yaml`.

The registry remains in the repository during the structural transition. The
prepared replacement is `kubernetes/applications/`, with service values and
manifests copied under `kubernetes/platform/`. The live root source remains
unchanged until the separately approved cutover. Keep the registry and old
configuration paths until the new source has converged and rollback is no
longer needed. See [GitOps Structure Migration](GitOps-Structure-Migration.md)
for the audit, path mapping, validation, and cutover procedure.

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

While this registry remains the live source, the Kubernetes workflow still
lint-renders it. The new files are also checked for YAML structure and source
paths, and their manifests are included in Kubernetes validation. The live
Application CRD schema was additionally checked with server-side dry-runs
during this refactor.

Do not edit the live root path or remove these legacy files as part of routine
application updates until the cutover is approved and complete.
