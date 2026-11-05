#!/usr/bin/env python3
"""Fail closed unless this is the dedicated, read-only Argo CD project token."""

from __future__ import annotations

import json
import argparse
import os
import subprocess
from pathlib import Path


EXPECTED_USERNAME = "proj:default:monikey-inspection-reader"
APPLICATION_OBJECT = "default/monikey-inspection-access"


class VerificationFailure(ValueError):
    def __init__(self, category: str, check_number: int | None = None, expected: str | None = None):
        self.category = category
        self.check_number = check_number
        self.expected = expected
        super().__init__(self.diagnostic())

    def diagnostic(self) -> str:
        if self.check_number is not None:
            return (
                f"Argo CD permission check {self.check_number:02d} expected {self.expected}: "
                f"{self.category}."
            )
        return f"Argo CD verification failed: {self.category}."

# The first check is required for this audit. All remaining checks must be
# denied, including the effective default role and resource-level actions.
PERMISSION_CHECKS = [
    ("get", "applications", APPLICATION_OBJECT, "yes"),
    ("create", "applications", APPLICATION_OBJECT, "no"),
    ("update", "applications", APPLICATION_OBJECT, "no"),
    ("delete", "applications", APPLICATION_OBJECT, "no"),
    ("sync", "applications", APPLICATION_OBJECT, "no"),
    ("rollback", "applications", APPLICATION_OBJECT, "no"),
    ("override", "applications", APPLICATION_OBJECT, "no"),
    ("action/*", "applications", APPLICATION_OBJECT, "no"),
    ("update/*", "applications", APPLICATION_OBJECT, "no"),
    ("delete/*", "applications", APPLICATION_OBJECT, "no"),
    ("update/*", "applications", "*", "no"),
    ("delete/*", "applications", "*", "no"),
    # Check for broad write grants inherited from policy.default or groups.
    ("create", "applications", "*", "no"),
    ("update", "applications", "*", "no"),
    ("delete", "applications", "*", "no"),
    ("sync", "applications", "*", "no"),
    ("rollback", "applications", "*", "no"),
    ("override", "applications", "*", "no"),
    ("action/*", "applications", "*", "no"),
    ("create", "applicationsets", "*", "no"),
    ("update", "applicationsets", "*", "no"),
    ("delete", "applicationsets", "*", "no"),
    ("create", "clusters", "*", "no"),
    ("update", "clusters", "*", "no"),
    ("delete", "clusters", "*", "no"),
    ("create", "projects", "*", "no"),
    ("update", "projects", "*", "no"),
    ("delete", "projects", "*", "no"),
    ("create", "repositories", "*", "no"),
    ("update", "repositories", "*", "no"),
    ("delete", "repositories", "*", "no"),
    ("create", "certificates", "*", "no"),
    ("update", "certificates", "*", "no"),
    ("delete", "certificates", "*", "no"),
    ("create", "gpgkeys", "*", "no"),
    ("update", "gpgkeys", "*", "no"),
    ("delete", "gpgkeys", "*", "no"),
    ("create", "accounts", "*", "no"),
    ("update", "accounts", "*", "no"),
    ("delete", "accounts", "*", "no"),
    ("create", "exec", "*", "no"),
    ("invoke", "extensions", "*", "no"),
]


def request_category(message: str) -> str:
    lowered = message.lower()
    if any(marker in lowered for marker in ("x509", "certificate", "tls", "unknown authority")):
        return "TLS_OR_TRUST_FAILURE"
    if any(marker in lowered for marker in ("deadline exceeded", "timed out", "timeout", "i/o timeout")):
        return "CONNECTION_TIMEOUT"
    return "AUTHENTICATION_OR_REQUEST_ERROR"


def run_checked(command: list[str], run=subprocess.run) -> str:
    try:
        result = run(command, capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired as error:
        raise VerificationFailure("CONNECTION_TIMEOUT") from error
    except OSError as error:
        raise VerificationFailure("AUTHENTICATION_OR_REQUEST_ERROR") from error
    if result.returncode != 0:
        raise VerificationFailure(request_category(result.stderr or ""))
    return result.stdout.strip()


def verify(executable: str = "argocd", run=subprocess.run, server_crt: str | None = None) -> None:
    command_prefix = [executable]
    if server_crt:
        certificate = Path(server_crt)
        if not certificate.is_file() or not os.access(certificate, os.R_OK):
            raise VerificationFailure("TLS_OR_TRUST_FAILURE")
        command_prefix.extend(["--server-crt", server_crt])
    identity_output = run_checked(
        [*command_prefix, "account", "get-user-info", "--grpc-web", "--output", "json"], run
    )
    try:
        identity = json.loads(identity_output)
    except (json.JSONDecodeError, TypeError) as error:
        raise VerificationFailure("INVALID_PERMISSION_RESPONSE") from error
    if not isinstance(identity, dict) or identity.get("username") != EXPECTED_USERNAME:
        raise VerificationFailure("IDENTITY_MISMATCH")

    for check_number, (action, resource, object_name, expected) in enumerate(PERMISSION_CHECKS, start=1):
        try:
            actual = run_checked(
                [*command_prefix, "account", "can-i", action, resource, object_name, "--grpc-web"], run
            )
        except VerificationFailure as error:
            raise VerificationFailure(error.category, check_number, expected) from error
        if actual not in {"yes", "no"}:
            raise VerificationFailure("INVALID_PERMISSION_RESPONSE", check_number, expected)
        if actual != expected:
            category = "REQUIRED_PERMISSION_DENIED" if expected == "yes" else "UNEXPECTED_PERMISSION_ALLOWED"
            raise VerificationFailure(category, check_number, expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-crt", default=os.environ.get("ARGOCD_SERVER_CRT_FILE"))
    args = parser.parse_args()
    try:
        verify(server_crt=args.server_crt)
    except VerificationFailure as error:
        print(f"::error::{error.diagnostic()}")
        return 1
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        print("::error::Argo CD verification failed: AUTHENTICATION_OR_REQUEST_ERROR.")
        return 1
    print("Argo CD token identity and effective read-only permissions verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
