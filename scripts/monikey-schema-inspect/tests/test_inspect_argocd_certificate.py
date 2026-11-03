import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import json
import subprocess


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "inspect_argocd_certificate", SCRIPT_DIR / "inspect_argocd_certificate.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
REPOSITORY = SCRIPT_DIR.parents[1]


class FakeOpenSSL:
    def __init__(self, *, hostname_matches=False, verify_code="18"):
        self.hostname_matches = hostname_matches
        self.verify_code = verify_code
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[1:3] == ["s_client", "-connect"]:
            return SimpleNamespace(
                returncode=1,
                stdout=(
                    "Certificate chain\n-----BEGIN CERTIFICATE-----\nZmFrZQ==\n"
                    f"-----END CERTIFICATE-----\nVerify return code: {self.verify_code} (fixture)\n"
                ),
                stderr="certificate failure for internal.private.example",
            )
        if command[1:3] == ["x509", "-noout"]:
            return SimpleNamespace(returncode=0, stdout=(
                "subject=CN = private-argocd.internal\n"
                "issuer=CN = private-argocd.internal\n"
                "notBefore=Jan  1 00:00:00 2026 GMT\n"
                "notAfter=Jan  1 00:00:00 2028 GMT\n"
                "sha256 Fingerprint=AA:BB:CC:DD\n"
                "X509v3 Subject Alternative Name:\n    DNS:private-argocd.internal, IP Address:10.5.0.4\n"
            ), stderr="")
        if command[1:3] == ["verify", "-check_ss_sig"]:
            return SimpleNamespace(returncode=0, stdout="fixture: OK\n", stderr="")
        if "-checkip" in command or "-checkhost" in command:
            return SimpleNamespace(
                returncode=0 if self.hostname_matches else 1,
                stdout="hostname response includes private name\n",
                stderr="",
            )
        raise AssertionError(f"unexpected OpenSSL command shape: {command[1:3]}")


