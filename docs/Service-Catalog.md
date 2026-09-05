# Homelab Service Catalog

This catalog documents how core homelab services are exposed inside the cluster and on the LAN.

## Network entrypoints

| Component | Namespace | Exposure | Address | Purpose |
| --- | --- | --- | --- | --- |
| Traefik | `traefik-system` | LoadBalancer | `192.168.86.200` | Main HTTP/HTTPS reverse proxy for `*.homelab.local` |
| Pi-hole DNS | `dns` | LoadBalancer | `192.168.86.201:53/TCP,UDP` | LAN DNS resolver and ad-blocking entrypoint |
| Unbound | `dns` | ClusterIP | `unbound.dns.svc.cluster.local:5335` | Recursive upstream resolver for Pi-hole |

## Web services

| Service | Namespace | Exposure | Hostname | Notes |
| --- | --- | --- | --- | --- |
| Grafana | `monitoring` | Traefik Ingress | `grafana.homelab.local` | Dashboards for metrics and logs |
| Pi-hole Web UI | `dns` | Traefik Ingress | `pihole.homelab.local` | Web UI only; DNS traffic stays on `192.168.86.201` |
| Longhorn UI | `longhorn-system` | Traefik Ingress | `longhorn.homelab.local` | Storage administration UI |
| Prometheus | `monitoring` | Traefik Ingress | `prometheus.homelab.local` | Metrics backend; consider restricting later |
| Monikey | `monikey` | Traefik Ingress | `monikey.homelab.local` | Personal finance app; Nginx `web` proxies `/api` to the `api` Service |

## Internal-only services

| Service | Namespace | Exposure | Notes |
| --- | --- | --- | --- |
| Loki | `monitoring` | ClusterIP | Queried by Grafana |
| Alloy | `monitoring` | Internal workload | Collects Kubernetes pod logs and forwards them to Loki |
| cert-manager | `cert-manager` | Internal controllers/webhook | Manages local TLS resources |
| MetalLB | `metallb-system` | Internal controllers/speakers | Allocates LAN LoadBalancer IPs |
| Monikey `api` / `worker` | `monikey` | ClusterIP / internal | `worker` runs background jobs only, no Service |
| Monikey `postgres` | `monikey` | ClusterIP (headless) | Longhorn-backed, single-node StatefulSet |

## DNS model

Pi-hole should resolve the homelab zone to Traefik:

```text
*.homelab.local -> 192.168.86.200
```

The current Pi-hole values use dnsmasq to map the zone:

```text
address=/homelab.local/192.168.86.200
```

Pi-hole forwards upstream DNS queries to Unbound inside the cluster.

## Verification commands

```bash
kubectl get svc -A | grep LoadBalancer
kubectl get ingress -A
nslookup grafana.homelab.local 192.168.86.201
nslookup pihole.homelab.local 192.168.86.201
nslookup longhorn.homelab.local 192.168.86.201
nslookup prometheus.homelab.local 192.168.86.201
nslookup monikey.homelab.local 192.168.86.201
```
