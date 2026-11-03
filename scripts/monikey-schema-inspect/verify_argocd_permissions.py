#!/usr/bin/env python3
"""Fail closed unless this is the dedicated, read-only Argo CD project token."""

from __future__ import annotations

import json
import subprocess


EXPECTED_USERNAME = "proj:default:monikey-inspection-reader"
APPLICATION_OBJECT = "default/monikey-inspection-access"

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


def run_checked(command: list[str], run=subprocess.run) -> str:
    result = run(command, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode != 0:
        raise ValueError("Argo CD permission query failed")
    return result.stdout.strip()


def verify(executable: str = "argocd", run=subprocess.run) -> None:
    identity_output = run_checked(
        [executable, "account", "get-user-info", "--grpc-web", "--output", "json"], run
    )
    try:
        identity = json.loads(identity_output)
    except json.JSONDecodeError as error:
        raise ValueError("Argo CD returned an unreadable token identity") from error
    if not isinstance(identity, dict) or identity.get("username") != EXPECTED_USERNAME:
        raise ValueError("token is not the dedicated inspection project-role identity")

    for action, resource, object_name, expected in PERMISSION_CHECKS:
        actual = run_checked(
            [executable, "account", "can-i", action, resource, object_name, "--grpc-web"], run
        )
        if actual not in {"yes", "no"} or actual != expected:
            raise ValueError("effective Argo CD permissions do not match the read-only policy")


def main() -> int:
    try:
        verify()
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        print("::error::Argo CD token identity or effective permissions failed closed.")
        return 1
    print("Argo CD token identity and effective read-only permissions verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