class ArgoCertificateDiagnosticTests(unittest.TestCase):
    def test_rejects_userinfo_and_non_tls_targets(self):
        for target in (
            "user:password@argocd.example:443",
            "http://argocd.example:443",
            "https://argocd.example:443?token=secret",
            "https://argocd.example:443#fragment",
        ):
            with self.subTest(target="[redacted]"), self.assertRaises(ValueError):
                module.parse_server_target(target)

    def test_report_contains_metadata_but_withholds_endpoint_names_and_sans(self):
        runner = FakeOpenSSL(hostname_matches=True)
        report = module.inspect_certificate("https://private-argocd.internal:443", runner=runner)
        self.assertIn("Presented certificate count: 1", report)
        self.assertIn("Leaf SHA-256 fingerprint: AABBCCDD", report)
        self.assertIn("Leaf hostname check: MATCH", report)
        self.assertIn("Leaf is cryptographically self-signed: YES", report)
        self.assertIn("Runner trust verification: self-signed certificate rejected", report)
        self.assertIn("DNS=1, IP=1 (values withheld)", report)
        for value in (
            "private-argocd.internal", "10.5.0.4", "internal.private.example", "https://",
        ):
            self.assertNotIn(value, report)
        self.assertTrue(any("-verify_return_error" in command for command in runner.commands))
        self.assertFalse(any("-insecure" in command for command in runner.commands))

    def test_real_openssl_x509_metadata_is_parsed_without_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            certificate = root / "fixture-cert.pem"
            key = root / "fixture-key.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(certificate), "-days", "1",
                 "-subj", "/CN=private-fixture.invalid",
                 "-addext", "subjectAltName=IP:127.0.0.1"],
                capture_output=True, check=True,
            )
            pem = certificate.read_text(encoding="ascii")

            def local_openssl(command, **kwargs):
                if command[1] == "s_client":
                    return SimpleNamespace(
                        returncode=1,
                        stdout=f"{pem}\nVerify return code: 18 (self-signed certificate)\n",
                        stderr="TLS certificate verification failed",
                    )
                return subprocess.run(command, **kwargs)

            report = module.inspect_certificate("127.0.0.1:443", runner=local_openssl)
        self.assertIn("Leaf hostname check: MATCH", report)
        self.assertIn("Leaf is cryptographically self-signed: YES", report)
        self.assertIn("DNS=0, IP=1", report)
        self.assertNotIn("private-fixture.invalid", report)
        self.assertNotIn("127.0.0.1", report)

    def test_hostname_mismatch_and_validity_boundaries_are_reported(self):
        report = module.inspect_certificate("127.0.0.1:443", runner=FakeOpenSSL())
        self.assertIn("Leaf hostname check: MISMATCH", report)
        self.assertEqual(module._validity("Jan  1 00:00:00 2020 GMT", "Jan  1 00:00:00 2021 GMT"), "EXPIRED")
        self.assertEqual(module._validity("Jan  1 00:00:00 2030 GMT", "Jan  1 00:00:00 2031 GMT"), "NOT YET VALID")

        untrusted = module.inspect_certificate("argocd.private:443", runner=FakeOpenSSL(verify_code="20"))
        self.assertIn("missing intermediate/private CA are both possible", untrusted)

    def test_no_certificate_output_classifies_probe_without_echoing_error(self):
        def failed_probe(command, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="dial tcp 10.0.0.3:443: connection refused",
            )

        report = module.inspect_certificate("argocd.private:443", runner=failed_probe)
        self.assertIn("Probe failure category: network", report)
        self.assertNotIn("10.0.0.3", report)
        self.assertNotIn("argocd.private", report)

    def test_certificate_workflow_is_manual_protected_and_read_only(self):
        import yaml

        path = REPOSITORY / ".github/workflows/monikey-argocd-certificate-diagnostic.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        job = workflow["jobs"]["inspect"]
        triggers = workflow.get("on", workflow.get(True, {}))
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        self.assertIn("github.ref == 'refs/heads/main' && inputs.confirm_certificate_diagnostic", job["if"])
        self.assertEqual(job["environment"]["name"], "monikey-inspection-provisioning")
        self.assertNotIn("ARGOCD_AUTH_TOKEN", job["env"])
        script = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("inspect_argocd_certificate.py", script)
        self.assertNotIn("argocd app sync", script)
        self.assertNotIn("kubectl", script)
        self.assertNotIn("--insecure", script)

    def test_bootstrap_only_uses_optional_trust_file_with_server_crt(self):
        import yaml

        path = REPOSITORY / ".github/workflows/monikey-argocd-readonly-token-bootstrap.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        job_script = "\n".join(step.get("run", "") for step in workflow["jobs"]["bootstrap"]["steps"])
        self.assertIn("ARGOCD_SERVER_TRUST_CERT_B64", path.read_text(encoding="utf-8"))
        self.assertIn("prepare_argocd_server_crt.py", job_script)
        self.assertIn("--server-crt", job_script)
        self.assertNotIn("--insecure", job_script)
        self.assertIn("monikey-argocd-server-trust.pem", job_script)

    def test_permission_verifier_uses_server_crt_for_every_argocd_call(self):
        verifier_path = SCRIPT_DIR / "verify_argocd_permissions.py"
        verifier_spec = importlib.util.spec_from_file_location("verify_argocd_permissions", verifier_path)
        verifier = importlib.util.module_from_spec(verifier_spec)
        verifier_spec.loader.exec_module(verifier)
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if "get-user-info" in command:
                output = json.dumps({"username": verifier.EXPECTED_USERNAME})
            else:
                check = command[command.index("can-i") + 1:]
                action, resource, object_name = check[:3]
                expected = next(
                    value for candidate_action, candidate_resource, candidate_object, value
                    in verifier.PERMISSION_CHECKS
                    if (action, resource, object_name)
                    == (candidate_action, candidate_resource, candidate_object)
                )
                output = expected
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        verifier.verify(run=runner, server_crt="/runner/temp/trust.pem")
        self.assertTrue(calls)
        self.assertTrue(all(
            command[1:3] == ["--server-crt", "/runner/temp/trust.pem"]
            for command in calls
        ))


if __name__ == "__main__":
    unittest.main()
