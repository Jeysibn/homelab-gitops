#!/usr/bin/env python3
"""Prepare MoniKey image promotion and migration-gate reset changes."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_SHA_RE = re.compile(r"sha-([0-9a-f]{40})")
APP_PATH = Path("kubernetes/platform/monikey/manifests/application.yaml")
MIGRATION_PATH = Path("kubernetes/platform/monikey/manifests/migration-runner.yaml")


def replace_once(text: str, pattern: str, replacement: str, description: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected exactly one {description}; found {count}")
    return updated


def promote(root: Path, sha: str, adoption: bool) -> None:
    if not SHA_RE.fullmatch(sha):
        raise ValueError("Expected a full 40-character lowercase commit SHA")
    app_path = root / APP_PATH
    migration_path = root / MIGRATION_PATH
    app = app_path.read_text()
    tag = f"sha-{sha}"
    for image in ("api", "frontend"):
        app = replace_once(
            app,
            rf"(ghcr\.io/jeysibn/monikey-v2-{image}:)sha-[0-9a-f]{{40}}",
            rf"\g<1>{tag}",
            f"{image} image reference",
        )
    migration = migration_path.read_text()
    migration = replace_once(
        migration,
        r'  MONIKEY_MIGRATION_APPLY: "(?:true|false)"',
        '  MONIKEY_MIGRATION_APPLY: "true"',
        "migration apply gate",
    )
    migration = replace_once(
        migration,
        r'  MONIKEY_MIGRATION_ADOPT: "(?:true|false)"',
        f'  MONIKEY_MIGRATION_ADOPT: "{str(adoption).lower()}"',
        "migration adoption gate",
    )
    migration = replace_once(
        migration,
        r"(ghcr\.io/jeysibn/monikey-v2-api:)sha-[0-9a-f]{40}",
        rf"\g<1>{tag}",
        "migration runner image",
    )
    app_path.write_text(app)
    migration_path.write_text(migration)


def close_gates(root: Path, sha: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise ValueError("Expected a full 40-character lowercase commit SHA")
    app = (root / APP_PATH).read_text()
    migration_path = root / MIGRATION_PATH
    migration = migration_path.read_text()
    if '  MONIKEY_MIGRATION_APPLY: "true"' not in migration:
        raise ValueError("Refusing to close gates: migration apply gate is not enabled")
    expected = f"sha-{sha}"
    app_shas = IMAGE_SHA_RE.findall(app)
    runner_shas = IMAGE_SHA_RE.findall(migration)
    if app_shas.count(sha) != 2 or runner_shas != [sha]:
        raise ValueError("Refusing to close gates: deployed application and migration runner do not match this SHA")
    migration = replace_once(
        migration,
        r'  MONIKEY_MIGRATION_APPLY: "(?:true|false)"',
        '  MONIKEY_MIGRATION_APPLY: "false"',
        "migration apply gate",
    )
    migration = replace_once(
        migration,
        r'  MONIKEY_MIGRATION_ADOPT: "(?:true|false)"',
        '  MONIKEY_MIGRATION_ADOPT: "false"',
        "migration adoption gate",
    )
    if expected not in migration:
        raise ValueError("Refusing to close gates: migration runner image SHA changed")
    migration_path.write_text(migration)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("promote", "close-gates"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--adoption", choices=("true", "false"), default="false")
    args = parser.parse_args()
    if args.action == "promote":
        promote(args.root, args.sha, args.adoption == "true")
    else:
        close_gates(args.root, args.sha)


if __name__ == "__main__":
    main()
