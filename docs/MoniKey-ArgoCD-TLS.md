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

## Optional Argo CD CLI trust certificate

If the expected private CA (or independently verified self-signed server certificate) is confirmed, configure the protected Environment secret `ARGOCD_SERVER_TRUST_CERT_B64` in `monikey-inspection-provisioning` with the base64-encoded PEM certificate bundle. For example, from a trusted workstation with the already verified PEM file:

```bash
base64 -w0 /path/to/verified-argocd-trust.pem | \
  gh secret set ARGOCD_SERVER_TRUST_CERT_B64 \
    --repo Jeysibn/homelab-gitops \
    --env monikey-inspection-provisioning
```

The bootstrap workflow accepts certificate PEM only, rejects private keys and out-of-validity certificates, writes it to a mode-`0600` runner temporary file, and passes it to the Argo CD CLI with the documented `--server-crt` option. The same trust file is used for preflight, project-role operations, candidate permission checks, and failure cleanup, then deleted. If the secret is absent, the CLI uses the runner's normal system trust store.

This format/validity check does not verify the bundle's authenticity. The owner must complete the independent comparison before storing it. TLS verification remains enabled; the bootstrap never uses `--insecure` or plaintext.

## Approval sequence

1. Review and merge the documentation, diagnostic, and bootstrap trust-support PR.
2. Dispatch the certificate diagnostic from `main` with confirmation and approve the protected Environment.
3. Compare the reported fingerprint, hostname result, validity, chain, and trust result with an independently obtained expected certificate/CA. Do not proceed if the expected identity cannot be verified.
4. Only after that comparison, configure `ARGOCD_SERVER_TRUST_CERT_B64` as an Environment secret. Do not commit or log the PEM value.
5. Review and approve a separate dispatch of the bootstrap workflow. Its role/token writes still require independent owner approval; successful TLS inspection does not authorize those writes.

No certificate diagnostic, secret configuration, Argo CD operation, sync, or deployment is performed by this repository change.
