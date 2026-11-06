# MoniKey read-only schema inspection

**Status: prepared but fail-closed. No live workflow run, database connection, role provisioning, or Kubernetes change is authorized by this document.**

## Safety boundary

`.github/workflows/monikey-schema-inspection.yml` is manually dispatched from GitOps `main` only. The operator must check the confirmation input. A no-secret preflight verifies that the GitHub Environment `monikey-production-inspection` exists, requires the repository owner (`Jeysibn`) as a reviewer, has `prevent_self_review` set to `false`, and allows exactly the `main` branch. This single-operator model lets the owner initiate and approve the inspection job. Configure those protections; this repository does not assume they already exist. The check uses GitHub's read-only environment and deployment-branch-policy endpoints and fails closed when a required rule is absent or differs.

Owner self-approval is an operational gate, **not independent review**. It authorizes only the catalog-only inspection workflow. A successful approval or an APPLIED/MISSING result does not approve any migration or deployment. Those operations still require a separate explicit owner approval after recovery evidence, schema results, and the exact change have been reviewed.

The workflow has fixed targets: context and API server are configured as environment variables and compared exactly; namespace `monikey`, pod `postgres-0`, PostgreSQL database `monikey_dev`, and login `monikey_schema_inspector` are fixed in code. It checks the pod name, namespace, label, running state, and PostgreSQL 17 image family. It accepts no SQL, database URL, host, namespace, pod, or command from dispatch input. Time limits are 12 minutes for the inspection job, 15 seconds per SQL statement, and 30 seconds to establish the tunnel. The tunnel and temporary credentials are removed in an always-run cleanup step.

The schema inspection workflow uses only the dedicated inspector Kubernetes token and PostgreSQL login. It never uses the backup kubeconfig or application role to inspect the database. The separate, owner-approved credential provisioning workflow temporarily uses the existing backup kubeconfig to invoke a fixed PostgreSQL role-provisioning script and request a short-lived token; it uses `monikey_app` only as a bootstrap administrator after checking that the database session is superuser. This bootstrap path is not the inspection identity. Tailscale connectivity uses the existing Tailscale OAuth secrets.

## Kubernetes access and safe Argo CD path

The dedicated child Application is `monikey-inspection-access`. The normal `monikey` Application watches `kubernetes/platform/monikey/manifests` and automatically syncs it; that directory contains database PreSync hooks 002 and 003. The inspection RBAC is in the separate path `kubernetes/platform/monikey/inspection-access/manifests`. The root Application may automatically register the child Application after this change is merged, but the child has `syncPolicy: {}` and cannot automatically apply its resources. A separate explicit Argo CD sync is required. Use the existing **Argo CD Sync** workflow with application `monikey-inspection-access` and `prune=false` only after reviewing the Application status. This child path contains no database migration Jobs or app image changes.

The inspector ServiceAccount has a namespaced Role limited to `get pods/postgres-0` and `create pods/portforward` for `postgres-0`. It has no `pods/exec`, Secret, workload, Role, or cluster-wide permissions. A separate RoleBinding grants the existing `monikey-backup` ServiceAccount only `create` on `serviceaccounts/token` named `monikey-schema-inspector`, so the protected provisioning workflow can request a short-lived inspector token. The backup identity cannot create RBAC or tokens for another service account. Port-forward permission opens a network tunnel; PostgreSQL privileges remain the data boundary.

### Dedicated Argo CD audit token

For the confirmed TLS verification failure, see [MoniKey Argo CD TLS inspection and trust](MoniKey-ArgoCD-TLS.md). It describes the protected certificate-metadata diagnostic and the independent verification required before configuring a CA bundle. The bootstrap supports that approved bundle through Argo CD CLI `--server-crt`; it never disables certificate verification.

The GitOps repository installs the upstream Argo CD v3.5.1 manifests and creates the root Application, but it does not define `argocd-cm`, `argocd-rbac-cm`, or an Argo RBAC policy. The live `policy.default`, project-role configuration, and existing token identities are therefore **unknown from this repository**. Do not reuse `ARGOCD_AUTH_TOKEN` from the Argo CD Sync workflow and do not change or rotate it for inspection.

