#!/usr/bin/env python3
"""Replace a bootstrap kubeconfig identity with a short-lived inspector token."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def build_config(
    base: dict, token: str, expected_context: str, expected_server: str
) -> dict:
    if not token or len(token.split(".")) != 3:
        raise ValueError("service account token is unavailable or malformed")
    if base.get("current-context") != expected_context:
        raise ValueError("bootstrap context does not match the approved context")
    if len(base.get("clusters", [])) != 1 or len(base.get("contexts", [])) != 1:
        raise ValueError("expected a flattened kubeconfig with one cluster and context")

    cluster = base["clusters"][0]
    context = base["contexts"][0]
    if context.get("name") != expected_context:
        raise ValueError("context name does not match the approved context")
    cluster_data = cluster.get("cluster", {})
    if cluster_data.get("server") != expected_server:
        raise ValueError("API server does not match the approved cluster")
    if not cluster_data.get("certificate-authority-data") or cluster_data.get("insecure-skip-tls-verify"):
        raise ValueError("cluster CA data is required and insecure TLS must be disabled")

    context_data = dict(context.get("context", {}))
    if context_data.get("cluster") != cluster.get("name"):
        raise ValueError("kubeconfig context refers to an unexpected cluster")
    context_data["user"] = "monikey-schema-inspector"
    context_data["namespace"] = "monikey"

    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [cluster],
        "contexts": [{"name": expected_context, "context": context_data}],
        "current-context": expected_context,
        "users": [{
            "name": "monikey-schema-inspector",
            "user": {"token": token},
        }],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_config", type=Path)
    parser.add_argument("token_file", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-context", required=True)
    parser.add_argument("--expected-server", required=True)
    args = parser.parse_args()
    try:
        base = json.loads(args.base_config.read_text(encoding="utf-8"))
        token = args.token_file.read_text(encoding="utf-8").strip()
        result = build_config(base, token, args.expected_context, args.expected_server)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(result, output, separators=(",", ":"))
            output.write("\n")
        os.chmod(args.output, 0o600)
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        print("::error::Could not build a safe inspector kubeconfig from the fixed target.")
        return 1
    print("Created an inspector-only kubeconfig with a short-lived token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
