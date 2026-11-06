#!/usr/bin/env python3
"""Fail closed unless this is the dedicated, read-only Argo CD project token."""

from __future__ import annotations

import json
import argparse
import fnmatch
import os
import subprocess
import re
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_USERNAME = "proj:default:monikey-inspection-reader"
APPLICATION_OBJECT = "default/monikey-inspection-access"
EXPECTED_PROJECT_POLICY = (
    "p, proj:default:monikey-inspection-reader, applications, get, "
    "default/monikey-inspection-access, allow"
)
SUPPORTED_ACTIONS = frozenset({
    "get", "create", "update", "delete", "sync", "rollback", "override", "action", "invoke",
})
SUPPORTED_RESOURCES = frozenset({
    "clusters", "projects", "applications", "applicationsets", "repositories",
    "write-repositories", "certificates", "accounts", "gpgkeys", "logs", "exec", "extensions",
})
RBAC_EVIDENCE_MAX_AGE_SECONDS = 15 * 60

CONFIGMAP_KEY = re.compile(r"[-._a-zA-Z0-9]+\Z")
GO_UNICODE_WHITESPACE = frozenset(
    "\t\n\v\f\r \u0085\u00a0\u1680\u2028\u2029\u202f\u205f\u3000"
) | frozenset(chr(codepoint) for codepoint in range(0x2000, 0x200B))


def valid_configmap_key(key: object) -> bool:
    return isinstance(key, str) and len(key) <= 253 and CONFIGMAP_KEY.fullmatch(key) is not None


def is_policy_fragment_key(key: object) -> bool:
    # Match Argo CD v3.5.1 PolicyCSV: policy.*.csv, excluding policy.csv.
    return (
        isinstance(key, str)
        and key.startswith("policy.")
        and key.endswith(".csv")
        and key != "policy.csv"
    )


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
    # Project reads can expose project destinations, source repositories, and
    # role configuration. Wildcard denial does not prove exact-project denial.
    ("get", "projects", "*", "no"),
    ("get", "projects", "default", "no"),
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

    # Version 1 omitted all other roles and cannot establish isolation.
    if evidence.get("schemaVersion") != 2:
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
        raise VerificationFailure("APP_PROJECT_VALIDATION")

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
        or any(not valid_configmap_key(key) for key in data)
        or any(key not in {"policy.csv", "policy.default", "policy.matchMode", "scopes"}
               and not is_policy_fragment_key(key) for key in data)
        or any(not isinstance(value, str) for value in data.values())
        or data.get("policy.matchMode", "glob") != "glob"
        or not isinstance(data.get("policy.csv", ""), str)
    ):
        raise VerificationFailure("RBAC_CONFIGMAP_VALIDATION")
    # role:readonly grants applications get */* in the pinned Argo CD policy.
    # It cannot be narrowed by the project role, so it violates this token's
    # one-Application read scope.
    if data.get("policy.default", "").strip():
        raise VerificationFailure("RBAC_POLICY_UNSAFE")

    validate_composed_policy(data, app_project, require_project_role=require_project_role)
    return evidence


def compose_policy(data: dict) -> str:
    # Argo CD v3.5.1 PolicyCSV: main first, then overlay keys lexicographically.
    return data.get("policy.csv", "") + "".join(
        "\n" + data[key] for key in sorted(data) if is_policy_fragment_key(key)
    )


def trim_go_space(value: str) -> str:
    # Match unicode.IsSpace, which strings.TrimSpace uses in Go. Keep this
    # explicit instead of relying on Python's Unicode database version.
    start, end = 0, len(value)
    while start < end and value[start] in GO_UNICODE_WHITESPACE:
        start += 1
    while end > start and value[end - 1] in GO_UNICODE_WHITESPACE:
        end -= 1
    return value[start:end]


class PolicyCSVError(ValueError):
    """A malformed single physical policy line, matching Go encoding/csv."""


