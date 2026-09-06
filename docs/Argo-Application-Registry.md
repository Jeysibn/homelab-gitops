# Argo Application Registry

The `kubernetes/argocd-apps` directory is a Helm chart that acts as the Argo CD application registry. Its values file is the source of truth for standard Helm releases; chart templates render the Argo `Application` objects consumed by the root application.

## Ownership boundary

K3s and Calico are **bootstrap infrastructure** and are intentionally not present in this registry. Argo CD requires a working pod network and DNS in order to fetch Git repositories and reconcile applications, so it must never be made responsible for installing or reconciling the Calico CNI that it depends on.

The bootstrap path owns:

- K3s server configuration
- Tigera operator
- Calico `Installation/default`
- Calico IPPool (`10.42.0.0/16`)
- CoreDNS bootstrap override and networking smoke tests

The Argo registry starts above that layer and manages platform/application services such as Longhorn, MetalLB, cert-manager, Traefik, monitoring, DNS workloads, and Monikey.

Do not add a `calico-operator` Application here. CI explicitly rejects that configuration.

## Structure

| Path | Responsibility |
| --- | --- |
| `Chart.yaml` | Declares the registry chart |
| `values.yaml` | Catalog of release names, repositories, chart versions, values files, namespaces, and sync waves |
| `values.schema.json` | Validates the catalog contract during `helm lint` |
| `templates/helm-applications.yaml` | Renders standard Helm-release Applications |
| `templates/explicit-applications.yaml` | Keeps non-Helm Applications explicit |
| `tests/expected-applications.yaml` | Semantic expected facts used by CI |

## Standard release entry

Each Helm release declares:

- Stable Argo Application name
- Chart repository and chart name
- Explicit chart version
- Optional repository-owned values file
- Destination namespace
- Argo sync wave

The registry implementation supplies the repeated project, destination, automated sync, pruning, self-heal, namespace creation, and finalizer policy. Individual entries can override policy where existing behavior requires it.

## Explicit Applications

Non-Helm workloads remain readable templates under `templates/explicit-applications.yaml`. The current exceptions are:

- `metallb-config`
- `cluster-issuer`
- `unbound-dns`
- `monikey` — the only entry here that is application (not platform) workload; see `kubernetes/manifests/monikey/README.md` for its Secret bootstrap and image-promotion steps

This keeps the registry focused on the repeated Helm-release shape without hiding raw-manifest behavior behind conventions.

## Validation

CI performs the following checks:

1. Verifies `install-k3s.sh` and `calico-installation.yaml` declare the same pod CIDR.
2. Rejects a `calico-operator` entry in the Argo registry and legacy Calico `192.168.0.0/16` install paths.
3. Checks bootstrap shell scripts with `bash -n`.
4. `helm lint` validates the registry chart and catalog schema.
5. `helm template` renders all child Applications.
6. `yq` and `jq` extract stable Application facts from the render.
7. `diff` compares those facts with `tests/expected-applications.yaml`.
8. Kubeconform validates bootstrap/raw/rendered manifests against the Kubernetes minor used by K3s.
9. Trivy scans Kubernetes configuration for high/critical misconfigurations.

When adding a release, update `values.yaml` and the semantic fixture together. When changing a release, review the rendered facts for its name, source, version, namespace, sync wave, policy, and source count.
