# 🚀 Enterprise GitOps Homelab

![Kubernetes](https://img.shields.io/badge/Kubernetes-K3s-blue?logo=kubernetes)
![ArgoCD](https://img.shields.io/badge/GitOps-ArgoCD-orange?logo=argo)
![Terraform](https://img.shields.io/badge/IaC-Terraform-purple?logo=terraform)
![Proxmox](https://img.shields.io/badge/Hypervisor-Proxmox-darkgreen?logo=proxmox)

A production-grade, GitOps-driven local Kubernetes cluster. This repository serves as the single source of truth for my infrastructure, utilizing **Infrastructure as Code (IaC)** and the **ArgoCD App-of-Apps** pattern for zero-touch provisioning.

## 🏗️ Architecture & Tech Stack

*   **Infrastructure:** Proxmox VMs provisioned via **Terraform**.
*   **Cluster Engine:** **K3s** (Lightweight Kubernetes).
*   **GitOps Controller:** **ArgoCD** (App-of-Apps pattern with Sync Waves).
*   **Networking & Ingress:** **Calico** (CNI + IPAM LoadBalancer), **Traefik** (Ingress), **Cert-Manager** (TLS).
*   **DNS & Routing:** **Pi-hole** (Ad-block) + **Unbound** (Recursive) + **ExternalDNS** (Zero-touch records).
*   **Storage:** **Longhorn** (Highly Available CSI).
*   **Observability (Decoupled):** **Prometheus** (Metrics), **Loki** (Logs), **Grafana** (Visualization).
*   **Remote Access:** **Tailscale** (Subnet routing).

## ⚙️ The GitOps Workflow

1. **Develop:** Code changes (Terraform or Kubernetes YAML) are pushed to a feature branch.
2. **Validate:** GitHub Actions CI pipelines automatically lint and validate manifests (`kubeconform`, `tflint`).
3. **Merge:** Upon PR approval, code is merged to `main`.
4. **Reconcile:** ArgoCD detects state drift and automatically synchronizes the K3s cluster to match the repository.

## 🚀 Bootstrap Process (Disaster Recovery)

The entire cluster can be rebuilt from bare metal in under 15 minutes:

1. Provision VMs: `cd terraform && terraform apply`
2. Bootstrap K3s & CNI: `./kubernetes/bootstrap/k3s-install.sh`
3. Install GitOps Engine: `kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml`
4. Trigger Automation: `kubectl apply -f kubernetes/root-app-of-apps.yaml`

