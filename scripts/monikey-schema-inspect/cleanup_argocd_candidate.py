#!/usr/bin/env python3
"""Revoke only a uniquely identified candidate project token after failure."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Callable


PROJECT = "default"
ROLE = "monikey-inspection-reader"


def candidate_is_unique(project: object, metadata: object) -> bool:
    if not isinstance(project, dict) or not isinstance(metadata, dict):
        return False
    project_metadata = project.get("metadata")
    spec = project.get("spec")
    if not isinstance(project_metadata, dict) or project_metadata.get("name") != PROJECT:
        return False
    if not isinstance(spec, dict) or not isinstance(metadata.get("id"), str):
        return False
    issued_at = metadata.get("iat")
    if not isinstance(issued_at, int) or not metadata["id"]:
        return False
    roles = spec.get("roles")
    if not isinstance(roles, list):
        return False
    role_matches = [r for r in roles if isinstance(r, dict) and r.get("name") == ROLE]
    if len(role_matches) != 1:
        return False
    token_records = role_matches[0].get("jwtTokens")
    if not isinstance(token_records, list):
        return False
    same_issued_at = [
        item for item in token_records
        if isinstance(item, dict) and item.get("iat") == issued_at
    ]
    exact = [
        item for item in same_issued_at
        if item.get("id") == metadata["id"]
    ]
    return len(same_issued_at) == 1 and len(exact) == 1


def revoke_candidate(
    metadata: dict[str, int | str],
    *,
    runner: Callable = subprocess.run,
    environ: dict[str, str] | None = None,
) -> bool:
    child_env = dict(os.environ if environ is None else environ)
    if not child_env.get("ARGOCD_AUTH_TOKEN"):
        return False
    project_result = runner(
        ["argocd", "proj", "get", PROJECT, "--grpc-web", "--output", "json"],
        capture_output=True, text=True, timeout=20, check=False, env=child_env,
    )
    if project_result.returncode != 0:
        return False
    try:
        project = json.loads(project_result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    if not candidate_is_unique(project, metadata):
        return False

    result = runner(
        ["argocd", "proj", "role", "delete-token", PROJECT, ROLE,
         str(metadata["iat"]), "--grpc-web"],
        capture_output=True, text=True, timeout=20, check=False, env=child_env,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("metadata_file", type=Path)
    args = parser.parse_args()
    try:
        metadata = json.loads(args.metadata_file.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or not revoke_candidate(metadata):
            print("Candidate token could not be safely identified and revoked.")
            return 1
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, TypeError, ValueError):
        print("Candidate token could not be safely identified and revoked.")
        return 1
    print("The uniquely identified candidate token was revoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
