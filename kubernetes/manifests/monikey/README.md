# Monikey

Personal finance app (https://github.com/Jeysibn/monikey). Deployed as four
workloads in the `monikey` namespace:

| File | Workload | Notes |
| --- | --- | --- |
| `postgres.yaml` | `postgres` StatefulSet + Service | Longhorn-backed PVC, matches `compose.yaml`'s topology |
| `migrate-job.yaml` | `monikey-migrate` Job | Argo `PreSync` hook — runs `prisma migrate deploy` before api/worker roll out |
| `api.yaml` | `api` Deployment + Service | `node dist/server.js`, HTTP on 3000 |
| `worker.yaml` | `worker` Deployment | `node dist/worker.js`, no HTTP surface (background jobs) |
| `web.yaml` | `web` Deployment + Service | Nginx SPA + `/api` reverse proxy, HTTP on 80 |
| `ingress.yaml` | Traefik `Ingress` | `monikey.homelab.local` -> `web` |

## Images

Built and pushed by `.github/workflows/publish.yaml` in the monikey repo,
tagged with the commit SHA:

- `ghcr.io/jeysibn/monikey-api:<sha>` (shared by `api` and `worker`)
- `ghcr.io/jeysibn/monikey-web:<sha>`

Bump the `image:` tag in `api.yaml`, `worker.yaml`, `migrate-job.yaml`, and
`web.yaml` to promote a new build — this repo pins exact tags rather than
tracking `:latest`, so a promotion is always an explicit, reviewable commit.

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

Because the Secret lives outside Git, Argo CD will always show the `monikey`
Application as missing that one resource — this is expected and is why
`ignoreDifferences`/an out-of-band Secret is the deliberate tradeoff chosen
over standing up sealed-secrets for a single app (see PR discussion). All
other Monikey env vars are non-secret and live in `configmap.yaml` instead.

If you rotate `POSTGRES_PASSWORD`, update both the Secret and the running
Postgres role (`ALTER ROLE monikey WITH PASSWORD '...'`) — this manifest set
does not automate that.
