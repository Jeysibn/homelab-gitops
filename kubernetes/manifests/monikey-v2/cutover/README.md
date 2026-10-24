# Hostname cutover resources

This Ingress is included in the V2 GitOps application because the legacy V1
MoniKey Application and namespace have been retired. It routes the approved
private hostname to the V2 web Service.

The Ingress references the V2 web Service and cert-manager-managed
`monikey-v2-web-tls` Secret. Verify the CA is installed on each owner device
and that the existing Traefik HTTP-to-HTTPS redirect applies.
