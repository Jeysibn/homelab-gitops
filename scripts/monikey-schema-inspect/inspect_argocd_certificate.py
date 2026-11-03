#!/usr/bin/env python3
"""Inspect Argo CD's presented TLS chain and print redacted certificate metadata."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from diagnose_argocd_failure import classify_failure


CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+.+?\s+-----END CERTIFICATE-----",
    re.DOTALL,
)
VERIFY_CODE = re.compile(r"Verify return code:\s*(\d+)")


def parse_server_target(value: str) -> tuple[str, str, bool]:
    if not value or any(character in value for character in "\r\n\t"):
        raise ValueError("invalid configured server target")
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if parsed.scheme not in ("", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid configured server target")
    if parsed.query or parsed.fragment:
        raise ValueError("invalid configured server target")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid configured server target") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid configured server target")
    authority = parsed.netloc
    if not authority:
        raise ValueError("invalid configured server target")
    is_ip = False
    try:
        import ipaddress
        ipaddress.ip_address(parsed.hostname)
        is_ip = True
    except ValueError:
        pass
    return authority, parsed.hostname, is_ip


def _run(command: list[str], *, runner=subprocess.run, **kwargs):
    return runner(command, capture_output=True, timeout=20, check=False, **kwargs)


def _parse_x509(pem: str, cert_path: Path, *, runner=subprocess.run) -> dict[str, str | int]:
    result = _run(
        ["openssl", "x509", "-noout", "-subject", "-issuer", "-dates", "-fingerprint",
         "-sha256", "-ext", "subjectAltName"],
        runner=runner, input=pem, text=True,
    )
    if result.returncode != 0:
        raise ValueError("presented certificate could not be parsed")
    fields: dict[str, str] = {}
    san_output = ""
    in_sans = False
    for line in result.stdout.splitlines():
        if "Subject Alternative Name:" in line:
            in_sans = True
            continue
        if in_sans:
            san_output += f" {line}"
        if "=" in line:
            name, value = line.split("=", 1)
            fields[name.strip()] = value.strip()
    match = re.search(r"(?:SHA256|SHA2-256) Fingerprint\s*=\s*([0-9A-Fa-f:]+)", result.stdout)
    if not match:
        match = re.search(r"Fingerprint=([0-9A-Fa-f:]+)", result.stdout)
    if not match or "notBefore" not in fields or "notAfter" not in fields:
        raise ValueError("presented certificate metadata is incomplete")

    self_signed = False
    if fields.get("subject") == fields.get("issuer"):
        verify = _run(
            ["openssl", "verify", "-check_ss_sig", "-CAfile", str(cert_path), str(cert_path)],
            runner=runner, text=True,
        )
        self_signed = verify.returncode == 0

    return {
        "subject": fields.get("subject", ""),
        "issuer": fields.get("issuer", ""),
        "not_before": fields["notBefore"],
        "not_after": fields["notAfter"],
        "fingerprint": match.group(1).replace(":", "").upper(),
        "dns_sans": len(re.findall(r"\bDNS:", san_output)),
        "ip_sans": len(re.findall(r"\bIP Address:", san_output)),
        "self_signed": self_signed,
    }


def _validity(not_before: str, not_after: str) -> str:
    date_format = "%b %d %H:%M:%S %Y %Z"
    try:
        start = datetime.strptime(not_before, date_format).replace(tzinfo=timezone.utc)
        end = datetime.strptime(not_after, date_format).replace(tzinfo=timezone.utc)
    except ValueError:
        return "UNKNOWN"
    now = datetime.now(timezone.utc)
    if now < start:
        return "NOT YET VALID"
    if now > end:
        return "EXPIRED"
    return "CURRENT"


def inspect_certificate(server: str, *, runner=subprocess.run) -> str:
    authority, hostname, is_ip = parse_server_target(server)
    name_flag = "-checkip" if is_ip else "-checkhost"
    with tempfile.TemporaryDirectory(prefix="monikey-argocd-cert-") as tempdir:
        command = ["openssl", "s_client", "-connect", authority]
        if not is_ip:
            command.extend(["-servername", hostname])
        command.extend(["-showcerts", "-verify_return_error"])
        try:
            result = _run(command, runner=runner, input="", text=True)
        except subprocess.TimeoutExpired:
            return "Argo CD certificate probe failed.\nProbe failure category: network."
        combined_output = f"{result.stdout}\n{result.stderr}"
        pem_chain = CERTIFICATE.findall(result.stdout)
        if not pem_chain:
            category = classify_failure(result.stderr, result.stdout, result.returncode)
            return (
                "Argo CD certificate probe did not return a certificate.\n"
                f"Probe failure category: {category}."
            )

        descriptions = []
        for index, pem in enumerate(pem_chain):
            cert_path = Path(tempdir) / f"presented-{index}.pem"
            cert_path.write_text(pem + "\n", encoding="ascii")
            descriptions.append(_parse_x509(pem, cert_path, runner=runner))

        leaf_path = Path(tempdir) / "presented-0.pem"
        hostname_check = _run(
            ["openssl", "x509", "-in", str(leaf_path), "-noout", name_flag, hostname],
            runner=runner, text=True,
        )
        verify_match = VERIFY_CODE.search(combined_output)
        verify_code = verify_match.group(1) if verify_match else "UNKNOWN"
        leaf = descriptions[0]
        validity = _validity(str(leaf["not_before"]), str(leaf["not_after"]))
        if len(descriptions) == 1:
            chain_observation = "one certificate presented; upstream issuer availability is unresolved"
        else:
            links_resolve = all(
                item["issuer"] in {later["subject"] for later in descriptions[index + 1:]}
                for index, item in enumerate(descriptions[:-1])
            )
            chain_observation = (
                "presented issuer links resolve" if links_resolve
                else "unresolved issuer link; an intermediate may be missing"
            )
        verification_observation = {
            "0": "trusted by the runner's system CA store",
            "18": "self-signed certificate rejected by the runner's system CA store",
            "19": "self-signed certificate in an untrusted chain",
            "20": "issuer unavailable or not trusted; missing intermediate/private CA are both possible",
            "21": "chain could not be verified",
            "62": "hostname mismatch reported by TLS verification",
        }.get(verify_code, f"not verified by the runner's system CA store (code {verify_code})")

        return "\n".join((
            "Argo CD TLS certificate diagnostic (read only)",
            f"Presented certificate count: {len(descriptions)}",
            f"Leaf SHA-256 fingerprint: {leaf['fingerprint']}",
            f"Leaf validity: {validity}",
            f"Leaf hostname check: {'MATCH' if hostname_check.returncode == 0 else 'MISMATCH'}",
            f"Leaf is cryptographically self-signed: {'YES' if leaf['self_signed'] else 'NO'}",
            f"Presented chain: {chain_observation}",
            f"Leaf subject alternative names: DNS={leaf['dns_sans']}, IP={leaf['ip_sans']} (values withheld)",
            f"Runner trust verification: {verification_observation}",
            "Presented issuer/subject names and target address: withheld",
        ))


def main() -> int:
    server = os.environ.get("ARGOCD_SERVER", "")
    try:
        if not server:
            raise ValueError("missing fixed target")
        print(inspect_certificate(server))
    except (OSError, subprocess.SubprocessError, ValueError):
        print("Argo CD certificate diagnostic failed: target or certificate metadata was unavailable.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
