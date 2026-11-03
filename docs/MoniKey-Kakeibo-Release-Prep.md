# MoniKey Kakeibo GitOps Release Preparation

**Status:** planning only; no migrations, image changes, active manifests, or
workflows are authorized by this document.

**Inspected GitOps revision:** `d791038c4b728536100c7d9e8c646aa26f005fac`

**Reviewed MoniKey source revision:** `ccb318d8559ee7e356e562a0e06cf35908360d9f`

**Assessment date:** 2026-11-03

This is repository-based preparation. It does not establish homelab health,
live schema state, actual deployed images, backup recoverability, or private
network reachability. No private cluster, Argo CD, database, or network was
accessed.

## Repository inventory

The MoniKey Argo CD Application is `monikey` in namespace `argocd`. It watches
`https://github.com/jeysibn/homelab-gitops.git`, branch `main`, path
`kubernetes/platform/monikey/manifests`, and deploys to namespace `monikey`.
Its sync policy enables automated sync, prune, self-heal, and namespace
creation. Therefore, merging a change into the watched path can be applied
without a separate sync command.

The app Services are ClusterIP. The desired frontend/backend Deployments each
have one replica and use SHA-derived GHCR tags with startup/readiness/liveness
probes. The desired database is PostgreSQL 17.6 on a one-replica StatefulSet
with a Longhorn PVC and a Retain policy. The checked-in ingress is intended for
the private `monikey.homelab.local` service. This desired configuration does
not prove the running cluster has the same state.

Current GitOps app image references are:

- API: `ghcr.io/jeysibn/monikey-v2-api:sha-e54924985acb220797ba65254d1274f39365cabe`
- Frontend: `ghcr.io/jeysibn/monikey-v2-frontend:sha-e54924985acb220797ba65254d1274f39365cabe`

Current GitOps database bootstrap is ConfigMap `monikey-db-init` in
`kubernetes/platform/monikey/manifests/database.yaml`. Current migration
resources are `transfer-migration.yaml` (002) and
`account-amount-migration.yaml` (003). The backup workflow is manual and
production-connected only when explicitly dispatched with its confirmation
input. Kubernetes CI validates manifests on pull requests to `main` and `dev`.

## Gaps against the reviewed Kakeibo source

| Area | Current GitOps state | Kakeibo release requirement |
| --- | --- | --- |
| Existing database | PreSync hooks 002 and 003 run during Argo synchronization. | Inspect the live schema first; apply only verified missing 004, 005 and 006 after separate backup, restore, and execution approvals. |
| Migration safety | 002 and 003 are idempotent and currently run on sync. | 004–006 contain non-idempotent `ALTER TABLE`/`CREATE` statements; do not add them as automatic PreSync hooks. |
| Fresh install | `monikey-db-init` initializes base tables and seeds categories, but lacks card metadata and Kakeibo fields/tables/state. | A newly empty PGDATA must finish with the same intended schema as MoniKey's current fresh-install sequence through 006. |
| Images | Both app Deployments use the prior `e549249…` SHA-derived tag. | Publish a tested compatible API/frontend pair from an approved source revision and verify its registry digest before GitOps promotion. |
| Recovery | Backup artifact retention is seven days; a temporary dump is restored before encryption. | Durable encrypted retention, retained integrity metadata, and a restore test of the downloaded encrypted artifact are still needed. |

PR #8 merged the reviewed Kakeibo/browser source into the MoniKey integration
branch. Its checks passed, but the MoniKey publishing job was skipped because
the merge did not push to MoniKey `main`. Do not treat CI success or the
integration SHA as proof that corresponding GHCR images exist.

## Future migration design for an existing database

The canonical SQL files are maintained in the MoniKey repository:

1. `database/004_account_card_product.sql` adds nullable `accounts.card_product_id`
   and its format/not-cash constraints. It does not change balances.
2. `database/005_kakeibo.sql` adds nullable category-default and transaction
   envelope fields, a transaction report index, and the monthly plan and weekly
   review metadata tables. Historical transaction envelopes remain NULL. No
   account, transaction, card-payment, or transfer values are reclassified.
3. `database/006_kakeibo_plan_presence.sql` adds nullable `plan_presence` and
   its allowed-value constraint. Existing rows remain NULL (`legacy_unknown`);
   the migration does not guess whether old zero-valued plan rows were saved
   plans or reflection-only records.

