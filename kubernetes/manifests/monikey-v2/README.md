# MoniKey V2 GitOps deployment

This application is deployed from validated immutable image digests published by the MoniKey V2 `main` release workflow.

Runtime credentials are intentionally not stored in this repository. Before Argo syncs the application, create these Secrets in namespace `monikey-v2`:

- `monikey-v2-runtime-secrets`: `POSTGRES_PASSWORD`, `DATABASE_URL`
- `monikey-v2-postgres-client`: `host`, `database`, `username`, `password`

The live Argo CD installation does not have a verified SOPS/age decryption plugin, so do not commit plaintext or unverified encrypted secret manifests. Provision the credentials through the authorized cluster secret workflow before merging this deployment change.

The V1 MoniKey deployment and namespace were retired before this application was introduced. The ingress at `monikey.homelab.local` is therefore managed by this application.
