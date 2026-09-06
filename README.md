# 🚀 Enterprise GitOps Homelab

![Kubernetes](https://img.shields.io/badge/Kubernetes-K3s-blue?logo=kubernetes)
![ArgoCD](https://img.shields.io/badge/GitOps-ArgoCD-orange?logo=argo)
![Terraform](https://img.shields.io/badge/IaC-Terraform-purple?logo=terraform)
![Proxmox](https://img.shields.io/badge/Hypervisor-Proxmox-darkgreen?logo=proxmox)

A GitOps-driven local Kubernetes homelab. This repository is the source of truth for the infrastructure, using **Terraform**, **K3s**, and the **Argo CD App-of-Apps** pattern to manage cluster services from Git.

## 🏗️ Architecture & Tech Stack

![System Architecture Diagram](docs/Architecture.png)

* **Infrastructure:** Proxmox VM provisioned with Terraform.
* **Cluster Engine:** K3s lightweight Kubernetes.
* **Bootstrap Networking:** Calico CNI is installed and owned exclusively by bootstrap, with pod CIDR `10.42.0.0/16`, Service CIDR `10.43.0.0/16`, and CoreDNS Service IP `10.43.0.10`.
* **GitOps Controller:** Argo CD App-of-Apps with sync waves. Argo starts only after pod routing, ClusterIP routing, CoreDNS service routing, Kubernetes service discovery, and external GitHub DNS all pass a network acceptance test.
* **Ingress & Load Balancing:** MetalLB LoadBalancer IPAM and Traefik ingress.
* **TLS:** cert-manager with local self-signed issuer by default. Public Let’s Encrypt requires a real DNS domain and reachable HTTP-01 or DNS-01 validation.
* **DNS & Routing:** Pi-hole for LAN DNS/ad-blocking and Unbound for recursive upstream DNS. Kubernetes CoreDNS uses the stock K3s Corefile and the node's real resolver file; bootstrap does not inject a second `forward .` override.
* **Storage:** Longhorn CSI. Current setup is single-node with one replica; HA requires additional nodes/disks.
* **Observability:** Prometheus metrics, Loki logs, Grafana dashboards, and Alloy log collection.
* **Remote Access:** Tailscale for CI or remote homelab access.

See [docs/Service-Catalog.md](docs/Service-Catalog.md) for service URLs, namespaces, and exposure methods.

See [docs/Proxmox-Environment.md](docs/Proxmox-Environment.md) for the current Proxmox node and K3s VM inventory, including the captured environment overview.

## ⚙️ GitOps Workflow

1. **Develop:** Changes are made on `dev`.
2. **Validate:** GitHub Actions checks bootstrap networking invariants, shell syntax, Kubernetes manifests, Helm rendering, Kubeconform, Trivy, Terraform format/validate, and TFLint where applicable.
3. **Promote:** A pull request merges `dev` into `main` after checks pass.
4. **Reconcile:** Argo CD watches `main`, renders the application registry, and reconciles cluster services to match the repository.

Calico is intentionally excluded from the Argo application registry. Argo CD depends on a working CNI, so K3s and Calico are treated as bootstrap infrastructure rather than GitOps-managed child applications.

The Argo CD Application registry is documented in [docs/Argo-Application-Registry.md](docs/Argo-Application-Registry.md).

## 🚀 Bootstrap Process

Use this flow for a new node or disaster recovery rebuild.

1. Provision the VM:

   ```bash
   cd terraform
   terraform apply
   ```

2. Install K3s and Calico:

   ```bash
   ./kubernetes/bootstrap/install-k3s.sh
   ```

   Bootstrap refuses to complete unless all of the following are true:

   - K3s and Calico both use pod CIDR `10.42.0.0/16`.
   - K3s uses Service CIDR `10.43.0.0/16` and CoreDNS `10.43.0.10`.
   - Pod and Service CIDRs do not overlap.
   - No active legacy Calico IPPool/IPAM block exists outside `10.42.0.0/16`.
   - The host is not running an unconfigured UFW/firewalld policy.
   - K3s uses a real resolver file rather than the systemd-resolved `127.0.0.53` stub.
   - pod → pod traffic works.
   - pod → ClusterIP traffic works.
   - pod → CoreDNS endpoint and pod → CoreDNS Service traffic work.
   - Kubernetes service discovery works.
   - `github.com` resolves from a pod through CoreDNS.

3. Install the pinned Argo CD release and apply the root application:

   ```bash
   ./kubernetes/bootstrap/install-argocd.sh
   ```

   Argo CD reruns the same network acceptance test before installation, verifies `argocd-repo-server` can resolve GitHub, and requires `root-app` to reach `Synced` before reporting success.

4. Verify Argo CD and cluster services:

   ```bash
   kubectl get ippools.crd.projectcalico.org
   kubectl get pods -A -o wide
   kubectl get app -n argocd
   kubectl get svc -A | grep LoadBalancer
   ```

The acceptance test can also be run independently:

```bash
bash ./kubernetes/bootstrap/verify-cluster-network.sh
```

If an older cluster contains Calico allocations outside `10.42.0.0/16`, inspect them separately with:

```bash
bash ./kubernetes/bootstrap/recover-calico-ipam.sh --plan
```

The normal bootstrap never performs destructive IPAM cleanup automatically.

## 🌐 Local Routing Model

Traefik is the main HTTP/HTTPS entrypoint at `192.168.86.200`. Pi-hole provides LAN DNS on `192.168.86.201` and resolves `*.homelab.local` hostnames back to Traefik.

Expected core routes:

| Service | Hostname |
| --- | --- |
| Grafana | `grafana.homelab.local` |
| Pi-hole Web UI | `pihole.homelab.local` |
| Longhorn UI | `longhorn.homelab.local` |
| Prometheus | `prometheus.homelab.local` |
| Monikey | `monikey.homelab.local` |

## 🔐 Secrets Status

Some admin passwords are still placeholders in Helm values. The next recommended milestone is to move application credentials into a proper secrets workflow such as SOPS, Sealed Secrets, or External Secrets Operator.
