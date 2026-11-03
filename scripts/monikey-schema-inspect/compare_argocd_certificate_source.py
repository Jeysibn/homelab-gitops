#!/usr/bin/env python3
"""Compare the observed Argo CD endpoint fingerprint with authorized K8s TLS sources."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml


OBSERVED_ENDPOINT_SHA256 = "753D8311A193F17E7B403C97EAE44E3A95DFC3CBC8DA8C1FE587F2E139ADDDE5"
NAMESPACE = "argocd"
ARGOCD_SERVICE = "argocd-server"
ARGOCD_SECRET = "argocd-server-tls"
FINGERPRINT = re.compile(
    r"(?:SHA256|SHA2-256) Fingerprint\s*=\s*([0-9A-Fa-f:]+)", re.IGNORECASE
)


class InspectionBlocked(Exception):
    """Raised when identity, permissions, or authoritative source is uncertain."""


def _run_kubectl(arguments: list[str], runner: Callable = subprocess.run) -> str:
    try:
        result = runner(
            ["kubectl", *arguments], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InspectionBlocked from error
    if result.returncode != 0:
        raise InspectionBlocked
    return result.stdout.strip()


def _allowed(resource: str, verb: str, runner: Callable) -> bool:
    try:
        result = runner(
            ["kubectl", "auth", "can-i", verb, resource, "--namespace", NAMESPACE],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InspectionBlocked from error
    if result.returncode != 0 or result.stdout.strip() not in {"yes", "no"}:
        raise InspectionBlocked
    return result.stdout.strip() == "yes"


def _validate_cluster(expected_context: str, expected_server: str, config: dict) -> None:
    if not expected_context or config.get("current-context") != expected_context:
        raise InspectionBlocked
    clusters = config.get("clusters", [])
    if len(clusters) != 1:
        raise InspectionBlocked
    contexts = config.get("contexts", [])
    if len(contexts) != 1:
        raise InspectionBlocked
    context = contexts[0]
    if (
        context.get("name") != expected_context
        or context.get("context", {}).get("user") != "monikey-schema-inspector"
        or context.get("context", {}).get("namespace") != "monikey"
    ):
        raise InspectionBlocked
    cluster = clusters[0].get("cluster", {})
    if (
        cluster.get("server") != expected_server
        or not cluster.get("certificate-authority-data")
        or cluster.get("insecure-skip-tls-verify") is True
    ):
        raise InspectionBlocked


def _argocd_route_hosts(ingress: dict) -> set[str]:
    spec = ingress.get("spec", {})
    hosts: set[str] = set()
    for rule in spec.get("rules", []):
        paths = (rule.get("http") or {}).get("paths", [])
        for path in paths:
            service = ((path.get("backend") or {}).get("service") or {}).get("name")
            if service == ARGOCD_SERVICE:
                hosts.add(str(rule.get("host", "")))
    default_service = ((spec.get("defaultBackend") or {}).get("service") or {}).get("name")
    if default_service == ARGOCD_SERVICE:
        raise InspectionBlocked
    return hosts


def _candidate_secrets(ingresses: dict) -> list[tuple[str, str]]:
    candidates: dict[str, str] = {}
    items = ingresses.get("items")
    if not isinstance(items, list):
        raise InspectionBlocked
    for ingress in items:
        if not isinstance(ingress, dict):
            continue
        route_hosts = _argocd_route_hosts(ingress)
        if not route_hosts or "" in route_hosts:
            continue
        annotations = ingress.get("metadata", {}).get("annotations", {}) or {}
        passthrough = any(
            str(value).lower() == "true"
            for key, value in annotations.items()
            if "passthrough" in key.lower()
        )
        if passthrough:
            continue
        for tls in ingress.get("spec", {}).get("tls", []):
            name = tls.get("secretName") if isinstance(tls, dict) else None
            tls_hosts = set(tls.get("hosts", [])) if isinstance(tls, dict) else set()
            if name and route_hosts.intersection(tls_hosts):
                candidates[name] = "Kubernetes Ingress TLS terminator"

    return sorted(candidates.items())


def _deployment_is_insecure(deployment: dict) -> bool:
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    server = next((item for item in containers if item.get("name") == ARGOCD_SERVICE), None)
    if not isinstance(server, dict):
        raise InspectionBlocked
    arguments = {str(value).lower() for value in server.get("args", [])}
    environment = {
        str(item.get("name", "")): str(item.get("value", "")).lower()
        for item in server.get("env", []) if isinstance(item, dict)
    }
    return bool(arguments.intersection({"--insecure", "--insecure=true"})) or (
        environment.get("ARGOCD_SERVER_INSECURE") == "true"
    )


def _certificate_metadata(encoded_certificate: str, runner: Callable) -> tuple[str, str]:
    try:
        pem = base64.b64decode(encoded_certificate, validate=True)
    except (ValueError, binascii.Error) as error:
        raise InspectionBlocked from error
    if b"PRIVATE KEY" in pem or b"-----BEGIN CERTIFICATE-----" not in pem:
        raise InspectionBlocked
    try:
        result = runner(
            ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-dates"],
            input=pem, capture_output=True, text=False, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InspectionBlocked from error
    if result.returncode != 0:
        raise InspectionBlocked
    output = result.stdout.decode("ascii", errors="strict")
    fingerprint_match = FINGERPRINT.search(output)
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if not fingerprint_match or not {"notBefore", "notAfter"}.issubset(fields):
        raise InspectionBlocked
    validity = _validity(fields["notBefore"], fields["notAfter"])
    fingerprint = fingerprint_match.group(1).replace(":", "").upper()
    return fingerprint, validity


def _validity(not_before: str, not_after: str) -> str:
    date_format = "%b %d %H:%M:%S %Y %Z"
    try:
        start = datetime.strptime(not_before, date_format).replace(tzinfo=timezone.utc)
        end = datetime.strptime(not_after, date_format).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise InspectionBlocked from error
    now = datetime.now(timezone.utc)
    if now < start:
        return "NOT_YET_VALID"
    if now > end:
        return "EXPIRED"
    return "CURRENT"


def inspect(
    expected_context: str,
    expected_server: str,
    *,
    runner: Callable = subprocess.run,
    kubeconfig: dict | None = None,
) -> dict[str, str]:
    """Use only fixed, authorization-checked metadata and TLS certificate reads."""
    try:
        if kubeconfig is None:
            kubeconfig_path = os.environ.get("KUBECONFIG", "")
            if not kubeconfig_path:
                raise InspectionBlocked
            kubeconfig = yaml.safe_load(Path(kubeconfig_path).read_text(encoding="utf-8"))
        if not isinstance(kubeconfig, dict):
            raise InspectionBlocked
        _validate_cluster(expected_context, expected_server, kubeconfig)
        required_reads = (
            ("deployments/argocd-server", "get"),
            ("ingresses.networking.k8s.io", "list"),
        )
        if not all(_allowed(resource, verb, runner) for resource, verb in required_reads):
            raise InspectionBlocked

        # These reads contain controller and routing metadata only, never Secret data.
        deployment = json.loads(_run_kubectl(
            ["get", "deployment/argocd-server", "--namespace", NAMESPACE, "-o", "json"], runner
        ))
        ingresses = json.loads(_run_kubectl(
            ["get", "ingresses.networking.k8s.io", "--namespace", NAMESPACE, "-o", "json"], runner
        ))
        if deployment.get("metadata", {}).get("name") != ARGOCD_SERVICE:
            raise InspectionBlocked
        candidates = _candidate_secrets(ingresses)
        if not candidates:
            # Without an Ingress TLS certificate, verify Argo CD is not configured
            # for HTTP before treating its conventional TLS Secret as authoritative.
            if not _allowed("configmaps/argocd-cmd-params-cm", "get", runner):
                raise InspectionBlocked
            server_insecure = _deployment_is_insecure(deployment)
            config_insecure = _run_kubectl(
                ["get", "configmap/argocd-cmd-params-cm", "--namespace", NAMESPACE,
                 "-o", r"jsonpath={.data.server\.insecure}"], runner,
            ).lower()
            if server_insecure or config_insecure == "true":
                raise InspectionBlocked
            if config_insecure not in {"", "false"}:
                raise InspectionBlocked
            candidates = [(ARGOCD_SECRET, "Argo CD server TLS Secret")]
        if len(candidates) != 1:
            raise InspectionBlocked

        # Check authorization for every fixed/discovered Secret before retrieving
        # only its public tls.crt field. Secret data and private keys stay unprinted.
        for name, _source in candidates:
            if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
                raise InspectionBlocked
            if not _allowed(f"secrets/{name}", "get", runner):
                raise InspectionBlocked

        comparisons: list[tuple[str, str]] = []
        for name, source in candidates:
            if source == "Argo CD server TLS Secret":
                cert_manager = _run_kubectl(
                    ["get", f"secret/{name}", "--namespace", NAMESPACE,
                     "-o", r"jsonpath={.metadata.labels.cert-manager\.io/certificate-name}"], runner,
                )
                if cert_manager:
                    source = "Kubernetes cert-manager Secret used by Argo CD"
                else:
                    source = "Argo CD server TLS Secret; writer unverified"
            encoded = _run_kubectl(
                ["get", f"secret/{name}", "--namespace", NAMESPACE,
                 "-o", r"jsonpath={.data.tls\.crt}"], runner,
            )
            fingerprint, validity = _certificate_metadata(encoded, runner)
            comparison = "MATCH" if fingerprint == OBSERVED_ENDPOINT_SHA256 else "MISMATCH"
            comparisons.append((source, f"{comparison}/{validity}"))

        matched_sources = [source for source, result in comparisons if result.startswith("MATCH/")]
        if len(matched_sources) == 1:
            source = matched_sources[0]
            result = "MATCH"
        elif len(matched_sources) > 1:
            source = "MULTIPLE_AUTHORIZED_SOURCES"
            result = "MATCH"
        else:
            source = comparisons[0][0] if len(comparisons) == 1 else "MULTIPLE_CANDIDATE_SOURCES"
            result = "MISMATCH"
        validity = ",".join(sorted({item.split("/", 1)[1] for _, item in comparisons}))
        return {
            "comparison": result,
            "certificate_source": source,
            "certificate_metadata": f"candidate_count={len(comparisons)}; validity={validity}",
        }
    except (InspectionBlocked, OSError, yaml.YAMLError, json.JSONDecodeError,
            KeyError, TypeError, ValueError):
        return {
            "comparison": "BLOCKED",
            "certificate_source": "UNKNOWN",
            "certificate_metadata": "not read; authoritative source or required permission unavailable",
        }


def main() -> int:
    result = inspect(
        os.environ.get("EXPECTED_CONTEXT", ""),
        os.environ.get("EXPECTED_API_SERVER", ""),
    )
    print(f"Certificate fingerprint comparison: {result['comparison']}")
    print(f"Certificate source: {result['certificate_source']}")
    print(f"Redacted certificate metadata: {result['certificate_metadata']}")
    return 0 if result["comparison"] == "MATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
