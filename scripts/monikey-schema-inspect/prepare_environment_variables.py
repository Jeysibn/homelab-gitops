#!/usr/bin/env python3
"""Prepare missing GitHub Environment variables from a flattened kubeconfig."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


ENVIRONMENTS = (
    "monikey-inspection-provisioning",
    "monikey-production-inspection",
)
VARIABLES = (
    "MONIKEY_SCHEMA_EXPECTED_CONTEXT",
    "MONIKEY_SCHEMA_EXPECTED_API_SERVER",
)


def extract_target(config: dict) -> dict[str, str]:
    contexts = config.get("contexts")
    clusters = config.get("clusters")
    current = config.get("current-context")
    if not isinstance(contexts, list) or len(contexts) != 1:
        raise ValueError("expected exactly one selected context")
    if not isinstance(clusters, list) or len(clusters) != 1:
        raise ValueError("expected exactly one selected cluster")
    context = contexts[0]
    cluster_entry = clusters[0]
    if not isinstance(context, dict) or not isinstance(cluster_entry, dict):
        raise ValueError("invalid selected context or cluster")
    if not current or context.get("name") != current:
        raise ValueError("current context does not match the selected context")
    context_data = context.get("context")
    cluster_data = cluster_entry.get("cluster")
    if not isinstance(context_data, dict) or not isinstance(cluster_data, dict):
        raise ValueError("selected context or cluster data is missing")
    if context_data.get("cluster") != cluster_entry.get("name"):
        raise ValueError("selected context does not reference the selected cluster")
    if cluster_data.get("insecure-skip-tls-verify") is True:
        raise ValueError("insecure TLS verification is enabled")
    ca_data = cluster_data.get("certificate-authority-data")
    if not isinstance(ca_data, str) or not ca_data:
        raise ValueError("selected cluster certificate authority data is missing")
    try:
        if not base64.b64decode(ca_data, validate=True):
            raise ValueError("selected cluster certificate authority data is empty")
    except (ValueError, binascii.Error) as error:
        raise ValueError("selected cluster certificate authority data is invalid") from error

    server = cluster_data.get("server")
    if not isinstance(server, str) or not server:
        raise ValueError("selected cluster API server is missing")
    if any(character.isspace() or ord(character) < 0x20 for character in server):
        raise ValueError("selected cluster API server URL contains whitespace or control data")
    try:
        parsed = urlsplit(server)
        parsed.port  # Validate a supplied port.
    except ValueError as error:
        raise ValueError("selected cluster API server URL is invalid") from error
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment):
        raise ValueError("selected cluster API server must be HTTPS without user info or a fragment")

    return {
        "MONIKEY_SCHEMA_EXPECTED_CONTEXT": current,
        "MONIKEY_SCHEMA_EXPECTED_API_SERVER": server,
    }


def listed_values(payload: dict) -> dict[str, str]:
    entries = payload.get("variables")
    if not isinstance(entries, list):
        raise ValueError("GitHub Environment variable response is invalid")
    result: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("GitHub Environment variable entry is invalid")
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or name in result:
            raise ValueError("GitHub Environment variable entry is invalid or duplicated")
        result[name] = value
    return result


def build_plan(target: dict[str, str], existing: dict[str, dict]) -> dict:
    writes = []
    for environment in ENVIRONMENTS:
        if environment not in existing:
            raise ValueError("a target GitHub Environment could not be inspected")
        values = listed_values(existing[environment])
        for name in VARIABLES:
            wanted = target[name]
            if name in values:
                if values[name] != wanted:
                    raise ValueError("an existing target variable conflicts; no values were changed")
                continue
            writes.append({"environment": environment, "name": name, "value": wanted})
    return {"target": target, "writes": writes}


def verify_plan(plan: dict, after: dict[str, dict]) -> None:
    target = plan.get("target")
    if not isinstance(target, dict):
        raise ValueError("prepared target is invalid")
    for environment in ENVIRONMENTS:
        if environment not in after:
            raise ValueError("a target GitHub Environment could not be verified")
        values = listed_values(after[environment])
        if any(values.get(name) != target.get(name) for name in VARIABLES):
            raise ValueError("target variables do not match in both Environments")


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input JSON must contain an object")
    return value


def write_private_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, separators=(",", ":"))
        output.write("\n")
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("kubeconfig_json", type=Path)
    prepare.add_argument("provisioning_variables_json", type=Path)
    prepare.add_argument("production_variables_json", type=Path)
    prepare.add_argument("output", type=Path)
    writes = commands.add_parser("writes")
    writes.add_argument("plan", type=Path)
    payload = commands.add_parser("payload")
    payload.add_argument("plan", type=Path)
    payload.add_argument("environment")
    payload.add_argument("variable")
    verify = commands.add_parser("verify")
    verify.add_argument("plan", type=Path)
    verify.add_argument("provisioning_variables_json", type=Path)
    verify.add_argument("production_variables_json", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "prepare":
            target = extract_target(load_json(args.kubeconfig_json))
            existing = {
                ENVIRONMENTS[0]: load_json(args.provisioning_variables_json),
                ENVIRONMENTS[1]: load_json(args.production_variables_json),
            }
            plan = build_plan(target, existing)
            write_private_json(args.output, plan)
            print(f"Validated target metadata; {len(plan['writes'])} missing variables will be created.")
        elif args.command == "writes":
            plan = load_json(args.plan)
            for item in plan.get("writes", []):
                print(f"{item['environment']}\t{item['name']}")
        elif args.command == "payload":
            plan = load_json(args.plan)
            item = next((entry for entry in plan.get("writes", [])
                         if entry.get("environment") == args.environment
                         and entry.get("name") == args.variable), None)
            if item is None:
                raise ValueError("requested variable is not a missing-value write")
            json.dump({"name": item["name"], "value": item["value"]}, sys.stdout)
            sys.stdout.write("\n")
        elif args.command == "verify":
            plan = load_json(args.plan)
            after = {
                ENVIRONMENTS[0]: load_json(args.provisioning_variables_json),
                ENVIRONMENTS[1]: load_json(args.production_variables_json),
            }
            verify_plan(plan, after)
            print("Both protected Environments contain matching target variables.")
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        print("::error::Kubeconfig target validation or Environment variable update failed safely.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