Prefer an AppProject role token because it is scoped to the `default` project. The manual, approval-gated **Bootstrap MoniKey read-only Argo CD token** workflow is prepared to inspect the live `default` AppProject and check the existing `ARGOCD_AUTH_TOKEN` with the read-only `argocd account can-i` operation before it changes anything. It stops before writes unless that credential can read and update the fixed AppProject. A missing `update projects default` permission is reported as the exact blocker; the workflow never tries another credential or escalates access.

The repository does not define the live global `policy.default` or `argocd-rbac-cm`. The workflows therefore require a fresh, owner-captured evidence bundle in the protected `monikey-inspection-provisioning` Environment variable `ARGOCD_RBAC_EVIDENCE_B64`. Capture it from the approved Kubernetes context with `capture_argocd_rbac_evidence.py`; the script checks the context and API server, reads only the default AppProject and `argocd-rbac-cm`, and omits unrelated roles and token status. The snapshot is accepted for 15 minutes. It contains public RBAC configuration, not credentials. Without a fresh valid snapshot, bootstrap stops before role or token writes and audit stops before inspecting the Application. The audit also requires the snapshot's exact reader role and policy; bootstrap validates the post-write AppProject directly before candidate-token verification.

The approved role policy is the same as the documented [project-role commands](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/projects/):

```bash
argocd proj role create default monikey-inspection-reader
argocd proj role add-policy default monikey-inspection-reader \
  --action get --permission allow --object monikey-inspection-access
argocd proj role create-token default monikey-inspection-reader --expires-in 1h
```

The bootstrap workflow uses the existing `ARGOCD_AUTH_TOKEN` for read-only preflight and, only after `can-i` confirms project-update permission, the fixed project-role operation. It uses the protected Environment's existing `MONIKEY_ENVIRONMENT_SECRET_WRITER_TOKEN` to store the verified token as `ARGOCD_READONLY_AUTH_TOKEN`. The token value is captured in a mode-0600 runner-temporary file, masked, piped to `gh secret set`, and deleted by cleanup; it is not printed or uploaded. The expected token identity is `proj:default:monikey-inspection-reader`; the existing audit permission checker rejects any different identity, including the sync token's identity.

Argo CD grants every authenticated token at least the permissions in `policy.default`, and a default allow cannot be removed by a user-specific deny ([RBAC evaluation rules](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/rbac/)). The built-in `role:readonly` grants `get` on all Applications (`*/*`), so it is unsafe for this one-Application inspection token and is rejected; custom default roles are also rejected. Static validation permits only `get applications default/monikey-inspection-access allow` for the exact `proj:default:monikey-inspection-reader` identity. It rejects all other allow grants, including project and other resource reads, wildcard subjects or objects, mutations, and permission inheritance into this identity. The live probes require that named Application read and deny broad Application reads, `get projects *`, supported Application writes, sync, override, and custom action invocation, plus broad writes across Applications, ApplicationSets, clusters, projects, repositories and write-repositories, certificates, GPG keys, accounts, exec, and extensions. Argo CD v3.5.1 implements Application rollback through the same `ActionSync` authorization check, so denying `sync` also denies rollback. The API cannot query fine-grained `update/*`, `delete/*`, or `action/*` actions; static validation scans every captured policy row and rejects all positive grants outside the exact allowlist. Any unexpected grant, malformed response, evidence gap, identity mismatch, or CLI error blocks verification and prevents token storage.

Capture preserves `policy.csv` and every key selected by Argo CD v3.5.1: keys with prefix `policy.` and suffix `.csv`, except the exact key `policy.csv`. This includes `policy..csv`; fragment-name segments do not need to be nonempty. Other ConfigMap keys and non-string values fail closed. Verification composes optional `policy.csv` first, then selected fragments in lexicographic key order. No selected fragment is discarded. Capture and verification report AppProject failures as `APP_PROJECT_VALIDATION`, ConfigMap structure failures as `RBAC_CONFIGMAP_VALIDATION`, composed policy failures as `RBAC_POLICY_VALIDATION` or `RBAC_POLICY_UNSAFE`, evidence-envelope failures as `RBAC_EVIDENCE_*`, and live permission-probe failures with their existing numbered probe categories.

