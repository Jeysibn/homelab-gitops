# MoniKey deployment runbook

MoniKey is an unauthenticated personal finance app. Keep it on the trusted LAN
at `https://monikey.homelab.local`; do not add a public router or tunnel route.
The certificate is issued by the homelab self-signed issuer, so browsers may
show a trust warning until that issuer is trusted on the client. The ingress
uses HTTPS only; the app is not served over plain HTTP.

## First deployment

The `monikey` Argo CD Application creates the namespace and deploys the frontend,
API, and one PostgreSQL StatefulSet. GHCR images are pinned to the same full
commit SHA. The app and database Services are ClusterIP; only Traefik accepts
connections from the LAN. The API Service is named `backend` to match the
hostname used by the Nginx configuration in the frontend image.

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

PostgreSQL has no schema init ConfigMap. On a genuinely empty database, the
versioned migration Job initializes the schema and seed after PostgreSQL is
ready. Never delete the PVC to rerun initialization or to repair an Argo CD diff.

## Schema migrations

Use the automated release workflow in `homelab-gitops` for normal releases. It
reuses the backup workflow below, waits for its restore check, then promotes the
matching immutable frontend/API SHA tags. Argo CD applies the migration Job
before the application Deployments. It reads MoniKey's canonical migration
files from that SHA's API image, takes a PostgreSQL advisory lock, and records
versions and checksums in `schema_migrations`. A failed migration blocks the
API/frontend rollout. No live SQL is run from a shell on the homelab machine.

Before enabling automatic promotion for the first time, run **Actions → MoniKey
schema adoption** on `main` with a full MoniKey image SHA. The workflow creates
and restore-verifies a backup first, then waits for approval in the protected
`monikey-adoption` environment. Its Kubernetes Job verifies a known complete
schema and records the checksums for migrations already present without running
SQL migrations. If the live schema is partial, unknown, or does not match the
expected inventory, adoption stops. Only after its success should
`MONIKEY_GITOPS_AUTOMATION_ENABLED=true` be set in MoniKey repository variables.

Do not delete the PostgreSQL PVC to apply schema changes. Empty databases are
initialized by the same migration Job; existing databases require the one-time
adoption before the numbered forward migrations can run.

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
Run workflow** on `main` and confirm the live backup input. The same workflow
runs automatically before every promotion. It restores the dump into a
disposable PostgreSQL container before encrypting it and uploading a seven-day
artifact. Download the encrypted `.gpg` artifact and move it to owner-controlled
encrypted storage; the artifact is temporary transport, not long-term backup
retention. Decrypt it with the passphrase stored outside GitHub and Git.

The workflow requires these GitHub Actions repository secrets:

- `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET` for the existing Tailscale CI
  connection.
- `MONIKEY_BACKUP_KUBECONFIG_B64`: base64-encoded kubeconfig using a
  namespace-scoped service account. It needs `get` on pods and `get`/`create`
  on `pods/exec` for `postgres-0` in `monikey`; the `monikey-backup`
  ServiceAccount and Role are in
  `kubernetes/platform/monikey/manifests/backup-access.yaml`. The `get` verb
  supports kubectl's WebSocket exec transport; `create` supports POST-based
  clients. Its API endpoint must be reachable through the accepted Tailscale
  routes. Keep the token limited to this namespace and rotate it when it expires.
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

To roll back an application image, set both API and frontend to the same previous
full SHA in `kubernetes/platform/monikey/manifests/application.yaml` and merge
that Git change. Leave `migration.yaml` on the newest runner image so it
understands the current ledger. Argo CD runs the migration check and rolls the
Deployments back. The database schema and PVC are not rolled back with the
images; use only an API known to support the current schema.
