# MoniKey read-only schema inspection

**Status: prepared but fail-closed. No live workflow run, database connection, role provisioning, or Kubernetes change is authorized by this document.**

## Safety boundary

`.github/workflows/monikey-schema-inspection.yml` is manually dispatched from GitOps `main` only. The operator must check the confirmation input. A no-secret preflight verifies that the GitHub Environment `monikey-production-inspection` exists, requires the repository owner (`Jeysibn`) as a reviewer, has `prevent_self_review` set to `false`, and allows exactly the `main` branch. This single-operator model lets the owner initiate and approve the inspection job. Configure those protections; this repository does not assume they already exist. The check uses GitHub's read-only environment and deployment-branch-policy endpoints and fails closed when a required rule is absent or differs.

Owner self-approval is an operational gate, **not independent review**. It authorizes only the catalog-only inspection workflow. A successful approval or an APPLIED/MISSING result does not approve any migration or deployment. Those operations still require a separate explicit owner approval after recovery evidence, schema results, and the exact change have been reviewed.

The workflow has fixed targets: context and API server are configured as environment variables and compared exactly; namespace `monikey`, pod `postgres-0`, PostgreSQL database `monikey_dev`, and login `monikey_schema_inspector` are fixed in code. It checks the pod name, namespace, label, running state, and PostgreSQL 17 image family. It accepts no SQL, database URL, host, namespace, pod, or command from dispatch input. Time limits are 12 minutes for the inspection job, 15 seconds per SQL statement, and 30 seconds to establish the tunnel. The tunnel and temporary credentials are removed in an always-run cleanup step.

The workflow asks for a catalog-only PostgreSQL login and a dedicated Kubernetes identity. It must not reuse `MONIKEY_BACKUP_KUBECONFIG_B64`, the application role, or the backup ServiceAccount. The existing backup workflow's `pods/exec` access is not suitable for schema inspection. Tailscale connectivity uses the existing Tailscale OAuth secrets, consistent with the repository's private connectivity convention.

## Identity provisioning procedure (not performed)

Provision only after separate owner approval and an independently reviewed change. Never put these resources under `kubernetes/platform/monikey/manifests`, which Argo CD watches.