Run migrations in numeric order after confirming the existing database already
has the expected 002/003 baseline. For each of 004, 005, and 006, an authorized
read-only catalog inventory must classify the migration as:

- **Missing:** every object introduced by that migration is absent.
- **Applied:** every expected column, table, index, constraint and relevant
  definition is present and matches the canonical SQL.
- **Partial/unknown:** any subset is present, names or definitions differ, or
  the catalog cannot be inspected. Stop; do not run the migration or infer
  completion from a version filename, Argo status, or a previous run log.

Specific catalog signatures to compare against the canonical files include:

- **004:** `accounts.card_product_id`,
  `accounts_card_product_id_format`, and
  `accounts_card_product_id_not_cash`.
- **005:** `categories.default_kakeibo_envelope` and both associated checks;
  `transactions.kakeibo_envelope` and its allowed-value check;
  `idx_transactions_kakeibo_date_envelope`;
  `kakeibo_month_plans` and `kakeibo_weekly_reviews`, their column types,
  primary keys, date/amount/text checks, and the month snapshot JSON validation.
- **006:** `kakeibo_month_plans.plan_presence`, its check constraint, and the
  documented column comment.

These migration files are transactional but are not rerunnable: 004–006 use
plain `ADD COLUMN`, `ADD CONSTRAINT`, `CREATE INDEX`, and `CREATE TABLE` without
`IF NOT EXISTS`. A failed or interrupted operation, unexpected pre-existing
object, or partial signature requires a human investigation and a reviewed
forward-repair plan. Never automatically retry or delete objects to make a
migration pass. Keep the original canonical SQL byte-for-byte aligned with
MoniKey; do not create a second divergent copy or change financial rules.

### Required execution gate (future work only)

Do not add 004–006 as Argo PreSync hooks. The current Application automatically
synchronizes `main`, so a new hook would run as a consequence of merging a
GitOps PR. Instead, design a separate, manually dispatched migration process
with these gates:

1. A human verifies the active cluster/namespace and private DB path, captures
   the current immutable image pair, and records a read-only schema inventory.
2. A verified backup is copied to owner-controlled durable encrypted storage;
   its checksum is retained, and the downloaded encrypted artifact is decrypted
   and restored to a separate disposable environment successfully.
3. A reviewer checks the backup evidence, PostgreSQL version, migration SQL
   checksums, catalog inventory, expected starting state, and recovery plan.
4. A protected GitHub Environment with required human reviewers gates a
   **manual-only** workflow dispatch. The operator selects one migration at a
   time and supplies the reviewed expected schema state and backup evidence
   reference. No push, pull request merge, or Argo sync can trigger it.
5. The runner performs a read-only preflight immediately before execution. It
   applies only when the complete expected prior signature matches and the
   target migration signature is entirely absent. Fully applied means skip;
   partial, unknown, or mismatched state means fail closed without SQL writes.
6. Apply the exact reviewed canonical SQL with stop-on-error behavior. Record
   run ID, source SHA, SQL checksum, operator approvals, pre/post catalog
   signatures, PostgreSQL version and result without printing credentials or
   financial rows. A separate owner approval is required for any application
   rollout.

This describes a future control, not an existing workflow. Do not create,
dispatch, or activate it as part of this documentation PR. If GitHub Environment
approval cannot be guaranteed, keep database execution as a separately
authorized operator action; never substitute automatic Argo synchronization.

Migrations 002 and 003 are already automatic and are not altered by this plan.
Their behavior and the live schema must be understood before an operator
changes any existing migration policy.

## Fresh-install schema alignment

PostgreSQL's `/docker-entrypoint-initdb.d` scripts run only when its data
directory is initialized as an empty database. They are not an upgrade mechanism
for the existing Longhorn volume. Do not delete, replace, detach, or reformat
that PVC to apply schema changes.

MoniKey's current Compose initialization applies, in order:

1. `database/001_init.sql` (five core tables, liquid transfers, indexes and
   signed opening amount support),
2. `database/seed.sql`,
3. `database/004_account_card_product.sql`,
4. `database/005_kakeibo.sql`,
5. `database/006_kakeibo_plan_presence.sql`.

