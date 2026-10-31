# MoniKey deployment runbook

MoniKey is an unauthenticated personal finance app. Keep it on the trusted LAN
at `https://monikey.homelab.local`; do not add a public router or tunnel route.
The certificate is issued by the homelab self-signed issuer, so browsers may
show a trust warning until that issuer is trusted on the client. The ingress
uses HTTPS only; the app is not served over plain HTTP.

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

## Schema migrations

For migration `002_account_transfers.sql`, first take and verify a logical
backup using the procedure below. Then merge the reviewed GitOps change. Argo CD
runs the migration Job as a PreSync hook before applying the application
Deployments. The SQL is mirrored from MoniKey's canonical migration and is safe
to retry; a failed Job stops the sync so the new API image is not rolled out.
The hook is also invoked on later syncs, where the idempotent migration is a
no-op after its table and indexes exist.

This removes the manual `kubectl` step for applying the migration. The backup
and restore-check procedure remains manual because no protected off-cluster
backup destination is configured for automation.

Do not delete the PostgreSQL PVC to apply schema changes. A fresh empty database
gets the current schema from `monikey-db-init`; existing databases get numbered
forward migrations through the Argo CD hook.

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

For a manual GitHub Actions backup, open **Actions → MoniKey database backup →
Run workflow** on `main` and confirm the live backup input. The workflow restores
the dump into a disposable PostgreSQL container before encrypting it and
uploading a seven-day artifact. Download the encrypted `.gpg` artifact and move
it to owner-controlled encrypted storage; the artifact is temporary transport,
not long-term backup retention. Decrypt it with the passphrase stored outside
GitHub and Git.

The workflow requires these GitHub Actions repository secrets:

- `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET` for the existing Tailscale CI
  connection.
- `MONIKEY_BACKUP_KUBECONFIG_B64`: base64-encoded kubeconfig using a
  namespace-scoped service account. It needs `get` on pods and `create` on
  `pods/exec` in `monikey`; the `monikey-backup` ServiceAccount and Role are in
  `kubernetes/platform/monikey/manifests/backup-access.yaml`. Its API endpoint
  must be reachable through the accepted Tailscale routes. Keep the token
  limited to this namespace and rotate it when it expires.
- `MONIKEY_BACKUP_PASSPHRASE`: a high-entropy passphrase used to encrypt the
  artifact with GPG AES-256. Save it in the owner's password manager; it is
  required to recover the artifact.

Do not print or commit any secret. Before merging a database migration, run the
backup workflow and confirm both the restore check and encrypted artifact
completed successfully.

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
