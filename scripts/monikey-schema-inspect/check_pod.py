#!/usr/bin/env python3
"""Validate the fixed expected Postgres pod identity without exposing env values."""

import json
import sys

try:
    pod = json.load(sys.stdin)
except json.JSONDecodeError:
    print("::error::Kubernetes returned invalid pod metadata.")
    raise SystemExit(1)

valid = (
    pod.get("metadata", {}).get("name") == "postgres-0"
    and pod.get("metadata", {}).get("namespace") == "monikey"
    and pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "postgres"
    and pod.get("status", {}).get("phase") == "Running"
    and any(container.get("name") == "postgres"
            and container.get("image", "").startswith("postgres:17.")
            for container in pod.get("spec", {}).get("containers", []))
)
if not valid:
    print("::error::PostgreSQL pod metadata does not match the fixed expected identity.")
    raise SystemExit(1)
print("Expected PostgreSQL pod identity verified.")
