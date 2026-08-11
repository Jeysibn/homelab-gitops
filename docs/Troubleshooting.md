# CI/CD & Kubernetes Manifest Troubleshooting Guide

This document details historical configuration issues, root causes, and technical resolutions for the GitOps CI/CD pipeline defined in `.github/workflows/k3s-ci.yml`[cite: 1].

## Summary of Issues & Fixes

| Incident | Severity | Root Cause | Technical Resolution |
| :--- | :--- | :--- | :--- |
| **Missing CRD Schemas** | High | `kubeconform` lacks built-in schemas for Custom Resources (`IPPool`, `ClusterIssuer`, `Application`). | Integrated `datreeio/CRDs-catalog` schema location and added `-ignore-missing-schemas`. |
| **Helm Values Parsing Failure** | High | `kubeconform` attempted to validate Helm `values.yaml` files as raw Kubernetes API manifests. | Separated repo into `kubernetes/helm/` (values)[cite: 1] and `kubernetes/manifests/` (raw manifests)[cite: 1]. |
| **Action Resolution Failure** | Medium | `yannh/kubeconform-action` failed to resolve on GitHub Actions runners. | Replaced action with direct binary download (`curl`) in the runner environment. |
| **Traefik Values Schema Error** | High | Unpinned Helm chart versions evaluated against updated upstream schema breaking `redirectTo` & `tls` keys. | Aligned `values.yaml` syntax and pinned chart versions dynamically via ArgoCD `targetRevision` specs. |
| **Script Parsing Failures** | High | `jq` crashed on scalar strings and `yq` passed YAML document separators (`---`) as chart names. | Updated rendering step to `yq eval-all -N` with token filtering inside the bash loop. |
| **Trivy Security Scan Failures** | High | `unbound.yaml` failed security checks (`KSV-0014`, `KSV-0118`) due to unconfigured security contexts. | Added non-root `securityContext`, dropped capabilities, enabled `readOnlyRootFilesystem`, and added `/tmp` `emptyDir`. |
| **Workflow Execution Filtering** | Low | Pipeline did not trigger on custom feature or test branches. | Updated `on.push.branches` filters and added `workflow_dispatch` for manual UI triggers. |

---

## Architecture Guidelines & Guardrails

### 1. Directory Structure Separation
To prevent linter collisions across tools, keep files partitioned by role:
* **`kubernetes/helm/`**: Contains key-value Helm override files[cite: 1]. Checked via `yamllint`[cite: 1] and `helm template`.
* **`kubernetes/manifests/`**: Contains raw Kubernetes API objects[cite: 1]. Checked via `kubeconform` with `-strict`.
* **`kubernetes/argocd-apps/`**: Contains ArgoCD application manifests[cite: 1]. Checked via `kubeconform` and auto-parsed for Helm rendering checks.

### 2. Container Security Requirements
All workloads in `kubernetes/manifests/`[cite: 1] must pass Trivy security checks by defining explicit security contexts:
* **Pod Level**: Set `runAsNonRoot: true`, non-zero `runAsUser`/`runAsGroup` IDs, and `seccompProfile.type: RuntimeDefault`.
* **Container Level**: Set `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, and drop `ALL` capabilities.
* **Storage Handling**: Mount an `emptyDir` volume to temporary write directories (such as `/tmp`) when `readOnlyRootFilesystem: true` is active.

### 3. Automated Helm Render Validation
The CI pipeline automatically parses ArgoCD manifests under `kubernetes/argocd-apps/`[cite: 1] using `yq` to extract chart names, repositories, and versions. Any newly added ArgoCD application will be automatically rendered and validated without modifying `.github/workflows/k8s-ci.yml`[cite: 1].