import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "diagnose_argocd_failure.py"
SPEC = importlib.util.spec_from_file_location("diagnose_argocd_failure", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
REPOSITORY = SCRIPT.parents[2]


class ArgoFailureDiagnosticTests(unittest.TestCase):
    def test_classifies_tls_network_authentication_authorization_and_cli(self):
        cases = {
            "TLS": "x509: certificate signed by unknown authority",
            "network": "dial tcp 10.0.0.2:443: i/o timeout",
            "authentication": "rpc error: code = Unauthenticated desc = invalid bearer token",
            "authorization": "rpc error: code = PermissionDenied desc = forbidden",
            "CLI incompatibility": "unknown flag: --output",
        }
        for expected, error in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(module.classify_failure(error), expected)

    def test_classifies_timeout_and_unknown_without_exposing_raw_details(self):
        self.assertEqual(module.classify_failure("", exit_status=124), "network")
        self.assertEqual(module.classify_failure("opaque backend failure"), "unknown")

        private_error = (
            "https://argocd.private.example:8443/session?token=secret-token "
            "Authorization: Bearer private-bearer-value password=secret-password "
            "response body: private user identity"
        )
        output = module.diagnostic(private_error, exit_status=1)
        self.assertEqual(module.classify_failure(private_error), "unknown")
        for sensitive_value in (
            "argocd.private.example", "secret-token", "private-bearer-value",
            "secret-password", "private user identity", "https://",
        ):
            self.assertNotIn(sensitive_value, output)

    def test_cli_prints_only_fixed_diagnostic_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stderr = root / "stderr.txt"
            stdout = root / "stdout.txt"
            stderr.write_text(
                "x509: certificate verify failed for https://hidden.example; token=secret",
                encoding="utf-8",
            )
            stdout.write_text("private response body", encoding="utf-8")
            result = subprocess.run(
                ["python3", str(SCRIPT), str(stderr), str(stdout), "1"],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("category: TLS", result.stdout)
        self.assertNotIn("hidden.example", result.stdout)
        self.assertNotIn("secret", result.stdout)
        self.assertNotIn("private response body", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_bootstrap_uses_redacted_diagnostics_and_keeps_tls_verification(self):
        workflow_path = REPOSITORY / ".github/workflows/monikey-argocd-readonly-token-bootstrap.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        step = next(
            step for step in workflow["jobs"]["bootstrap"]["steps"]
            if step.get("id") == "preflight"
        )
        script = step["run"]
        self.assertIn("run_argocd_read()", script)
        self.assertIn("timeout 30s argocd \"$@\" >", script)
        self.assertIn("account get-user-info --grpc-web --output json", script)
        self.assertIn("diagnose_argocd_failure.py", script)
        self.assertIn('"$stderr_file" "$stdout_file" "$status"', script)
        self.assertIn("trap 'rm -rf \"$work\"' EXIT", script)
        self.assertNotIn("--insecure", script)
        self.assertNotIn('cat "$work/argocd-error.log"', script)
        self.assertNotIn('echo "$(<"$work/argocd-error.log")"', script)


if __name__ == "__main__":
    unittest.main()
