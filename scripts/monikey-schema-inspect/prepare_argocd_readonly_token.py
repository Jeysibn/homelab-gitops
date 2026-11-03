#!/usr/bin/env python3
"""Validate the fixed AppProject role and safely extract a CLI token response."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from pathlib import Path


PROJECT = "default"
ROLE = "monikey-inspection-reader"
SUBJECT = f"proj:{PROJECT}:{ROLE}"
EXPECTED_POLICY = (
    f"p, {SUBJECT}, applications, get, {PROJECT}/monikey-inspection-access, allow"
)
TOKEN_PATTERN = re.compile(r"(?m)^\s*Token:\s*(\S+)\s*$")


def role_plan(document: object) -> str:
    if not isinstance(document, dict):
        raise ValueError("default AppProject response is not an object")
    metadata = document.get("metadata")
    spec = document.get("spec")
    if not isinstance(metadata, dict) or metadata.get("name") != PROJECT:
        raise ValueError("Argo CD did not return the fixed default AppProject")
    if not isinstance(spec, dict):
        raise ValueError("default AppProject spec is missing")
    roles = spec.get("roles", [])
    if not isinstance(roles, list):
        raise ValueError("default AppProject roles are unreadable")
    matches = [item for item in roles if isinstance(item, dict) and item.get("name") == ROLE]
    if not matches:
        return "create"
    if len(matches) != 1:
        raise ValueError("inspection role is duplicated")
    role = matches[0]
    policies = role.get("policies", [])
    groups = role.get("groups", [])
    if (not isinstance(policies, list) or policies != [EXPECTED_POLICY]
            or groups not in ([], None)):
        raise ValueError("existing inspection role is not exactly the approved read-only role")
    return "reuse"


def require_role_ready(document: object) -> str:
    result = role_plan(document)
    if result != "reuse":
        raise ValueError("inspection project role is still absent after creation")
    return result


def extract_token(output: str) -> str:
    matches = TOKEN_PATTERN.findall(output)
    if len(matches) != 1:
        raise ValueError("Argo CD did not return exactly one token")
    token = matches[0]
    if token.count(".") != 2 or len(token) < 40:
        raise ValueError("Argo CD token response is malformed")
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("Argo CD token claims are malformed") from error
    if (not isinstance(claims, dict) or not isinstance(claims.get("iat"), int)
            or not isinstance(claims.get("exp"), int) or claims["exp"] - claims["iat"] != 3600):
        raise ValueError("Argo CD token lifetime is not exactly one hour")
    return token


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_umask = os.umask(0o077)
    try:
        path.write_text(content + "\n", encoding="utf-8")
        path.chmod(0o600)
    finally:
        os.umask(old_umask)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("role-plan")
    plan_parser.add_argument("project_json", type=Path)
    ready_parser = subparsers.add_parser("role-ready")
    ready_parser.add_argument("project_json", type=Path)
    token_parser = subparsers.add_parser("extract-token")
    token_parser.add_argument("response_file", type=Path)
    token_parser.add_argument("token_file", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "role-plan":
            print(role_plan(json.loads(args.project_json.read_text(encoding="utf-8"))))
        elif args.command == "role-ready":
            print(require_role_ready(json.loads(args.project_json.read_text(encoding="utf-8"))))
        else:
            token = extract_token(args.response_file.read_text(encoding="utf-8"))
            write_private(args.token_file, token)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
