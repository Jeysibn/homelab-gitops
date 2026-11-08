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

Normal MoniKey releases are dispatched only by the successful MoniKey
main-branch CI workflow. The dispatch includes its workflow run ID and full
commit SHA; GitOps verifies the run was a successful push to MoniKey main and
confirms both paired GHCR images exist before starting the backup. GitOps then
creates a SHA-specific deployment PR. The encrypted backup must restore
successfully into disposable PostgreSQL before that PR can be opened. GitOps CI
must pass and protected auto-merge must merge the PR before Argo CD is asked to
sync.

Argo CD's existing automated sync runs the migration Job as a PreSync hook. It
uses the matching API image, which contains the canonical migrations from
MoniKey's `database/` directory. The runner serializes with a PostgreSQL
advisory lock, checks version/checksum history, applies missing migrations in
order, and records each checksum. A failed migration stops Argo CD before the
API or frontend Deployment is updated. Migration 001 is initialization-only and
is never executed by the runner. No SQL is manually copied into this repository.

The migration ConfigMap defaults `MONIKEY_MIGRATION_APPLY` to `false`. A normal
Argo sync can verify a complete, current ledger, but it stops if adoption or a
migration is pending. Only the release workflow sets the flag to `true`, after
the encrypted backup has been restored successfully in disposable PostgreSQL.
After Argo CD reports Synced/Healthy and the API readiness probe succeeds, a
separate SHA-specific PR turns both gates off. That PR also must pass GitOps CI
and protected auto-merge before a final Argo sync is verified. A failed
migration or health check cannot open the reset PR; a failed reset PR leaves the
gate enabled. Rerunning the same release resumes its matching open or merged
PR instead of changing the protected main branch directly.

### One-time adoption of the existing database

Use this activation order:

1. Confirm `HOMELAB_GITOPS_DISPATCH_TOKEN` is absent from MoniKey. Merge MoniKey
   PR #14; its successful CI publishes both SHA images but automatic dispatch
   remains disabled.
2. Configure the GitOps-only `MONIKEY_GITOPS_PROMOTION_TOKEN` and enable
   repository auto-merge as described under Automated release setup. This token
   opens PRs and enables auto-merge; it does not bypass branch protection.
3. Merge GitOps PR #101. The existing Argo auto-sync still sees both migration
   gates disabled, so it will not adopt or apply a migration.
4. From the successful MoniKey main CI run, copy the exact commit SHA and run ID.
   In GitOps **Actions → MoniKey release and initial adoption → Run workflow** on
   `main`, enter both values and set `adopt_existing_schema` to `true`.
5. Wait for the backup job's restore verification to succeed. Then approve the
   protected `monikey-migration-adoption` Environment job. The workflow checks
   the known live schema state in the migration Job, creates a protected
   deployment PR, and waits for CI and auto-merge.
6. Argo CD runs the migration Job as PreSync. It recognizes a complete known
   baseline, records checksums without replaying those migrations, and applies
   only pending migrations. It refuses partial or unknown states and never runs
   `001_init.sql`.
7. Wait for the workflow's Argo Synced/Healthy and API readiness checks, then
   its separate gate-reset PR and final Argo sync. Confirm the summary includes
   backup verification, deployed SHA, migration version, and health results.
8. Only after the successful adoption run, add
   `HOMELAB_GITOPS_DISPATCH_TOKEN` to MoniKey. Future successful main CI runs
   then dispatch automatically.

The initial adoption must use a successful MoniKey main CI run and the required
Environment approval. The GitOps workflow cannot proceed after a failed backup
or before that approval. The actual existing database schema has not been
inspected as part of preparing this workflow; the migration runner recognizes
the schema at run time and fails safely if it is partial or unknown.

The live schema has not been inspected as part of preparing this workflow. The
existing project record reports migrations 002 and 003 deployed and migration
004 pending at the time of that record; the runner validates actual state
before adopting it.

Never delete the PostgreSQL PVC, reset the database, or overwrite financial
records to resolve a migration error. An application image rollback does not
roll back a schema migration.

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

## Automated release setup

Before the one-time adoption, configure:

- GitOps repository secret `MONIKEY_GITOPS_PROMOTION_TOKEN`: a fine-grained
  personal access token limited to `Jeysibn/homelab-gitops`, with
  `Contents: read/write`, `Pull requests: read/write`, and `Checks: read`
  (add `Commit statuses: read` if exposed separately by GitHub). It creates
  temporary branches and PRs, reads their checks, and requests auto-merge. It
  must have no Administration permission and no branch-protection bypass.
  Protect and rotate this token as a repository secret.
- In **Settings → General → Pull Requests**, enable **Allow auto-merge**. Do
  not weaken the main ruleset; its required `Lint & Validate Kubernetes` check
  remains the merge gate.
- Keep the existing backup, Tailscale, and Argo CD secrets here. No database
  credentials are moved to MoniKey.

Do not configure MoniKey's `HOMELAB_GITOPS_DISPATCH_TOKEN` until adoption has
completed successfully. After that one-time secret is added, releases create
protected PRs, wait for required checks, and enable auto-merge automatically.
If GitHub requires a review or disallows auto-merge, the workflow stops safely
without syncing or closing migration gates.

## Rollback

To roll back an application image, restore the previous full SHA tag in
`kubernetes/platform/monikey/manifests/application.yaml` and merge that reviewed
Git change. Argo CD will roll the Deployment back. The database schema and PVC
are not rolled back with the images; take a logical backup before schema changes
and do not run an older API against a schema it cannot read.