def parse_argocd_policy_line(line: str) -> list[str]:
    """Parse one already-trimmed physical line like Argo's Go CSV reader.

    Argo constructs a fresh encoding/csv.Reader for each line, enables
    TrimLeadingSpace, and leaves trailing and quoted-field whitespace intact.
    This small parser implements that single-line dialect explicitly.
    """
    fields = []
    index = 0
    while True:
        while index < len(line) and line[index] in GO_UNICODE_WHITESPACE:
            index += 1

        field = []
        if index < len(line) and line[index] == '"':
            index += 1
            while True:
                if index >= len(line):
                    raise PolicyCSVError("unterminated quoted field")
                char = line[index]
                if char != '"':
                    field.append(char)
                    index += 1
                    continue
                if index + 1 < len(line) and line[index + 1] == '"':
                    field.append('"')
                    index += 2
                    continue
                index += 1
                if index < len(line) and line[index] != ",":
                    raise PolicyCSVError("unexpected data after quoted field")
                break
        else:
            while index < len(line) and line[index] != ",":
                if line[index] == '"':
                    raise PolicyCSVError("quote in unquoted field")
                field.append(line[index])
                index += 1

        fields.append("".join(field))
        if index >= len(line):
            return fields
        # The only remaining character accepted after a field is its delimiter.
        if line[index] != ",":
            raise PolicyCSVError("unexpected CSV character")
        index += 1
        if index == len(line):
            fields.append("")
            return fields


def policy_records(policy: str) -> list[list[str]]:
    rows = []
    try:
        # Argo CD v3.5.1 splits policy text into physical lines, trims each
        # line, and recognizes comments before passing the line to CSV parsing.
        for raw_line in policy.split("\n"):
            line = trim_go_space(raw_line)
            if not line or line.startswith("#"):
                continue
            row = parse_argocd_policy_line(line)
            if any(not field or any(ord(char) < 32 for char in field) for field in row):
                raise VerificationFailure("RBAC_POLICY_VALIDATION")
            if row[0] == "g":
                # The pinned model has a two-argument role relation, without domains.
                if len(row) != 3 or any("*" in field for field in row):
                    raise VerificationFailure("RBAC_POLICY_VALIDATION")
                if row[2] in {"role:admin", "role:readonly"}:
                    raise VerificationFailure("RBAC_POLICY_UNSAFE")
                rows.append(row)
                continue
            if row[0] != "p" or len(row) != 6 or row[5] not in {"allow", "deny"}:
                raise VerificationFailure("RBAC_POLICY_VALIDATION")
            resource, action = row[2:4]
            if not re.fullmatch(r"[a-z*.-]+", resource) or not any(
                fnmatch.fnmatchcase(candidate, resource) for candidate in SUPPORTED_RESOURCES
            ) or not re.fullmatch(r"[A-Za-z0-9_/*.-]+", action):
                raise VerificationFailure("RBAC_POLICY_VALIDATION")
            if action not in SUPPORTED_ACTIONS and not action.startswith(("update/", "delete/", "action/")) and "*" not in action:
                raise VerificationFailure("RBAC_POLICY_VALIDATION")
            rows.append(row)
    except PolicyCSVError as error:
        raise VerificationFailure("RBAC_POLICY_VALIDATION") from error
    return rows


def project_policy_records(project: object, require_inspection: bool = True) -> tuple[list[list[str]], list[list[str]]]:
    """Compose all roles as v3.5.1 ProjectPoliciesString does, including groups.

    Return explicit and implicit records separately: upstream project reads are
    real permissions, but are NOT part of the approved inspection contract.
    """
    try:
        if isinstance(project, str):
            project = json.loads(project)
        metadata = project.get("metadata")
        roles = project.get("spec", {}).get("roles")
    except (json.JSONDecodeError, AttributeError, TypeError) as error:
        raise VerificationFailure("APP_PROJECT_VALIDATION") from error
    if (not isinstance(metadata, dict) or metadata.get("name") != "default"
            or metadata.get("namespace") != "argocd" or not metadata.get("uid")
            or not metadata.get("resourceVersion") or not isinstance(roles, list)):
        raise VerificationFailure("APP_PROJECT_VALIDATION")
    names, explicit, implicit = set(), [], []
    for role in roles:
        if not isinstance(role, dict):
            raise VerificationFailure("APP_PROJECT_VALIDATION")
        name = role.get("name")
        # Conservative literal identifiers: reject CSV injection, ambiguous
        # project-token subjects and unsupported role names rather than guess.
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or name in names:
            raise VerificationFailure("APP_PROJECT_VALIDATION")
        names.add(name)
        policies, groups = role.get("policies", []), role.get("groups", [])
        policies = [] if policies is None else policies
        groups = [] if groups is None else groups
        if (not isinstance(policies, list) or not all(isinstance(p, str) for p in policies)
                or not isinstance(groups, list) or not all(isinstance(g, str) for g in groups)):
            raise VerificationFailure("APP_PROJECT_VALIDATION")
        subject = f"proj:default:{name}"
        implicit.append(["p", subject, "projects", "get", "default", "allow"])
        explicit.extend(policy_records("\n".join(policies)))
        for group in groups:
            # ProjectPoliciesString interpolates groups directly into CSV.
            # Accept literal, unambiguous values only; Casbin g is exact equality.
            if (not group or any(c in GO_UNICODE_WHITESPACE or c in ',"' or ord(c) < 32 for c in group)):
                raise VerificationFailure("RBAC_POLICY_UNSAFE")
            explicit.extend(policy_records(f"g, {group}, {subject}"))
        if name == "monikey-inspection-reader" and policies != [EXPECTED_PROJECT_POLICY]:
            raise VerificationFailure("RBAC_POLICY_UNSAFE")
    if require_inspection and "monikey-inspection-reader" not in names:
        raise VerificationFailure("APP_PROJECT_VALIDATION")
    return explicit, implicit