Compose maps the files after 001 to initialization filenames 002–005; those
filenames do not rename or redefine the historical existing-database
migrations. In particular, Compose's `002_seed.sql` is not
`database/002_account_transfers.sql`.

GitOps `monikey-db-init` currently has only a mirrored base schema and seed.
Its base includes transfers but still has the old nonnegative opening amount
checks; the existing 003 hook drops those constraints. It does not include the
card product field or Kakeibo migrations 005/006. For a **future new empty
database** configuration, mirror the same final base, seed and 004/005/006
initialization scripts used by Compose, in that order, and add a CI comparison
against canonical source files. Preserve the current init script names in
comments/mounts so operators can distinguish initialization order from
historical migration numbers.

There is also a first-install ordering concern: current 002/003 jobs are
PreSync hooks, while the PostgreSQL StatefulSet is an ordinary Sync resource.
On a genuinely new cluster with no existing PostgreSQL pod, a PreSync job can
wait for a database that Argo has not created yet, then time out before the
StatefulSet is applied. The current existing deployment may already have a
database, but this repository review cannot establish that runtime fact. A
future clean-install design must stage database creation/readiness separately
from existing-database migrations and application rollout, or use a reviewed
Argo application dependency/order that makes the database ready first. Do not
assume the current migration hooks also solve first-install bootstrap.

Keep the three operations separate:

| Operation | Purpose | Must not do |
| --- | --- | --- |
| Empty-database initialization | Establish the final schema and seed categories on a genuinely new empty PGDATA. | Be used to upgrade or replace the current persistent volume. |
| Existing-database migration | Explicitly apply only a catalog-confirmed missing migration after recovery gates. | Re-run 001, infer status, or change ledger rows. |
| Application rollout | Promote a reviewed and available API/frontend image pair after schema and API compatibility are approved. | Implicitly execute unapproved migrations. |

Changing the ConfigMap does not execute init SQL on an existing initialized
volume. Still, any future ConfigMap update must be reviewed with the PVC retained
and a fresh-install rehearsal. This PR changes no ConfigMap or StatefulSet.

## Image build and promotion plan

MoniKey `.github/workflows/ci.yml` runs frontend, database/API, and container
build checks on pull requests and on pushes to MoniKey `main`. Its `publish`
job runs only after those checks pass on a push to `main`; it builds both images
and pushes:

- `ghcr.io/jeysibn/monikey-v2-api:sha-<main-push-commit-sha>`
- `ghcr.io/jeysibn/monikey-v2-frontend:sha-<main-push-commit-sha>`

Future release sequence:

1. Review and integrate the Kakeibo source into MoniKey's release branch and
   then `main` through the normal approved PR process. Record the exact commit
   that triggers the main-branch workflow; do not assume it is
   `ccb318d8559ee7e356e562a0e06cf35908360d9f` because a merge commit may have
   another SHA.
2. Confirm the full `checks` job passed for that main commit and that the
   `publish` job completed. A skipped publish job produces no release evidence.
3. Verify both package tags exist through GHCR metadata or `docker manifest
   inspect`; record the OCI manifest digest for each image and verify both
   architectures/platforms required by the cluster. Tag presence alone is not
   proof that the pair is the reviewed build.
4. Prefer pinning the GitOps image reference to both the SHA tag and the
   verified digest (for example `image: repository:sha-<commit>@sha256:<digest>`).
   SHA-derived tags are immutable by convention; registry tag overwrite is a
   separate setting. Verify or enforce immutable-tag policy where available.
5. Update API and frontend references together in a reviewed GitOps PR only
   after the migration plan and compatibility pair are approved. Keep the
   current full-SHA image pair and their registry digests available for
   application rollback. Do not move or delete old tags during release.

No image is built, published, promoted or deployed by this PR. The current
`e549249…` pair remains unchanged in active manifests.

## Backup and recovery improvements

The current `.github/workflows/monikey-db-backup.yml` is manual-only and
requires a confirmation input on `main`. It reads `postgres-0` through a
namespace-scoped Kubernetes credential, runs `pg_dump`, restores the plaintext
dump into a disposable PostgreSQL container, encrypts it with GPG AES-256, and
uploads only the encrypted dump as a seven-day GitHub artifact. A SHA-256 is
computed in the runner but the checksum file is not uploaded. The workflow
does not download, decrypt and restore the uploaded encrypted artifact.

