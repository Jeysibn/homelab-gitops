#!/usr/bin/env python3
"""Capture fresh, read-only AppProject and global Argo CD RBAC evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROLE_NAME = "monikey-inspection-reader"
RBAC_DATA_KEYS = {"policy.csv", "policy.default", "policy.matchMode", "scopes"}


class EvidenceError(ValueError):
    pass


def checked_json(command: list[str], run=subprocess.run) -> dict:
    try:
        result = run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError from error
    if result.returncode != 0:
        raise EvidenceError
    try:
        document = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise EvidenceError from error
    if not isinstance(document, dict):
        raise EvidenceError
    return document


def project_evidence(document: dict) -> dict:
    metadata = document.get("metadata")
    spec = document.get("spec")
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "default"
        or metadata.get("namespace") != "argocd"
        or not metadata.get("uid")
        or not metadata.get("resourceVersion")
        or not isinstance(spec, dict)
        or not isinstance(spec.get("roles", []), list)
    ):
        raise EvidenceError
    roles = [
        role for role in spec.get("roles", [])
        if isinstance(role, dict) and role.get("name") == ROLE_NAME
    ]
    return {
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata["namespace"],
            "uid": metadata["uid"],
            "resourceVersion": metadata["resourceVersion"],
        },
        "spec": {"roles": roles},
    }


def rbac_evidence(document: dict) -> dict:
    metadata = document.get("metadata")
    data = document.get("data", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "argocd-rbac-cm"
        or metadata.get("namespace") != "argocd"
        or not metadata.get("uid")
        or not metadata.get("resourceVersion")
        or not isinstance(data, dict)
        or set(data) - RBAC_DATA_KEYS
        or any(not isinstance(value, str) for value in data.values())
    ):
        raise EvidenceError
    return {
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata["namespace"],
            "uid": metadata["uid"],
            "resourceVersion": metadata["resourceVersion"],
        },
        "data": data,
    }


def capture(context: str, api_server: str, output: Path, run=subprocess.run) -> None:
    try:
        current_context = run(
            ["kubectl", "config", "current-context"], capture_output=True,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError from error
    if current_context.returncode != 0 or current_context.stdout.strip() != context:
        raise EvidenceError
    try:
        current_server = run(
            ["kubectl", "--context", context, "config", "view", "--minify", "-o",
             "jsonpath={.clusters[0].cluster.server}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError from error
    if current_server.returncode != 0 or current_server.stdout.strip() != api_server:
        raise EvidenceError
    project = checked_json(
        ["kubectl", "--context", context, "--request-timeout=10s", "get",
         "appproject.argoproj.io", "default", "--namespace", "argocd", "-o", "json"], run
    )
    config_map = checked_json(
        ["kubectl", "--context", context, "--request-timeout=10s", "get",
         "configmap", "argocd-rbac-cm", "--namespace", "argocd", "-o", "json"], run
    )
    evidence = {
        "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cluster": {"context": context, "apiServer": api_server},
        "appProject": project_evidence(project),
        "configMap": rbac_evidence(config_map),
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_umask = os.umask(0o077)
    try:
        output.write_text(json.dumps(evidence, separators=(",", ":")) + "\n", encoding="utf-8")
        output.chmod(0o600)
    finally:
        os.umask(old_umask)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--api-server", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        capture(args.context, args.api_server, args.output)
    except EvidenceError:
        print("::error::Fresh Argo CD RBAC evidence could not be captured from the approved cluster context.")
        return 1
    print("Fresh Argo CD RBAC evidence captured with restrictive file permissions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