def validate_composed_policy(
    data: dict, project: object = None, *, require_project_role: bool = True,
) -> frozenset[tuple[str, ...]]:
    """Validate isolation and return a conservative upper bound of allow grants.

    Denies never excuse unsafe inheritance or additional explicit allows.
    This is not approval of the strict contract: the CLI gate rejects the
    separately modeled upstream implicit project read.
    """
    global_rows = policy_records(compose_policy(data))
    project_rows, implicit_rows = ([], []) if project is None else project_policy_records(project, require_project_role)
    intended = ["applications", "get", APPLICATION_OBJECT]
    # Keep the existing exact global allowlist, even for isolated subjects.
    for row in global_rows:
        if row[0] == "p" and row[5] == "allow" and (row[1] != EXPECTED_USERNAME or row[2:5] != intended):
            raise VerificationFailure("RBAC_POLICY_UNSAFE")
    inheritance, inheritors = {}, {}
    for row in global_rows + project_rows:
        if row[0] == "g":
            inheritance.setdefault(row[1], set()).add(row[2])
            inheritors.setdefault(row[2], set()).add(row[1])
    # Iterative topological traversal detects cycles without recursion limits.
    nodes = set(inheritance) | {p for parents in inheritance.values() for p in parents}
    incoming = dict.fromkeys(nodes, 0)
    for parents in inheritance.values():
        for parent in parents:
            incoming[parent] += 1
    pending = [node for node in nodes if incoming[node] == 0]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        for parent in inheritance.get(current, ()):
            incoming[parent] -= 1
            if incoming[parent] == 0:
                pending.append(parent)
    if visited != len(nodes):
        raise VerificationFailure("RBAC_POLICY_VALIDATION")

    def reachable(subject, edges):
        pending, reached = [subject], set()
        while pending:
            current = pending.pop()
            if current not in reached:
                reached.add(current)
                pending.extend(edges.get(current, ()))
        return reached

    # Casbin's g relation is literal, reflexive and transitive (no glob subject
    # matching). Check every edge, even when there is NO global p record.
    # Unbounded reachability is deliberately stricter than Casbin's depth cap.
    if reachable(EXPECTED_USERNAME, inheritors) - {EXPECTED_USERNAME}:
        raise VerificationFailure("RBAC_POLICY_UNSAFE")
    effective_subjects = reachable(EXPECTED_USERNAME, inheritance)
    for row in global_rows + project_rows:
        if row[0] == "p" and row[5] == "allow" and row[1] in effective_subjects and row[2:5] != intended:
            raise VerificationFailure("RBAC_POLICY_UNSAFE")
    # Every implicit row in this captured project grants exactly projects/get/
    # default. It is unavoidable for the inspection role, including through
    # cross-role inheritance. The CLI contract gate below rejects that grant.
    return frozenset(
        tuple(row[2:5]) for row in global_rows + project_rows + implicit_rows
        if row[0] == "p" and row[5] == "allow" and row[1] in effective_subjects
    )


def validate_project_policy(project: object) -> None:
    project_policy_records(project)


def require_strict_permission_contract() -> None:
    # v3.5.1 always supplies projects/get/default for this AppProject role.
    # Deny precedence could suppress it, but approving a compensating global
    # policy is a separate security decision, outside this verifier contract.
    # No finite (or mocked) token probes approve that contract change.
    # Also block pre-provisioning --check-rbac-evidence before any role writes.
    raise VerificationFailure("RBAC_IMPLICIT_PROJECT_READ_BLOCKER")


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
        validate_composed_policy(evidence["configMap"]["data"], project_output)
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

    require_strict_permission_contract()


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
            require_strict_permission_contract()
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
