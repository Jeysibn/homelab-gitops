#!/usr/bin/env python3
"""Capture fresh, read-only AppProject and global Argo CD RBAC evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import stat
import re
from datetime import datetime, timezone
from pathlib import Path


ROLE_NAME = "monikey-inspection-reader"
RBAC_DATA_KEYS = {"policy.csv", "policy.default", "policy.matchMode", "scopes"}
POLICY_FRAGMENT_KEY = re.compile(r"policy\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.csv\Z")


class EvidenceError(ValueError):
    def __init__(self, stage="EVIDENCE_VALIDATION"):
        self.stage = stage
        super().__init__(stage)


def checked_json(command: list[str], run=subprocess.run, stage="KUBERNETES_READ") -> dict:
    try:
        result = run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError(stage) from error
    if result.returncode != 0:
        raise EvidenceError(stage)
    try:
        document = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise EvidenceError(stage) from error
    if not isinstance(document, dict):
        raise EvidenceError(stage)
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
        raise EvidenceError("APP_PROJECT_VALIDATION")
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
        or any(key not in RBAC_DATA_KEYS and not POLICY_FRAGMENT_KEY.fullmatch(key) for key in data)
        or any(not isinstance(value, str) for value in data.values())
    ):
        raise EvidenceError("RBAC_CONFIGMAP_VALIDATION")
    return {
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata["namespace"],
            "uid": metadata["uid"],
            "resourceVersion": metadata["resourceVersion"],
        },
        "data": dict(sorted(data.items())),
    }


def write_private(output: Path, evidence: dict) -> None:
    """Create once inside a private directory owned by this effective user."""
    try:
        payload = json.dumps(evidence, separators=(",", ":")) + "\n"
    except (TypeError, ValueError) as error:
        raise EvidenceError("SERIALIZATION") from error
    directory_fd = None
    created = False
    complete = False
    stage = "OUTPUT_DIRECTORY"
    try:
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise EvidenceError(stage)
        stage = "OUTPUT_CREATE"
        fd = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        created = True
        stage = "OUTPUT_WRITE"
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            stage = "OUTPUT_PERMISSIONS"
            os.fchmod(stream.fileno(), 0o600)
        complete = True
    except OSError as error:
        raise EvidenceError(stage) from error
    finally:
        try:
            if created and not complete:
                os.unlink(output.name, dir_fd=directory_fd)
        except OSError as error:
            raise EvidenceError("OUTPUT_CLEANUP") from error
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


def capture(context: str, api_server: str, output: Path, run=subprocess.run) -> None:
    try:
        current_context = run(
            ["kubectl", "config", "current-context"], capture_output=True,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError("CONTEXT_VALIDATION") from error
    if current_context.returncode != 0 or current_context.stdout.strip() != context:
        raise EvidenceError("CONTEXT_VALIDATION")
    try:
        current_server = run(
            ["kubectl", "--context", context, "config", "view", "--minify", "-o",
             "jsonpath={.clusters[0].cluster.server}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError("API_SERVER_VALIDATION") from error
    if current_server.returncode != 0 or current_server.stdout.strip() != api_server:
        raise EvidenceError("API_SERVER_VALIDATION")
    project = checked_json(
        ["kubectl", "--context", context, "--request-timeout=10s", "get",
         "appproject.argoproj.io", "default", "--namespace", "argocd", "-o", "json"], run, stage="APP_PROJECT_READ"
    )
    config_map = checked_json(
        ["kubectl", "--context", context, "--request-timeout=10s", "get",
         "configmap", "argocd-rbac-cm", "--namespace", "argocd", "-o", "json"], run, stage="RBAC_CONFIGMAP_READ"
    )
    evidence = {
        "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cluster": {"context": context, "apiServer": api_server},
        "appProject": project_evidence(project),
        "configMap": rbac_evidence(config_map),
    }
    write_private(output, evidence)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--api-server", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        capture(args.context, args.api_server, args.output)
    except EvidenceError as error:
        print(f"::error::Argo CD RBAC evidence capture failed: {error.stage}.")
        return 1
    print("Fresh Argo CD RBAC evidence captured with restrictive file permissions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
