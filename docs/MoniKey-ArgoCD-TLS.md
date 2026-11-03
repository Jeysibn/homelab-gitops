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

The bootstrap and subsequent read-only Application audit use the same protected Environment secret and `prepare_argocd_server_crt.py` helper. The helper accepts certificate PEM only, rejects private keys and out-of-validity certificates, and writes a mode-`0600` runner temporary file. When configured, both workflows pass that file to every Argo CD CLI invocation with `--server-crt`, including identity/permission checks and Application/resource/diff reads. A configured but invalid, missing, or unreadable file stops the workflow; it never falls back silently to system trust. Both workflows remove the temporary certificate after success or failure. If the optional secret is absent, system trust remains in use.

This format/validity check does not verify the bundle's authenticity. The owner must independently verify the expected certificate fingerprint or CA through a trusted source separate from the endpoint before storing it. The audit report's presented fingerprint is evidence to compare, not a trust decision. TLS verification remains enabled; neither workflow uses `--insecure` or plaintext.

## Approval sequence

1. Review and merge the documentation, diagnostic, and bootstrap trust-support PR.
2. Dispatch the certificate diagnostic from `main` with confirmation and approve the protected Environment.
3. Compare the reported fingerprint, hostname result, validity, chain, and trust result with an independently obtained expected certificate/CA. Do not proceed if the expected identity cannot be verified.
4. Only after that comparison, configure `ARGOCD_SERVER_TRUST_CERT_B64` as an Environment secret. Do not commit or log the PEM value.
5. After configuring the verified trust bundle, dispatch the read-only Application audit with confirmation and approve its protected Environment. The same trust configuration is then used for the later bootstrap workflow; review and approve a separate bootstrap dispatch because it creates a role/token. Successful TLS inspection or a read-only audit does not authorize those writes.

No certificate diagnostic, secret configuration, Argo CD operation, sync, or deployment is performed by this repository change.
