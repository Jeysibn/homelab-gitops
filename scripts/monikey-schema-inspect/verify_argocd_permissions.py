#!/usr/bin/env python3
"""Fail closed unless this is the dedicated, read-only Argo CD project token."""

from __future__ import annotations

import json
import argparse
import csv
import fnmatch
import io
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_USERNAME = "proj:default:monikey-inspection-reader"
APPLICATION_OBJECT = "default/monikey-inspection-access"
EXPECTED_PROJECT_POLICY = (
    "p, proj:default:monikey-inspection-reader, applications, get, "
    "default/monikey-inspection-access, allow"
)
SUPPORTED_ACTIONS = frozenset({
    "get", "create", "update", "delete", "sync", "override", "action", "invoke",
})
SUPPORTED_RESOURCES = frozenset({
    "clusters", "projects", "applications", "applicationsets", "repositories",
    "write-repositories", "certificates", "accounts", "gpgkeys", "logs", "exec", "extensions",
})
RBAC_EVIDENCE_MAX_AGE_SECONDS = 15 * 60


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

# In v3.5.1, server/application/application.go Rollback enforces ActionSync.
# The API rejects action/* and fine-grained actions; policy evidence covers them.
PERMISSION_CHECKS = [
    ("get", "applications", APPLICATION_OBJECT, "yes"),
    # This credential is scoped to one named Application; reject inherited
    # application-read access from global roles or policy.default.
    ("get", "applications", "*/*", "no"),
    ("create", "applications", APPLICATION_OBJECT, "no"),
    ("update", "applications", APPLICATION_OBJECT, "no"),
    ("delete", "applications", APPLICATION_OBJECT, "no"),
    ("sync", "applications", APPLICATION_OBJECT, "no"),
    ("override", "applications", APPLICATION_OBJECT, "no"),
    ("action", "applications", APPLICATION_OBJECT, "no"),
    # Check for broad write grants inherited from policy.default or groups.
    ("create", "applications", "*/*", "no"),
    ("update", "applications", "*/*", "no"),
    ("delete", "applications", "*/*", "no"),
    ("sync", "applications", "*/*", "no"),
    ("override", "applications", "*/*", "no"),
    ("action", "applications", "*/*", "no"),
    ("create", "applicationsets", "*/*", "no"),
    ("update", "applicationsets", "*/*", "no"),
    ("delete", "applicationsets", "*/*", "no"),
    ("create", "clusters", "*", "no"),
    ("update", "clusters", "*", "no"),
    ("delete", "clusters", "*", "no"),
    ("create", "projects", "*", "no"),
    ("update", "projects", "*", "no"),
    ("delete", "projects", "*", "no"),
    ("create", "repositories", "*", "no"),
    ("update", "repositories", "*", "no"),
    ("delete", "repositories", "*", "no"),
    ("create", "write-repositories", "*", "no"),
    ("update", "write-repositories", "*", "no"),
    ("delete", "write-repositories", "*", "no"),
    ("create", "certificates", "*", "no"),
    ("update", "certificates", "*", "no"),
    ("delete", "certificates", "*", "no"),
    ("create", "gpgkeys", "*", "no"),
    ("update", "gpgkeys", "*", "no"),
    ("delete", "gpgkeys", "*", "no"),
    ("create", "accounts", "*", "no"),
    ("update", "accounts", "*", "no"),
    ("delete", "accounts", "*", "no"),
    ("create", "exec", "*/*", "no"),
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


def validate_rbac_evidence(
    path: str | None,
    now: datetime | None = None,
    require_project_role: bool = True,
    expected_context: str | None = None,
    expected_api_server: str | None = None,
) -> dict:
    if not path or not Path(path).is_file():
        raise VerificationFailure("RBAC_EVIDENCE_UNAVAILABLE")
    try:
        evidence = json.loads(Path(path).read_text(encoding="utf-8"))
        captured_at = datetime.fromisoformat(evidence["capturedAt"].replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, AttributeError) as error:
        raise VerificationFailure("RBAC_EVIDENCE_INVALID") from error
    now = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None or (now - captured_at).total_seconds() > RBAC_EVIDENCE_MAX_AGE_SECONDS:
        raise VerificationFailure("RBAC_EVIDENCE_UNAVAILABLE")
    if (captured_at - now).total_seconds() > 60:
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")

    cluster = evidence.get("cluster") if isinstance(evidence, dict) else None
    if (
        not isinstance(cluster, dict)
        or not expected_context
        or not expected_api_server
        or cluster.get("context") != expected_context
        or cluster.get("apiServer") != expected_api_server
    ):
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")

    app_project = evidence.get("appProject") if isinstance(evidence, dict) else None
    project_metadata = app_project.get("metadata") if isinstance(app_project, dict) else None
    project_spec = app_project.get("spec") if isinstance(app_project, dict) else None
    if (
        not isinstance(project_metadata, dict)
        or project_metadata.get("name") != "default"
        or project_metadata.get("namespace") != "argocd"
        or not project_metadata.get("uid")
        or not project_metadata.get("resourceVersion")
        or not isinstance(project_spec, dict)
        or not isinstance(project_spec.get("roles"), list)
    ):
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")
    if require_project_role:
        validate_project_policy(app_project)

    config_map = evidence.get("configMap") if isinstance(evidence, dict) else None
    metadata = config_map.get("metadata") if isinstance(config_map, dict) else None
    data = config_map.get("data") if isinstance(config_map, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "argocd-rbac-cm"
        or metadata.get("namespace") != "argocd"
        or not metadata.get("uid")
        or not metadata.get("resourceVersion")
        or not isinstance(data, dict)
        or set(data) - {"policy.csv", "policy.default", "policy.matchMode", "scopes"}
        or any(not isinstance(value, str) for value in data.values())
        or data.get("policy.matchMode", "glob") != "glob"
        or not isinstance(data.get("policy.csv", ""), str)
    ):
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")
    # role:readonly grants applications get */* in the pinned Argo CD policy.
    # It cannot be narrowed by the project role, so it violates this token's
    # one-Application read scope.
    if data.get("policy.default", "").strip():
        raise VerificationFailure("RBAC_EVIDENCE_UNSAFE")

    try:
        for row in csv.reader(io.StringIO(data.get("policy.csv", "")), skipinitialspace=True):
            if not row or not any(field.strip() for field in row) or row[0].lstrip().startswith("#"):
                continue
            kind = row[0].strip()
            if (kind == "p" and len(row) != 6) or (kind == "g" and len(row) not in {3, 4}):
                raise VerificationFailure("RBAC_EVIDENCE_INVALID")
            if kind not in {"p", "g"}:
                raise VerificationFailure("RBAC_EVIDENCE_INVALID")
            if kind != "p" or row[5].strip().lower() != "allow":
                continue
            resource_pattern = row[2].strip()
            action_pattern = row[3].strip().lower()
            if not fnmatch.fnmatchcase("applications", resource_pattern):
                continue
            if action_pattern == "*" or action_pattern.startswith(("update/", "delete/", "action/")) or any(
                fnmatch.fnmatchcase(f"{action}/__fine_grained_probe__", action_pattern)
                for action in ("update", "delete", "action")
            ):
                raise VerificationFailure("RBAC_EVIDENCE_UNSAFE")
    except csv.Error as error:
        raise VerificationFailure("RBAC_EVIDENCE_INVALID") from error
    return evidence


def validate_project_policy(project: object) -> None:
    try:
        if isinstance(project, str):
            project = json.loads(project)
        metadata = project.get("metadata")
        roles = project.get("spec", {}).get("roles", [])
    except (json.JSONDecodeError, AttributeError, TypeError) as error:
        raise VerificationFailure("RBAC_EVIDENCE_INVALID") from error
    if (
        not isinstance(project, dict)
        or not isinstance(metadata, dict)
        or metadata.get("name") != "default"
        or metadata.get("namespace") != "argocd"
        or not metadata.get("uid")
        or not metadata.get("resourceVersion")
    ):
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")
    if not isinstance(roles, list):
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")
    matches = [role for role in roles if isinstance(role, dict) and role.get("name") == "monikey-inspection-reader"]
    if len(matches) != 1:
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")
    role = matches[0]
    if role.get("policies") != [EXPECTED_PROJECT_POLICY] or role.get("groups") not in ([], None):
        raise VerificationFailure("RBAC_EVIDENCE_INVALID")


def verify(
    executable: str = "argocd",
    run=subprocess.run,
    server_crt: str | None = None,
    rbac_evidence_file: str | None = None,
    project_json_file: str | None = None,
    expected_context: str | None = None,
    expected_api_server: str | None = None,
) -> None:
    command_prefix = [executable]
    if server_crt:
        certificate = Path(server_crt)
        if not certificate.is_file() or not os.access(certificate, os.R_OK):
            raise VerificationFailure("TLS_OR_TRUST_FAILURE")
        command_prefix.extend(["--server-crt", server_crt])
    evidence = validate_rbac_evidence(
        rbac_evidence_file,
        require_project_role=not project_json_file,
        expected_context=expected_context,
        expected_api_server=expected_api_server,
    )
    identity_output = run_checked(
        [*command_prefix, "account", "get-user-info", "--grpc-web", "--output", "json"], run
    )
    try:
        identity = json.loads(identity_output)
    except (json.JSONDecodeError, TypeError) as error:
        raise VerificationFailure("INVALID_PERMISSION_RESPONSE") from error
    if not isinstance(identity, dict) or identity.get("username") != EXPECTED_USERNAME:
        raise VerificationFailure("IDENTITY_MISMATCH")
    groups = identity.get("groups", [])
    if not isinstance(groups, list) or groups:
        raise VerificationFailure("IDENTITY_MISMATCH")

    if project_json_file:
        try:
            project_output = Path(project_json_file).read_text(encoding="utf-8")
        except OSError as error:
            raise VerificationFailure("RBAC_EVIDENCE_UNAVAILABLE") from error
        validate_project_policy(project_output)
    else:
        validate_project_policy(evidence["appProject"])

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
    parser.add_argument("--rbac-evidence-file", default=os.environ.get("ARGOCD_RBAC_EVIDENCE_FILE"))
    parser.add_argument("--expected-kubernetes-context", default=os.environ.get("ARGOCD_KUBE_CONTEXT"))
    parser.add_argument("--expected-kubernetes-api-server", default=os.environ.get("ARGOCD_KUBE_API_SERVER"))
    parser.add_argument("--project-json-file")
    parser.add_argument("--check-rbac-evidence", action="store_true")
    args = parser.parse_args()
    try:
        if args.check_rbac_evidence:
            validate_rbac_evidence(
                args.rbac_evidence_file,
                require_project_role=False,
                expected_context=args.expected_kubernetes_context,
                expected_api_server=args.expected_kubernetes_api_server,
            )
            print("Fresh global Argo CD RBAC evidence verified.")
            return 0
        verify(
            server_crt=args.server_crt,
            rbac_evidence_file=args.rbac_evidence_file,
            project_json_file=args.project_json_file,
            expected_context=args.expected_kubernetes_context,
            expected_api_server=args.expected_kubernetes_api_server,
        )
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
