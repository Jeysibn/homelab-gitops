#!/usr/bin/env python3
"""Validate and write an owner-approved Argo CD trust certificate bundle."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+.+?\s+-----END CERTIFICATE-----",
    re.DOTALL,
)


def decode_certificates(encoded: str, *, runner=subprocess.run) -> bytes:
    compact = "".join(encoded.split())
    try:
        pem = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("configured trust material is not valid base64") from error
    if not pem or b"PRIVATE KEY" in pem:
        raise ValueError("trust material must contain certificates only")

    certificates = CERTIFICATE.findall(pem)
    remainder = CERTIFICATE.sub(b"", pem).strip()
    if not certificates or remainder:
        raise ValueError("trust material must be a PEM certificate bundle")
    for certificate in certificates:
        result = runner(
            ["openssl", "x509", "-noout", "-dates"],
            input=certificate + b"\n", capture_output=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise ValueError("trust bundle contains an unreadable certificate")
        output = result.stdout.decode("ascii", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        try:
            not_before = datetime.strptime(fields["notBefore"], "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc
            )
            not_after = datetime.strptime(fields["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc
            )
        except (KeyError, ValueError) as error:
            raise ValueError("trust bundle certificate validity is unreadable") from error
        now = datetime.now(timezone.utc)
        if not_before > now or not_after < now:
            raise ValueError("trust bundle contains a certificate outside its validity period")
    return pem


def write_private(path: Path, pem: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(pem)
    os.chmod(path, 0o600)


def prepare_optional(encoded: str, path: Path) -> bool:
    if not encoded:
        return False
    write_private(path, decode_certificates(encoded))
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_file", type=Path)
    args = parser.parse_args()
    try:
        encoded = os.environ.get("ARGOCD_SERVER_TRUST_CERT_B64", "")
        if not encoded:
            print("No optional Argo CD trust certificate was configured; system trust remains in use.")
            return 0
        prepare_optional(encoded, args.output_file)
    except (OSError, subprocess.SubprocessError, ValueError):
        print("::error::Configured Argo CD trust material is not a valid, current PEM certificate bundle.")
        return 1
    print("Configured Argo CD trust certificate material is ready for TLS verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
