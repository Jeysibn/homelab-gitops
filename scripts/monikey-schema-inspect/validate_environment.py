#!/usr/bin/env python3
"""Validate the manual schema-inspection GitHub Environment configuration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENVIRONMENT_NAME = "monikey-production-inspection"


def validate(environment: dict, branch_policy: dict, repository_owner: str) -> list[str]:
    problems = []
    if environment.get("name") != ENVIRONMENT_NAME:
        problems.append("expected protected Environment is missing")

    owner = repository_owner.casefold()
    owner_rule_found = any(
        rule.get("type") == "required_reviewers"
        and rule.get("prevent_self_review") is False
        and any(
            reviewer.get("type") == "User"
            and (reviewer.get("reviewer") or {}).get("login", "").casefold() == owner
            for reviewer in rule.get("reviewers", [])
        )
        for rule in environment.get("protection_rules", [])
    )
    if not owner_rule_found:
        problems.append("repository owner must be a required reviewer with self-approval enabled")

    policy = environment.get("deployment_branch_policy") or {}
    names = [entry.get("name") for entry in branch_policy.get("branch_policies", [])]
    if (policy.get("custom_branch_policies") is not True
            or policy.get("protected_branches") is not False
            or names != ["main"]):
        problems.append("Environment must allow exactly the main branch")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("environment", type=Path)
    parser.add_argument("branch_policy", type=Path)
    parser.add_argument("repository_owner")
    args = parser.parse_args()
    try:
        environment = json.loads(args.environment.read_text(encoding="utf-8"))
        branch_policy = json.loads(args.branch_policy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("::error::Environment protection metadata is unavailable or invalid.")
        return 1
    problems = validate(environment, branch_policy, args.repository_owner)
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    print("Owner self-approval is enabled for this read-only inspection; it is not independent review.")
    print("Environment is protected for exactly the main branch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