Every composed record is checked. Malformed CSV, unknown effects/actions/resources, wildcard group mappings, inheritance into built-in `role:admin` or `role:readonly`, cycles, and mappings that let another identity inherit the inspection identity are rejected. Any global positive permission grant outside the exact identity/resource/action/object allowlist is rejected across all fragments. This can block unrelated automation policies: the verifier does not assume they are irrelevant or approve fragments based on their filenames. Supported live `account can-i` checks remain required after static evidence validation; no token is approved from policy evidence alone. An unsafe live fragment requires separate owner review, not automatic policy changes.

The captured evidence records its Kubernetes context and API server. The workflows compare those fields with the protected Environment variables `ARGOCD_KUBE_CONTEXT` and `ARGOCD_KUBE_API_SERVER`, so a snapshot from another cluster is rejected. For the current homelab, the approved values are context `default` and API server `https://127.0.0.1:6443`. The K3s kubeconfig is root-owned on the homelab host, so capture must run with the existing authorized `sudo` access there:

```bash
set -euo pipefail
# Create the directory as the same privileged user that runs capture.
evidence_dir="$(sudo -n mktemp -d /tmp/argocd-rbac-evidence.XXXXXXXX)"
trap 'sudo -n rm -f -- "$evidence_dir/evidence.json"; sudo -n rmdir -- "$evidence_dir"' EXIT
sudo -n python3 scripts/monikey-schema-inspect/capture_argocd_rbac_evidence.py \
  --context default \
  --api-server https://127.0.0.1:6443 \
  --output "$evidence_dir/evidence.json"
sudo -n python3 scripts/monikey-schema-inspect/verify_argocd_permissions.py \
  --rbac-evidence-file "$evidence_dir/evidence.json" --check-rbac-evidence \
  --expected-kubernetes-context default \
  --expected-kubernetes-api-server https://127.0.0.1:6443
# Only during an explicitly authorized evidence-update operation:
sudo -n base64 -w0 "$evidence_dir/evidence.json" | \
  gh variable set ARGOCD_RBAC_EVIDENCE_B64 --repo Jeysibn/homelab-gitops \
    --env monikey-inspection-provisioning
```

The directory must be private (0700), owned by the capture process user, and not a symlink. Capture creates a new mode-0600 file exclusively and refuses existing paths; failed partial files are removed. Do not pre-create the output file as another user or write directly into shared `/tmp`. Diagnostic messages identify only the failed stage.

Set `ARGOCD_KUBE_CONTEXT=default`, `ARGOCD_KUBE_API_SERVER=https://127.0.0.1:6443`, and the encoded snapshot in the protected Environment variables `ARGOCD_RBAC_EVIDENCE_B64`, then start the intended workflow within 15 minutes. Refresh the snapshot before each run. These variables are not secrets; do not put credentials or Secret objects in the evidence file.

The project-role create/update operations are administrative changes and are not a migration or deployment approval. Owner self-approval in the GitHub Environment is not independent review. A separate explicit owner approval is still required for any schema migration, image promotion, Application sync, or deployment.

The audit workflow itself does not provision credentials or change RBAC. The separate bootstrap workflow can add only the named project role and store its one-hour token after protected Environment approval and successful effective-permission validation. This administrative change is separate from the backup identity, PostgreSQL inspector login, Kubernetes inspector token, Argo CD Sync token, database migrations, and application deployment.

