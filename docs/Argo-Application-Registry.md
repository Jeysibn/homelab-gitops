# Argo Application Registry

The `kubernetes/argocd-apps` directory is a Helm chart that acts as the Argo CD application registry. Its values file is the source of truth for standard Helm releases; chart templates render the Argo `Application` objects consumed by the root application.

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

This keeps the registry focused on the repeated Helm-release shape without hiding raw-manifest behavior behind conventions.

## Validation

CI performs the following checks:

1. `helm lint` validates the registry chart and catalog schema.
2. `helm template` renders all child Applications.
3. `yq` and `jq` extract stable Application facts from the render.
4. `diff` compares those facts with `tests/expected-applications.yaml`.
5. Kubeconform and Trivy validate the rendered Kubernetes output.

When adding a release, update `values.yaml` and the semantic fixture together. When changing a release, review the rendered facts for its name, source, version, namespace, sync wave, policy, and source count.
