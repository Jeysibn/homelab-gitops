# MoniKey deployment runbook

MoniKey is an unauthenticated personal finance app. Keep it on the trusted LAN
at `https://monikey.homelab.local`; do not add a public router or tunnel route.
The certificate is issued by the homelab self-signed issuer, so browsers may
show a trust warning until that issuer is trusted on the client.

## First deployment

The `monikey` Argo CD Application creates the namespace and deploys the frontend,
API, and one PostgreSQL StatefulSet. GHCR images are public and pinned to full
commit SHA tags. The app and database Services are ClusterIP; only Traefik
accepts connections from the LAN. The API Service is named `backend` to match
the hostname used by the Nginx configuration in the frontend image.

Create the runtime Secret on the cluster before merging the Argo CD Application.
Keep its password in a password manager or a private file outside Git. The
password must be URL-safe (hexadecimal is suitable), because it is also used in
`DATABASE_URL`:

```bash
kubectl create namespace monikey
kubectl create secret generic monikey-db -n monikey \
  --from-env-file=/secure/path/monikey-db.env
```

The env file contains these two entries, with the same generated password:

```text
POSTGRES_PASSWORD=<url-safe-password>
DATABASE_URL=postgresql://monikey_app:<url-safe-password>@postgres:5432/monikey_dev
```

Use file permissions that restrict access to the owner (`chmod 600`). Do not
commit the file or paste its contents into logs, tickets, or chat. Kubernetes
Secrets are not encrypted by base64 encoding; access is limited to cluster
administrators and the MoniKey pods.

The ConfigMap `monikey-db-init` mirrors the canonical scripts in the MoniKey
repository. PostgreSQL runs them only when the Longhorn-backed volume is empty.
Keep both copies in sync when changing the initial schema or seed. Never delete
the PVC to rerun initialization or to repair an Argo CD diff.

## Verify the deployment

```bash
kubectl get applications -n argocd monikey
kubectl get pods,svc,pvc,ingress -n monikey
kubectl get certificate -n monikey
curl --insecure --fail https://monikey.homelab.local/api/health/ready
```

`--insecure` is needed only while the self-signed issuer is not trusted by the
client. The app has no login; use it only on a trusted private LAN.

## Logical backup and restore

Store backups outside Git in encrypted storage with access limited to the owner.
Run the backup command from a machine with cluster access; redirect its output
to the chosen protected backup location:

```bash
kubectl exec -n monikey postgres-0 -- \
  pg_dump -U monikey_app -d monikey_dev -Fc > monikey-$(date +%F).dump
```

Restore into a new, empty database for a check or a planned recovery. Keep the
current database untouched until the restored copy has been verified:

```bash
kubectl exec -n monikey postgres-0 -- \
  createdb -U monikey_app monikey_restore_check
kubectl exec -i -n monikey postgres-0 -- \
  pg_restore -U monikey_app -d monikey_restore_check --no-owner \
  < monikey-YYYY-MM-DD.dump
kubectl exec -n monikey postgres-0 -- \
  psql -U monikey_app -d monikey_restore_check -c '\dt'
```

After verifying a test restore, remove only the temporary database:

```bash
kubectl exec -n monikey postgres-0 -- \
  dropdb -U monikey_app monikey_restore_check
```

For recovery, stop the API before restoring into the live database and preserve
the existing PVC and a separate backup copy. Confirm the target database before
any restore that replaces existing records.

## Rollback

To roll back an application image, restore the previous full SHA tag in
`kubernetes/platform/monikey/manifests/application.yaml` and merge that reviewed
Git change. Argo CD will roll the Deployment back. The database schema and PVC
are not rolled back with the images; take a logical backup before schema changes
and do not run an older API against a schema it cannot read.
