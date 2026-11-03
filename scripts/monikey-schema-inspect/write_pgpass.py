#!/usr/bin/env python3
"""Write a pgpass file for a fixed local tunnel and fixed database role."""

import pathlib
import sys

password = sys.stdin.readline().rstrip("\n")
if not password:
    print("Database password is required", file=sys.stderr)
    raise SystemExit(2)
escaped = password.replace("\\", "\\\\").replace(":", "\\:")
pathlib.Path(sys.argv[1]).write_text(
    f"127.0.0.1:15432:monikey_dev:monikey_schema_inspector:{escaped}\n",
    encoding="utf-8",
)