1. Create a dedicated namespace-scoped ServiceAccount and RoleBinding in namespace `monikey`, with no token automount where compatible with the chosen kubeconfig credential flow.
2. Permit `get` on `pods` scoped to `postgres-0`, and `create` on `pods/portforward` scoped to `postgres-0`. Do not grant `pods/exec`, Secret reads, pod creation, workload updates, or cluster-wide access. Verify actual authorization with `kubectl auth can-i` using the resulting identity before any workflow run.
3. `pods/portforward` authorization permits a network tunnel to the pod. It is not a database read-only control and can bypass network policy boundaries. The PostgreSQL role is the data access boundary.
4. Create a PostgreSQL login `monikey_schema_inspector` with `LOGIN`, `NOINHERIT`, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`, no memberships, no ownership, and no role administration. Grant only `CONNECT` to `monikey_dev`. Do not grant `pg_read_all_data`, table/sequence privileges, DML, DDL, or ownership. The SQL also verifies database `CREATE`/`TEMP`, schema `CREATE`, table/sequence ACLs, and ownership. PostgreSQL commonly grants database TEMP to `PUBLIC`; if that makes this dedicated login fail the check, stop. Revoking TEMP from `PUBLIC` is a database-wide ACL change that could affect other users and requires a separate impact review and owner approval. Do not run the inspection until the login is proven to have no create capability under the accepted policy. PostgreSQL exposes the required schema catalogs and metadata functions to ordinary connected roles; verify this against the exact deployed PostgreSQL major version in a disposable environment first. If the query cannot see required catalog metadata with default catalog visibility, stop and request a reviewed minimum-privilege solution; do not broaden access silently.
5. Store the kubeconfig and database password as dedicated secrets in the protected Environment: `MONIKEY_SCHEMA_INSPECTION_KUBECONFIG_B64` and `MONIKEY_SCHEMA_INSPECTION_DB_PASSWORD`. Configure Environment variables `MONIKEY_SCHEMA_EXPECTED_CONTEXT` and `MONIKEY_SCHEMA_EXPECTED_API_SERVER`. Configure `Jeysibn` as the required reviewer, disable “Prevent self-review” (`prevent_self_review: false`), and allow exactly `main`. This enables the single operator to approve the read-only inspection, but is not an independent review or migration/deployment approval. Do not copy values into repository secrets or logs.

An illustrative PostgreSQL role command may be prepared by the database owner, but must be reviewed against the deployed role policy and executed only through a separately approved provisioning procedure. No role command is included here because it would change production.

## Manual dispatch and result

After all provisioning requirements are approved, the operator selects GitOps `main`, checks `confirm_schema_inspection`, starts the run, and approves the inspection job as the Environment reviewer. This approval permits only the read-only inventory. It is not an independent review and does not authorize SQL migrations, image promotion, or deployment; each requires a separate explicit approval. No Argo CD command, sync, migration, or write statement is invoked by this workflow. The SQL begins a read-only transaction, sets a 15-second statement timeout, and inspects `pg_catalog` objects (`pg_class`, `pg_attribute`, `pg_constraint`, `pg_index`, role metadata, and metadata functions). It verifies the dedicated role's ACLs and ownership flags using metadata functions; it never selects application rows. The output includes fixed target identifiers, PostgreSQL version, run ID/time/source commit, exact reviewed schema-signature hashes, classifications, and mismatched object names only. A JSON report is retained as a short-lived Actions artifact (7 days); it contains no finance rows or credentials.

Each migration is classified individually:

- **APPLIED:** all required objects are present and match the reviewed schema signature.
- **MISSING:** all objects introduced by that migration are absent and the expected prior schema is established.
- **PARTIAL:** a subset is present or an object is incomplete/mismatched.
- **UNKNOWN:** catalog visibility, prior schema, target identity, or definitions cannot be established.

`PARTIAL` or `UNKNOWN` blocks migration planning and requires a human-reviewed investigation. `MISSING` is an inventory result, never permission to apply SQL. A report with all migrations classified is still not migration authorization.

## Offline validation

Run the unit fixtures with `python3 -m unittest discover -s scripts/monikey-schema-inspect/tests -p 'test_*.py'`. To rehearse PostgreSQL catalog visibility and the exact signatures, use a local disposable Docker PostgreSQL 17.6 container and a MoniKey checkout containing canonical migrations: `MONIKEY_SOURCE_DIR=/path/to/monikey sg docker -c 'bash scripts/monikey-schema-inspect/test-postgres.sh'`. The test revokes database TEMP only inside its disposable fixture, creates a synthetic login, applies canonical migrations, and removes the container on exit. It does not connect to any homelab network.

## Stop conditions and remaining approvals

The workflow deliberately fails before the network path if its Environment owner-review rule is absent or configured to prevent owner self-approval, the selected ref is not GitOps `main`, confirmation is false, dedicated secrets or expected cluster identity are missing, pod identity differs, or required permissions are absent. SQL target mismatch or any query error also fails closed. No fallback to the backup credentials exists.

The inspected schema-signature SHA-256 values were derived from the exact canonical MoniKey migrations on `origin/feat/card-artwork-discovery` commit `d490ffddd3e5681b5db21f5d524e14b1251acc95`, applied to a disposable PostgreSQL 17.6 fixture. The hashes for 002, 004, 005, and 006 include complete relevant columns, constraint definitions, indexes, and the 006 column comment. Migration 003 is checked by the exact legacy named checks, expected base account constraint, and absence of unreviewed constraints involving signed opening amounts. PostgreSQL 17 major-version output changes or MoniKey SQL changes require regenerating and reviewing these signatures before using the workflow.

Still requiring owner approval: create and test the dedicated identities, configure environment protection and secrets, review the SQL/report signatures against canonical MoniKey migrations 002–006, approve an actual read-only run, review its report, and separately approve any later live migration or deployment. Environment self-approval is not independent review. No schema status is claimed here.
