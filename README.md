# 🚀 Enterprise GitOps Homelab

![Kubernetes](https://img.shields.io/badge/Kubernetes-K3s-blue?logo=kubernetes)
![ArgoCD](https://img.shields.io/badge/GitOps-ArgoCD-orange?logo=argo)
![Terraform](https://img.shields.io/badge/IaC-Terraform-purple?logo=terraform)
![Proxmox](https://img.shields.io/badge/Hypervisor-Proxmox-darkgreen?logo=proxmox)

A GitOps-driven local Kubernetes homelab using **Terraform**, **K3s**, **Calico**, and **Argo CD**.

## 🏗️ Architecture

![System Architecture Diagram](docs/Architecture.png)

- **Infrastructure:** Proxmox VM provisioned with Terraform.
- **Cluster:** K3s.
- **CNI:** Calico, installed during bootstrap.
- **Pod CIDR:** `10.42.0.0/16`.
- **Service CIDR:** `10.43.0.0/16`.
- **CoreDNS Service IP:** `10.43.0.10`.
- **GitOps:** Argo CD App-of-Apps watching `main`.
- **Ingress:** Traefik.
- **Load balancing:** MetalLB.
- **Storage:** Longhorn.
- **Observability:** Prometheus, Loki, Grafana, and Alloy.
- **LAN DNS:** Pi-hole and Unbound.

See [docs/Service-Catalog.md](docs/Service-Catalog.md) for service URLs and [docs/Proxmox-Environment.md](docs/Proxmox-Environment.md) for the homelab inventory.

## ⚙️ GitOps Workflow

1. Make changes on `dev`.
2. GitHub Actions validates shell scripts, YAML, Helm rendering, and Kubernetes schemas.
3. Merge `dev` into `main`.
4. Argo CD reconciles the cluster from `main`.

Calico is bootstrap-owned because Argo CD requires a working CNI before it can operate.

## 🚀 Fresh Bootstrap

The normal deployment flow starts by cloning this repository onto the new Ubuntu VM.

```bash
git clone https://github.com/Jeysibn/homelab-gitops.git
cd homelab-gitops
bash bootstrap.sh
```

**Do not run the bootstrap with `sudo`.** Run it as your normal Ubuntu user. The scripts call `sudo` only for the host operations that require root privileges and create kubeconfig for the user who launched the bootstrap.

`bootstrap.sh` automatically determines the cloned repository directory from its own location, then runs:

```text
bootstrap.sh
├── kubernetes/bootstrap/install-k3s.sh
│   ├── configure host networking
│   ├── install K3s
│   ├── install Calico
│   └── verify cluster networking
└── kubernetes/bootstrap/install-argocd.sh
    ├── install Argo CD
    └── apply root-app.yaml
```

You do not need to `chmod +x` the scripts when using `bash bootstrap.sh`.

### Run stages separately

K3s and Calico only:

```bash
bash kubernetes/bootstrap/install-k3s.sh
```

Argo CD only:

```bash
bash kubernetes/bootstrap/install-argocd.sh
```

The scripts resolve their manifest and helper paths from the directory where the scripts themselves are stored, so they do not depend on your current working directory.

## ✅ Bootstrap Safety Checks

The K3s bootstrap keeps only the checks required for a predictable fresh installation:

- Calico CIDR must match the K3s pod CIDR.
- UFW/firewalld must not silently block cluster networking.
- K3s must use a real upstream resolver instead of a loopback DNS stub.
- Calico must become ready.
- The node and CoreDNS must become ready.
- The cluster network acceptance test must pass before Argo CD is installed.

The separate acceptance test verifies pod routing, Service ClusterIP routing, CoreDNS, Kubernetes service discovery, and external GitHub DNS:

```bash
bash kubernetes/bootstrap/verify-cluster-network.sh
```

Recovery logic is intentionally kept outside the normal fresh bootstrap. For an old cluster with stale Calico IPAM state, inspect it separately with:

```bash
bash kubernetes/bootstrap/recover-calico-ipam.sh --plan
```

## 🔎 Verify the Cluster

After bootstrap:

```bash
kubectl get nodes
kubectl get pods -A -o wide
kubectl get applications -n argocd
kubectl get svc -A | grep LoadBalancer
```

Argo CD's root application points to `main`, so GitOps-managed services are reconciled from the production branch.

## 🌐 Local Routing

Traefik is the main HTTP/HTTPS entrypoint at `192.168.86.200`. Pi-hole provides LAN DNS at `192.168.86.201` and resolves `*.homelab.local` hostnames to Traefik.

| Service | Hostname |
| --- | --- |
| Grafana | `grafana.homelab.local` |
| Pi-hole | `pihole.homelab.local` |
| Longhorn | `longhorn.homelab.local` |
| Prometheus | `prometheus.homelab.local` |
| MoniKey | `monikey.homelab.local` |

## 🔐 Secrets

Some application credentials are still placeholders. A future improvement is to manage them with SOPS, Sealed Secrets, or External Secrets Operator.
