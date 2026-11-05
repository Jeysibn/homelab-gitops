import importlib.util
import json
import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "verify_argocd_permissions.py"
spec = importlib.util.spec_from_file_location("verify_argocd_permissions_diagnostics", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def runner_for(*, username=None, permission_values=None, failure=None, failure_at="identity"):
    permission_values = permission_values or {}

    def runner(command, **_kwargs):
        if "get-user-info" in command:
            if failure and failure_at == "identity":
                if isinstance(failure, BaseException):
                    raise failure
                return subprocess.CompletedProcess(command, 1, "", failure)
            identity = {"username": username or verifier.EXPECTED_USERNAME}
            return subprocess.CompletedProcess(command, 0, json.dumps(identity), "")
        if failure and failure_at == "permission":
            if isinstance(failure, BaseException):
                raise failure
            return subprocess.CompletedProcess(command, 1, "", failure)
        permission = tuple(command[command.index("can-i") + 1:command.index("--grpc-web")])
        default = next(
            expected for action, resource, object_name, expected in verifier.PERMISSION_CHECKS
            if (action, resource, object_name) == permission
        )
        return subprocess.CompletedProcess(command, 0, permission_values.get(permission, default), "")

    return runner


class VerifyArgocdPermissionsDiagnosticTests(unittest.TestCase):
    def test_identity_mismatch_is_sanitized(self):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(username="unexpected-private-identity"))
        self.assertEqual(caught.exception.category, "IDENTITY_MISMATCH")
        self.assertNotIn("unexpected-private-identity", caught.exception.diagnostic())

    def test_required_permission_denial_reports_check_number_expected_and_category(self):
        first = verifier.PERMISSION_CHECKS[0][:3]
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(permission_values={first: "no"}))
        self.assertEqual(caught.exception.category, "REQUIRED_PERMISSION_DENIED")
        self.assertEqual(caught.exception.diagnostic(),
                         "Argo CD permission check 01 expected yes: REQUIRED_PERMISSION_DENIED.")

    def test_unexpected_permission_grant_reports_check_number_expected_and_category(self):
        second = verifier.PERMISSION_CHECKS[1][:3]
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(permission_values={second: "yes"}))
        self.assertEqual(caught.exception.category, "UNEXPECTED_PERMISSION_ALLOWED")
        self.assertEqual(caught.exception.check_number, 2)
        self.assertEqual(caught.exception.expected, "no")

    def test_malformed_permission_response_is_sanitized(self):
        first = verifier.PERMISSION_CHECKS[0][:3]
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(permission_values={first: "yes\nprivate response"}))
        self.assertEqual(caught.exception.category, "INVALID_PERMISSION_RESPONSE")
        self.assertNotIn("private response", caught.exception.diagnostic())
        self.assertEqual(caught.exception.check_number, 1)

    def test_authentication_or_request_error_is_categorized_without_raw_response(self):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(failure="rpc unauthenticated: private response"))
        self.assertEqual(caught.exception.category, "AUTHENTICATION_OR_REQUEST_ERROR")
        self.assertNotIn("private response", caught.exception.diagnostic())

    def test_connection_timeout_is_categorized(self):
        timeout = subprocess.TimeoutExpired(["argocd"], 20, stderr="private timeout detail")
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(failure=timeout))
        self.assertEqual(caught.exception.category, "CONNECTION_TIMEOUT")
        self.assertNotIn("private timeout detail", caught.exception.diagnostic())

        timeout = subprocess.TimeoutExpired(["argocd"], 20, stderr="private timeout detail")
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(failure=timeout, failure_at="permission"))
        self.assertEqual(caught.exception.category, "CONNECTION_TIMEOUT")
        self.assertEqual(caught.exception.check_number, 1)
        self.assertEqual(caught.exception.expected, "yes")

    def test_tls_or_trust_failure_is_categorized_at_permission_check(self):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            verifier.verify(run=runner_for(
                failure="x509: certificate private.invalid is untrusted", failure_at="permission"
            ))
        self.assertEqual(caught.exception.category, "TLS_OR_TRUST_FAILURE")
        self.assertEqual(caught.exception.check_number, 1)
        self.assertEqual(caught.exception.expected, "yes")
        self.assertNotIn("private.invalid", caught.exception.diagnostic())


if __name__ == "__main__":
    unittest.main()
