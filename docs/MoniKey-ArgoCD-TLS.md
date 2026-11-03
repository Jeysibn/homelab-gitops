# MoniKey Argo CD TLS inspection and trust

## What the repository establishes

The GitOps repository defines a self-signed cert-manager `ClusterIssuer` used by the MoniKey ingress. It does not contain an Argo CD ingress or certificate manifest; Argo CD is installed from the upstream manifest by `kubernetes/bootstrap/install-argocd.sh`. The MoniKey ingress certificate does not establish which certificate the Argo CD endpoint presents.

The failed bootstrap diagnostic confirms certificate verification failed, but its temporary OpenSSL/CLI details were removed after the run. The current repository evidence cannot determine whether the Argo CD endpoint certificate is self-signed, issued by a private CA, missing intermediates, expired, or mismatched to the configured server name. The new diagnostic reports those certificate properties without printing the private endpoint, subject/issuer names, or SAN values.

## Run the read-only diagnostic

After reviewing and merging the diagnostic workflow, an owner can dispatch `MoniKey Argo CD TLS certificate diagnostic` from `main`, set the explicit confirmation input to `true`, and approve the protected `monikey-inspection-provisioning` Environment. It uses the fixed `ARGOCD_SERVER` and existing Tailscale connection, but does not read or use `ARGOCD_AUTH_TOKEN`. It only makes a TLS handshake and reads certificate metadata. It does not sync or alter Argo CD.

The report contains the presented chain count, leaf SHA-256 fingerprint, validity state, hostname match result, self-signature indication, SAN type counts, and runner trust result. It withholds endpoint addresses, certificate subject/issuer strings, SAN values, response bodies, credentials, and private keys. A trust failure caused by an unknown issuer may mean a private CA or a missing intermediate; compare the presented chain links with the expected chain from an independent source.

## Independently verify the expected certificate or CA

Do not treat a certificate as trusted merely because the endpoint presents it. Obtain the expected leaf fingerprint or CA certificate through a source independent of that endpoint, such as an existing trusted administrative record, the CA operator, or a separately authenticated system that already trusts the intended CA. Compare fingerprints exactly. Confirm the intended DNS name is present in the certificate SAN, the certificate is within its validity dates, and each required intermediate is available. If a private CA is used, verify the CA certificate's fingerprint and authority constraints against the independent source. A self-signed leaf requires an independently communicated fingerprint.

The GitHub workflow only reports observations. It does not select, trust, or save the presented certificate. If there is no independent expected fingerprint or CA source, stop and arrange an owner-verified source before changing trust.

## Read-only Kubernetes source comparison

The manual workflow `.github/workflows/monikey-argocd-certificate-kubernetes-verification.yml` uses the existing CA-verified schema-inspection kubeconfig and Tailscale connection. It is gated by the protected `monikey-production-inspection` Environment, the owner/main-only policy check, and an explicit confirmation input. It verifies the current context, exact API server, CA data, and that insecure TLS is disabled before checking any resource permissions.

The workflow checks read permission for the fixed `argocd-server` Deployment and namespace-scoped Ingress metadata before inspecting those objects. If no matching Ingress TLS source exists, it also checks and reads only the `server.insecure` field from `argocd-cmd-params-cm` before considering the conventional `argocd-server-tls` Secret. It checks `kubectl auth can-i get secrets/<name>` for each candidate before any Secret request. If allowed, it reads only `.data.tls.crt` with a fixed JSONPath; it never retrieves the Secret object or private key. Multiple or unclear candidate sources fail closed. The script compares the certificate's SHA-256 fingerprint with the observed endpoint fingerprint and reports only MATCH, MISMATCH, or BLOCKED plus redacted source/validity metadata.

The GitOps-managed inspection Role grants only `get pods/postgres-0` and `create pods/portforward` in namespace `monikey`; it grants no Argo CD namespace access. Other live bindings could exist, so effective permissions are checked at runtime. If the existing identity cannot read the required metadata, this workflow reports BLOCKED without reading a Secret. No additional access is granted by this PR. Do not widen this Role to make the check pass; any separate read-only permission proposal requires its own review and approval. The repository has no Argo CD Ingress or TLS Secret manifest, and installs Argo CD from its upstream manifest, so the live certificate source remains UNKNOWN until authorized runtime metadata can establish it.

## Optional Argo CD CLI trust certificate

If the expected private CA (or independently verified self-signed server certificate) is confirmed, configure the protected Environment secret `ARGOCD_SERVER_TRUST_CERT_B64` in `monikey-inspection-provisioning` with the base64-encoded PEM certificate bundle. For example, from a trusted workstation with the already verified PEM file:

```bash
base64 -w0 /path/to/verified-argocd-trust.pem | \
  gh secret set ARGOCD_SERVER_TRUST_CERT_B64 \
    --repo Jeysibn/homelab-gitops \
    --env monikey-inspection-provisioning
```

The bootstrap and subsequent read-only Application audit use the same protected Environment secret and `prepare_argocd_server_crt.py` helper. The helper accepts certificate PEM only, rejects private keys and out-of-validity certificates, and writes a mode-`0600` runner temporary file. When configured, both workflows pass that file to every Argo CD CLI invocation with `--server-crt`, including identity/permission checks and Application/resource/diff reads. A configured but invalid, missing, or unreadable file stops the workflow; it never falls back silently to system trust. Both workflows remove the temporary certificate after success or failure. If the optional secret is absent, system trust remains in use.

This format/validity check does not verify the bundle's authenticity. The owner must independently verify the expected certificate fingerprint or CA through a trusted source separate from the endpoint before storing it. The audit report's presented fingerprint is evidence to compare, not a trust decision. TLS verification remains enabled; neither workflow uses `--insecure` or plaintext.

## Approval sequence

1. Review and merge the workflow and documentation PR.
2. Dispatch the read-only TLS metadata diagnostic from `main` with confirmation and approve the protected Environment.
3. Independently verify the expected fingerprint or CA through a trusted source separate from the endpoint. Then dispatch the Kubernetes API source comparison with confirmation and approve `monikey-production-inspection`. If it reports BLOCKED, do not infer the certificate source or add permissions under this workflow.
4. Only after the comparison and independent verification agree, configure `ARGOCD_SERVER_TRUST_CERT_B64` in the protected provisioning Environment. Do not commit or log the PEM value.
5. Dispatch and approve the one-time Argo CD token bootstrap. It is a separate write operation and uses the verified trust bundle.
6. After bootstrap completes, dispatch and approve the read-only Argo CD Application audit. It uses the same verified trust bundle. Passing certificate checks does not itself authorize token creation, Application sync, or deployment.

No certificate diagnostic, secret configuration, Argo CD operation, sync, or deployment is performed by this repository change.
