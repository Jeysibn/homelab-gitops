# Monikey

Personal finance app (https://github.com/Jeysibn/monikey). Deployed in the
`monikey` namespace with an ordered Argo CD sync sequence:

| Wave | File | Workload | Notes |
| --- | --- | --- | --- |
| 0 | `configmap.yaml` | `monikey-config` ConfigMap | Non-secret application configuration |
| 0 | `receipts-pvc.yaml` | `monikey-receipts` PVC | Longhorn-backed receipt storage |
| 0 | `postgres.yaml` | `postgres` StatefulSet + Service | Longhorn-backed database; must be healthy before migrations |
| 1 | `migrate-job.yaml` | `monikey-migrate` Sync hook | Runs `prisma migrate deploy` after prerequisites are healthy |
| 2 | `api.yaml` | `api` Deployment + Service | `node dist/server.js`, HTTP on 3000 |
| 2 | `worker.yaml` | `worker` Deployment | `node dist/worker.js`, background jobs |
| 2 | `web.yaml` | `web` Deployment + Service | Nginx SPA + `/api` reverse proxy, HTTP on 80 |
| 3 | `ingress.yaml` | Traefik `Ingress` | `monikey.homelab.local` -> `web` |

The migration Job is deliberately a `Sync` hook rather than `PreSync`.
`PreSync` runs before all normal Sync resources, which means a fresh cluster
would start migrations before the ConfigMap, PVCs, and PostgreSQL resources
exist. The wave ordering above lets Argo CD create and wait for those
prerequisites before running Prisma, then deploys the application workloads
only after the migration succeeds.

## Images

Built and pushed by `.github/workflows/publish.yaml` in the Monikey repo:

- `ghcr.io/jeysibn/monikey-api:latest` (shared by `api`, `worker`, and migration)
- `ghcr.io/jeysibn/monikey-web:latest`

The publish workflow also creates commit-SHA tags. The current homelab manifests
track `:latest`, so a new registry push by itself does not change Git desired
state; a pod rollout or a Git manifest change is still required for already
running pods to consume a newly moved `latest` tag.

## Secrets

`monikey-secrets` is **not** committed here (no sealed-secrets/SOPS is set up
in this homelab yet — see `docs/Argo-Application-Registry.md`). Create it
once per cluster before the `monikey` Application first syncs:

```bash
kubectl create namespace monikey
kubectl create secret generic monikey-secrets -n monikey \
  --from-literal=DATABASE_URL='postgresql://monikey:<password>@postgres:5432/monikey' \
  --from-literal=POSTGRES_PASSWORD='<password>' \
  --from-literal=ENCRYPTION_SECRET="$(openssl rand -hex 32)"
```

Because the Secret lives outside Git, it must already exist before PostgreSQL
and the migration Job can become healthy. All other Monikey environment
variables are non-secret and live in `configmap.yaml`.

If you rotate `POSTGRES_PASSWORD`, update both the Secret and the running
Postgres role (`ALTER ROLE monikey WITH PASSWORD '...'`) — this manifest set
does not automate that.