Argo CD v3.5.1's [`ApplicationStatus.ResourceStatus` type](https://github.com/argoproj/argo-cd/blob/v3.5.1/pkg/apis/application/v1alpha1/types.go#L2063-L2088) marks per-resource `status` and `health` as optional JSON fields. Resource health can also be reported through the Application resource tree rather than inline status, depending on the Argo CD configuration. The audit therefore does not assume that ServiceAccount, Role, and RoleBinding entries always include those fields: missing or unknown Application/resource status, missing health, health other than `Healthy`/expected `Missing`, or a mismatch between sync status and the read-only diff blocks a `SAFE TO SYNC` verdict. CLI command forms are checked against the pinned v3.5.1 help; the live controller output is still unknown until an approved audit.

## PostgreSQL TEMP privilege behavior

PostgreSQL grants `CONNECT` and `TEMPORARY` to `PUBLIC` by default. Revoking `TEMPORARY` from `PUBLIC` could alter behavior for existing database users, so this provisioning design does not do that. The role-creation script gives the inspector no direct `TEMP` privilege and rejects an existing role with direct `TEMP`, `CREATE`, memberships, ownership, or financial-table privileges. The inventory records effective `TEMP` separately from a direct grant and accepts only the `PUBLIC`-inherited case with no role memberships. The fixed inspection SQL starts a `READ ONLY` transaction; PostgreSQL 17 disallows all `CREATE` commands in that transaction, including temporary-table creation. The disposable PostgreSQL test retains the default `PUBLIC` ACL, confirms the effective privilege is inherited rather than direct, and verifies that `CREATE TEMP TABLE` fails in the read-only transaction. No live ACL was inspected or changed.

If a deployed database has an explicit TEMP grant to the inspector or unexpected role memberships/ACLs, provisioning fails closed. Do not revoke TEMP from `PUBLIC` automatically. Review the ACLs and choose a separate, owner-approved change that preserves existing application permissions.

## Credential provisioning procedure (prepared, not run)

1. Configure a protected GitHub Environment named `monikey-inspection-provisioning`: required reviewer `Jeysibn`, `prevent_self_review: false`, custom branch policy allowing exactly `main`. Add variables `MONIKEY_SCHEMA_EXPECTED_CONTEXT` and `MONIKEY_SCHEMA_EXPECTED_API_SERVER`.
2. Add Environment secret `MONIKEY_ENVIRONMENT_SECRET_WRITER_TOKEN`, a fine-grained token limited to this repository with **Environments: write**. The workflow needs it because the `GITHUB_TOKEN` is not granted environment-secret write access by the workflow's read-only permissions. Keep this token only in the protected provisioning Environment. It can change Environment secrets, so review the workflow and its `main`-only gate before configuring it.
3. After the child Argo Application is registered, explicitly sync only `monikey-inspection-access` with pruning disabled. The existing backup ServiceAccount already has documented `get pods` and `get/create pods/exec`; this PR adds only the narrow TokenRequest rule described above. The provisioning workflow checks all three required permissions before proceeding.
4. From GitOps `main`, manually dispatch **Prepare MoniKey schema-inspection credentials**, check the confirmation input, and approve its Environment job. It creates a 32-byte random password, applies `scripts/monikey-schema-inspect/provision_role.sql` through the fixed `postgres-0` exec path, obtains a one-hour-requested ServiceAccount token, builds a kubeconfig containing only that token, then writes both values directly to the protected `monikey-production-inspection` Environment using `gh secret set`. Secrets are never printed or uploaded. PostgreSQL role configuration requests read-only transactions and 15-second statement/30-second idle-transaction timeouts. The workflow refuses an unexpected target, non-superuser bootstrap connection, or unsafe pre-existing role.
5. Configure the existing protected `monikey-production-inspection` Environment with `Jeysibn` as reviewer, self-approval enabled, exactly `main` allowed, and the existing expected-context/API-server variables. Then promptly dispatch the **MoniKey read-only schema inspection** workflow. The requested token lifetime is one hour; the API server may enforce a shorter maximum. If the token expires, rerun credential provisioning to rotate the safe role password and mint a new token.

The existing backup credential does not have `serviceaccounts/token` permission in its documented Role; this PR adds that exact permission only when the separate child Application is explicitly synced. The existing `argocd-sync` workflow's Argo token is not assumed to have access to this new Application; confirm it can sync only the named child before use. No GitHub Environment, secret, Kubernetes identity, PostgreSQL role, or Argo resource has been provisioned by this PR.

