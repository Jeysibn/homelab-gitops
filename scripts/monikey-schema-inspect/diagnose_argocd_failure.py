#!/usr/bin/env python3
"""Classify an Argo CD CLI failure without exposing its output."""

from __future__ import annotations

import argparse
from pathlib import Path


DIAGNOSTICS = {
    "TLS": "Check the server certificate chain and hostname; TLS verification was not bypassed.",
    "network": "Check Tailscale connectivity, DNS, routing, and server reachability.",
    "authentication": "Check whether the configured Argo CD token is present, valid, and accepted.",
    "authorization": "Review the credential's effective permission for this read-only request.",
    "CLI incompatibility": "Check the pinned Argo CD CLI version and the documented command flags.",
    "unknown": "Review the protected run and its local error details before retrying.",
}


def classify_failure(stderr: str, stdout: str = "", exit_status: int = 1) -> str:
    text = f"{stderr}\n{stdout}".lower()

    if any(marker in text for marker in (
        "x509:", "certificate signed by", "certificate verify failed",
        "tls handshake", "ssl certificate", "unknown authority",
    )):
        return "TLS"
    if any(marker in text for marker in (
        "unknown flag", "unknown command", "unknown shorthand flag",
        "flag provided but not defined", "unrecognized option", "unknown output format",
    )) or exit_status in (2, 126, 127):
        return "CLI incompatibility"
    if any(marker in text for marker in (
        "unauthenticated", "unauthorized", "status code 401", "http 401",
        "invalid bearer token", "invalid session", "token is expired", "token expired",
        "authentication required",
    )):
        return "authentication"
    if any(marker in text for marker in (
        "permissiondenied", "permission denied by rbac", "status code 403", "http 403",
        "forbidden", "does not have permission", "not authorized to",
    )):
        return "authorization"
    if exit_status in (124, 137) or any(marker in text for marker in (
        "dial tcp", "connection refused", "connection reset", "no such host",
        "network is unreachable", "no route to host", "i/o timeout",
        "context deadline exceeded", "failed to connect", "transport is closing",
        "server misbehaving",
    )):
        return "network"
    return "unknown"


def diagnostic(stderr: str, stdout: str = "", exit_status: int = 1) -> str:
    category = classify_failure(stderr, stdout, exit_status)
    return f"Argo CD failure category: {category}. {DIAGNOSTICS[category]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stderr_file", type=Path)
    parser.add_argument("stdout_file", type=Path)
    parser.add_argument("exit_status", type=int)
    args = parser.parse_args()
    try:
        stderr = args.stderr_file.read_text(encoding="utf-8", errors="replace")
        stdout = args.stdout_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr, stdout = "", ""
    print(diagnostic(stderr, stdout, args.exit_status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