Before relying on this for a Kakeibo release, propose a separate reviewed backup
improvement that:

- Retains an encrypted copy in owner-controlled storage outside the cluster
  and outside GitHub's seven-day artifact lifetime, with documented retention,
  access control, lifecycle and recovery ownership.
- Uploads a checksum manifest next to each encrypted object and verifies it
  after transfer. Capture the artifact identifier, digest, source run, source
  database PostgreSQL version and completion result without logging database
  rows or secrets.
- Downloads that encrypted artifact into an isolated restore runner, verifies
  the checksum, decrypts it using a protected secret, checks `pg_restore --list`,
  and restores it into a separate disposable database with the matching
  PostgreSQL major version. Do not restore into the production PVC.
- Produces a redacted evidence record for restore success, schema/object
  validation, cleanup and elapsed time. Do not print row contents, credentials,
  passphrases, or connection URLs.
- Uses short-lived identity where the infrastructure supports it; otherwise
  narrowly scopes and rotates the kubeconfig/service-account token, Tailscale
  credential, and encryption passphrase. Keep secrets in protected GitHub
  environments or an approved secret manager. Never put backup contents or
  secret values in Git.
- Remains manual and protected; do not add a scheduled workflow that silently
  connects to production as part of this preparation.

The October 31, 2026 artifact was reported to expire on November 7, 2026. This
repository-only review did not trigger the workflow, access its artifact, or
verify production backup recoverability. The seven-day artifact is temporary
transport, not a durable recovery plan.

## Future approval-gated release order

1. Restore homelab access. Confirm the intended context and namespace, Argo
   Application health/sync, private ingress, deployed image pair, and PVC
   identity through approved read-only checks.
2. Verify an owner-controlled encrypted backup, retained checksum, and
   successful download/decrypt/restore test in a separate disposable
   environment. Record evidence and the PostgreSQL version.
3. Obtain a read-only catalog inventory for 002–006. Classify each as applied,
   missing, partial or unknown; stop on partial/unknown. Do not inspect or
   export financial rows for this inventory.
4. Approve the exact migration sequence and execution method. Apply only
   catalog-confirmed missing migrations, one at a time, after explicit
   migration approval. Keep Argo's automatic sync from being the migration
   trigger.
5. Verify the candidate API/frontend tags and digests and approve a compatible
   pair. Promote only through a separate reviewed GitOps change.
6. Run health and application smoke checks, then compare account balances,
   card debt, and ledger counts around Kakeibo metadata operations without
   exposing private financial data. Record results and operator/time.

### Rollback

Application rollback means restoring the previous reviewed API/frontend SHA
and digest pair through a reviewed GitOps change while leaving additive database
schema in place. Do not drop schema objects during an application rollback.
First verify the older API can run against the expanded schema. If the API is
healthy but the frontend is failing, roll back the compatible application pair
as planned. If accounting positions differ, stop financial writes and
investigate before any further rollout.

Database recovery is separate and higher risk. Stop writes, preserve the
pre-release backup, and follow an explicitly approved recovery/cutover plan only
after a verified restore. Restoring a pre-release copy can lose subsequent
writes; identify and reconcile them before any recovery. Never use PVC deletion,
`001_init.sql`, or improvised reverse SQL as a rollback.

## Deferred until homelab access returns

- Verify actual Kubernetes context, Argo sync/health, deployment image IDs,
  ingress reachability, and PVC state.
- Inspect live schema catalogs and establish the exact migration starting
  point without querying financial rows.
- Establish durable backup retention and verify the encrypted-artifact
  recovery path with an authorized operator.
- Approve and implement any future migration gate, fresh-install ConfigMap
  update, image promotion, and deployment procedure as separate reviewed work.
- Perform the release smoke tests and outstanding real-device/browser and
  screen-reader QA in their approved environments.

## Checks and limits

This document is based on the inspected GitHub source and repository history.
Repository checks can validate the GitOps files and SQL copies; they cannot
prove runtime health, production schema state, network privacy, image pulls in
the cluster, backup recoverability, or the correctness of a future migration
against the live database. Those require the separately authorized operational
checks above.