Environment-secret writes are documented as requiring Environments: write. The provisioning job uses a separately configured fine-grained token because `GITHUB_TOKEN` remains limited to `contents: read` and `actions: read`.

## Manual dispatch and result

After all provisioning requirements are approved, the operator selects GitOps `main`, checks `confirm_schema_inspection`, starts the run, and approves the inspection job as the Environment reviewer. This approval permits only the read-only inventory. It is not an independent review and does not authorize SQL migrations, image promotion, or deployment; each requires a separate explicit approval. No Argo CD command, sync, migration, or write statement is invoked by this workflow. The SQL begins a read-only transaction, sets a 15-second statement timeout, and inspects `pg_catalog` objects (`pg_class`, `pg_attribute`, `pg_constraint`, `pg_index`, role metadata, and metadata functions). It verifies the dedicated role's ACLs and ownership flags using metadata functions; it never selects application rows. The output includes fixed target identifiers, PostgreSQL version, run ID/time/source commit, exact reviewed schema-signature hashes, classifications, and mismatched object names only. A JSON report is retained as a short-lived Actions artifact (7 days); it contains no finance rows or credentials.

Each migration is classified individually:

- **APPLIED:** all required objects are present and match the reviewed schema signature.
- **MISSING:** all objects introduced by that migration are absent and the expected prior schema is established.
- **PARTIAL:** a subset is present or an object is incomplete/mismatched.
- **UNKNOWN:** catalog visibility, prior schema, target identity, or definitions cannot be established.

`PARTIAL` or `UNKNOWN` blocks migration planning and requires a human-reviewed investigation. `MISSING` is an inventory result, never permission to apply SQL. A report with all migrations classified is still not migration authorization.

## Offline validation

Run the unit fixtures with `python3 -m unittest discover -s scripts/monikey-schema-inspect/tests -p 'test_*.py'`. To rehearse PostgreSQL catalog visibility and the exact signatures, use a local disposable Docker PostgreSQL 17.6 container and a MoniKey checkout containing canonical migrations: `MONIKEY_SOURCE_DIR=/path/to/monikey sg docker -c 'bash scripts/monikey-schema-inspect/test-postgres.sh'`. The test retains PostgreSQL’s default PUBLIC TEMP privilege, creates a synthetic login, applies canonical migrations, confirms the role has no direct TEMP or table privileges, and verifies READ ONLY rejects CREATE TEMP TABLE. It removes the container on exit. It does not connect to any homelab network.

## Stop conditions and remaining approvals

The inspection workflow deliberately fails before the network path if its Environment owner-review rule is absent or configured to prevent owner self-approval, the selected ref is not GitOps `main`, confirmation is false, dedicated secrets or expected cluster identity are missing, pod identity differs, or required permissions are absent. The credential-provisioning workflow is separate, manual, main-only, explicitly confirmed, and uses the additional protected provisioning Environment. SQL target mismatch or any query error also fails closed. The inspection job has no fallback to bootstrap credentials.

The inspected schema-signature SHA-256 values were derived from the exact canonical MoniKey migrations on `origin/feat/card-artwork-discovery` commit `d490ffddd3e5681b5db21f5d524e14b1251acc95`, applied to a disposable PostgreSQL 17.6 fixture. The hashes for 002, 004, 005, and 006 include complete relevant columns, constraint definitions, indexes, and the 006 column comment. Migration 003 is checked by the exact legacy named checks, expected base account constraint, and absence of unreviewed constraints involving signed opening amounts. PostgreSQL 17 major-version output changes or MoniKey SQL changes require regenerating and reviewing these signatures before using the workflow.

Still requiring owner approval: create and test the dedicated identities, configure environment protection and secrets, review the SQL/report signatures against canonical MoniKey migrations 002–006, approve an actual read-only run, review its report, and separately approve any later live migration or deployment. Environment self-approval is not independent review. No schema status is claimed here.
