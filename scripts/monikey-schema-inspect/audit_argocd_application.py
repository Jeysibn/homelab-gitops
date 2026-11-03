#!/usr/bin/env python3
"""Summarize one Argo CD Application without printing resource manifests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


EXPECTED = {
    ("", "ServiceAccount", "monikey", "monikey-schema-inspector"): "inspector-sa",
    ("rbac.authorization.k8s.io", "Role", "monikey", "monikey-schema-inspector"): "inspector-role",
    ("rbac.authorization.k8s.io", "RoleBinding", "monikey", "monikey-schema-inspector"): "inspector-binding",
    ("rbac.authorization.k8s.io", "Role", "monikey", "monikey-inspection-token-issuer"): "token-issuer-role",
    ("rbac.authorization.k8s.io", "RoleBinding", "monikey", "monikey-inspection-token-issuer"): "token-issuer-binding",
}
EXPECTED_SOURCE = {
    "repoURL": "https://github.com/jeysibn/homelab-gitops.git",
    "targetRevision": "main",
    "path": "kubernetes/platform/monikey/inspection-access/manifests",
}
EXPECTED_RULES = {
    "inspector-role": [
        {"apiGroups": [""], "resources": ["pods"], "resourceNames": ["postgres-0"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods/portforward"], "resourceNames": ["postgres-0"], "verbs": ["create"]},
    ],
    "token-issuer-role": [
        {"apiGroups": [""], "resources": ["serviceaccounts/token"],
         "resourceNames": ["monikey-schema-inspector"], "verbs": ["create"]},
    ],
}


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def identity(resource: dict) -> tuple[str, str, str, str]:
    return (
        resource.get("group", "") or "",
        resource.get("kind", "") or "",
        resource.get("namespace", "") or "",
        resource.get("name", "") or "",
    )


def normalize_rules(rules) -> list[dict]:
    if not isinstance(rules, list):
        raise ValueError("role rules are missing")
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("role rule is malformed")
        # Reject non-resource and aggregation privileges and compare every field.
        if set(rule) - {"apiGroups", "resources", "resourceNames", "verbs"}:
            raise ValueError("role contains a privilege outside the reviewed resource rules")
        normalized.append({
            key: sorted(rule.get(key, []))
            for key in ("apiGroups", "resources", "resourceNames", "verbs")
        })
    return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))


def validate_live_object(key: str, resource: dict) -> bool:
    group, kind, namespace, name = next(identity for identity, name_key in EXPECTED.items()
                                       if name_key == key)
    metadata = resource.get("metadata", {})
    if (resource.get("kind") != kind or metadata.get("name") != name
            or metadata.get("namespace") != namespace):
        return False
    if key == "inspector-sa":
        return resource.get("automountServiceAccountToken") is False
    if key in EXPECTED_RULES:
        try:
            return normalize_rules(resource.get("rules")) == normalize_rules(EXPECTED_RULES[key])
        except ValueError:
            return False
    expected_binding = {
        "inspector-binding": (
            "monikey-schema-inspector",
            [{"kind": "ServiceAccount", "name": "monikey-schema-inspector", "namespace": "monikey"}],
        ),
        "token-issuer-binding": (
            "monikey-inspection-token-issuer",
            [{"kind": "ServiceAccount", "name": "monikey-backup", "namespace": "monikey"}],
        ),
    }
    role_name, subjects = expected_binding[key]
    role_ref = resource.get("roleRef", {})
    actual_subjects = resource.get("subjects", [])
    normalized_subjects = [
        {field: subject.get(field, "") for field in ("kind", "name", "namespace")}
        for subject in actual_subjects if isinstance(subject, dict)
    ]
    return (
        role_ref == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role_name}
        and normalized_subjects == subjects
    )


def summarize(application: dict, diff_exit_code: int, live_objects: dict[str, dict]) -> dict:
    metadata = application.get("metadata", {})
    spec = application.get("spec", {})
    status = application.get("status", {})
    source = spec.get("source", {})
    destination = spec.get("destination", {})
    sync_policy = spec.get("syncPolicy") or {}
    automated = sync_policy.get("automated")
    automated_enabled = isinstance(automated, dict) and automated.get("enabled") is not False
    operation_state = status.get("operationState") or {}
    operation_phase = operation_state.get("phase", "")
    operation_running = operation_phase in {"Pending", "Running", "Terminating"} or bool(spec.get("operation"))
    application_sync_status = (status.get("sync") or {}).get("status")
    application_health_status = (status.get("health") or {}).get("status")

    source_ok = (
        all(source.get(key) == value for key, value in EXPECTED_SOURCE.items())
        and not spec.get("sources")
    )
    destination_ok = (
        destination.get("namespace") == "monikey"
        and destination.get("server") == "https://kubernetes.default.svc"
    )
    resources = status.get("resources")
    if not isinstance(resources, list):
        raise ValueError("Application resource comparison status is unavailable")
    by_identity = {}
    unexpected = []
    additions = []
    updates = []
    deletions = []
    live_count = 0
    incomplete = []
    unsafe_health = []

    if application_sync_status not in {"Synced", "OutOfSync"}:
        incomplete.append("Application sync status")
    elif (application_sync_status == "OutOfSync") != (diff_exit_code == 1):
        incomplete.append("Application sync/diff status")
    if application_health_status not in {"Healthy", "Missing"}:
        incomplete.append("Application health status")

    for resource in resources:
        if not isinstance(resource, dict):
            raise ValueError("Application resource status entry is malformed")
        item_id = identity(resource)
        if item_id in by_identity:
            raise ValueError("Application contains duplicate resource status entries")
        by_identity[item_id] = resource
        label = "/".join(part for part in item_id if part)
        if item_id not in EXPECTED:
            unexpected.append(label)
        pruning = resource.get("requiresPruning") is True
        health = resource.get("health")
        health_status = health.get("status") if isinstance(health, dict) else None
        sync_status = resource.get("status")
        if health_status is None:
            incomplete.append(label)
        elif health_status != "Missing":
            live_count += 1
        if sync_status not in {"Synced", "OutOfSync"}:
            incomplete.append(label)
        if health_status not in {"Healthy", "Missing"}:
            unsafe_health.append(label)
        if health_status == "Missing" and (sync_status != "OutOfSync" or pruning):
            incomplete.append(label)
        if pruning:
            deletions.append(label)
        elif health_status == "Missing":
            additions.append(label)
        elif sync_status == "OutOfSync":
            updates.append(label)

    missing_status = ["/".join(item) for item in EXPECTED if item not in by_identity]
    if missing_status:
        incomplete.extend(missing_status)

    live_rbac_checks = {}
    for item_id, key in EXPECTED.items():
        state = by_identity.get(item_id)
        if not state:
            continue
        health = state.get("health", {})
        if state.get("requiresPruning") or health.get("status") == "Missing":
            continue
        live_obj = live_objects.get(key)
        live_rbac_checks[key] = bool(live_obj and validate_live_object(key, live_obj))
    rbac_valid = all(live_rbac_checks.values())
    rbac_status = (
        "NOT YET PRESENT; desired rules are covered by repository tests"
        if not live_rbac_checks
        else "VERIFIED" if rbac_valid else "FAILED"
    )

    pending = bool(additions or updates or deletions)
    diff_consistent = (diff_exit_code == 1) == pending
    desired_count = sum(1 for item_id in EXPECTED if item_id in by_identity
                        and by_identity[item_id].get("requiresPruning") is not True)
    safe_to_sync = (
        metadata.get("name") == "monikey-inspection-access"
        and metadata.get("namespace") == "argocd"
        and spec.get("project") == "default"
        and source_ok and destination_ok and not automated_enabled and not operation_running
        and desired_count == len(EXPECTED) and not unexpected and not incomplete
        and not unsafe_health and not updates and not deletions and diff_consistent and rbac_valid
    )

    return {
        "applicationFound": metadata.get("name") == "monikey-inspection-access",
        "applicationNamespace": metadata.get("namespace"),
        "projectMatches": spec.get("project") == "default",
        "syncStatus": (status.get("sync") or {}).get("status", "Unknown"),
        "health": (status.get("health") or {}).get("status", "Unknown"),
        "unsafeHealthResources": sorted(unsafe_health),
        "targetRevision": source.get("targetRevision"),
        "observedRevision": (status.get("sync") or {}).get("revision"),
        "sourcePath": source.get("path"),
        "sourceRepositoryMatches": source_ok,
        "destinationNamespace": destination.get("namespace"),
        "automatedSync": "ENABLED" if automated_enabled else "DISABLED",
        "operationPhase": operation_phase or "None",
        "operationRunning": operation_running,
        "desiredResourceCount": len(EXPECTED),
        "comparedDesiredResourceCount": desired_count,
        "liveResourceCount": live_count,
        "pendingAdditions": sorted(additions),
        "pendingUpdates": sorted(updates),
        "pendingDeletions": sorted(deletions),
        "unexpectedResources": sorted(unexpected),
        "incompleteResourceStatus": sorted(set(incomplete)),
        "diffFound": diff_exit_code == 1,
        "diffStatusConsistent": diff_consistent,
        "liveRbacChecks": live_rbac_checks,
        "liveRbacStatus": rbac_status,
        "liveRbacValid": rbac_valid if live_rbac_checks else None,
        "safeToSync": safe_to_sync,
    }


def write_plan(application: dict, path: Path) -> None:
    resources = application.get("status", {}).get("resources")
    if not isinstance(resources, list):
        raise ValueError("Application resource comparison status is unavailable")
    by_identity = {identity(item): item for item in resources if isinstance(item, dict)}
    rows = []
    for item_id, key in EXPECTED.items():
        item = by_identity.get(item_id)
        if not item:
            raise ValueError("Application resource comparison is incomplete")
        health = item.get("health", {})
        if item.get("requiresPruning") is not True and health.get("status") != "Missing":
            rows.append(key)
    path.write_text("".join(f"{key}\n" for key in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan-live-reads")
    plan.add_argument("application_json", type=Path)
    plan.add_argument("output", type=Path)
    report = commands.add_parser("report")
    report.add_argument("application_json", type=Path)
    report.add_argument("diff_exit_code", type=int)
    report.add_argument("live_directory", type=Path)
    report.add_argument("output", type=Path)
    args = parser.parse_args()

    try:
        application = read_json(args.application_json)
        if args.command == "plan-live-reads":
            write_plan(application, args.output)
        else:
            if args.diff_exit_code not in (0, 1):
                raise ValueError("Argo CD diff command returned an error")
            live = {}
            for key in EXPECTED.values():
                path = args.live_directory / f"{key}.json"
                if path.exists():
                    live[key] = read_json(path)
            result = summarize(application, args.diff_exit_code, live)
            args.output.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
            os.chmod(args.output, 0o600)
            print(json.dumps(result, indent=2, sort_keys=True))
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        print("::error::Argo CD inspection was incomplete or failed closed; no resource data was printed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
