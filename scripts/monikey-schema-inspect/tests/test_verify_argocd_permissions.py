import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "verify_argocd_permissions.py"
EXPECTED_CONTEXT = "default"
EXPECTED_API_SERVER = "https://127.0.0.1:6443"
spec = importlib.util.spec_from_file_location("verify_argocd_permissions_diagnostics", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def evidence_document(policy_csv="", *, project_roles=None, captured_at=None, extra_data=None):
    data = {"policy.csv": policy_csv}
    data.update(extra_data or {})
    return {
        "capturedAt": captured_at or datetime.now(timezone.utc).isoformat(),
        "cluster": {"context": EXPECTED_CONTEXT, "apiServer": EXPECTED_API_SERVER},
        "appProject": {
            "metadata": {
                "name": "default", "namespace": "argocd", "uid": "project-uid",
                "resourceVersion": "12",
            },
            "spec": {"roles": project_roles if project_roles is not None else [{
                "name": "monikey-inspection-reader",
                "policies": [verifier.EXPECTED_PROJECT_POLICY],
                "groups": [],
            }]},
        },
        "configMap": {
            "metadata": {
                "name": "argocd-rbac-cm", "namespace": "argocd", "uid": "rbac-uid",
                "resourceVersion": "34",
            },
            "data": data,
        },
    }


def runner_for(*, username=None, groups=None, permission_values=None, failure=None, failure_at="identity"):
    permission_values = permission_values or {}

    def runner(command, **_kwargs):
        if "get-user-info" in command:
            if failure and failure_at == "identity":
                if isinstance(failure, BaseException):
                    raise failure
                return subprocess.CompletedProcess(command, 1, "", failure)
            identity = {"username": username or verifier.EXPECTED_USERNAME, "groups": groups or []}
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
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.evidence_path = Path(self.tempdir.name) / "evidence.json"
        self.write_evidence(evidence_document())

    def write_evidence(self, document):
        self.evidence_path.write_text(json.dumps(document), encoding="utf-8")

    def verify(self, runner):
        verifier.verify(
            run=runner,
            rbac_evidence_file=str(self.evidence_path),
            expected_context=EXPECTED_CONTEXT,
            expected_api_server=EXPECTED_API_SERVER,
        )

    def validate(self, path=None, **kwargs):
        return verifier.validate_rbac_evidence(
            path if path is not None else str(self.evidence_path),
            expected_context=EXPECTED_CONTEXT,
            expected_api_server=EXPECTED_API_SERVER,
            **kwargs,
        )

    def test_identity_mismatch_is_sanitized(self):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(username="unexpected-private-identity"))
        self.assertEqual(caught.exception.category, "IDENTITY_MISMATCH")
        self.assertNotIn("unexpected-private-identity", caught.exception.diagnostic())

    def test_required_permission_denial_reports_check_number_expected_and_category(self):
        first = verifier.PERMISSION_CHECKS[0][:3]
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(permission_values={first: "no"}))
        self.assertEqual(caught.exception.category, "REQUIRED_PERMISSION_DENIED")
        self.assertEqual(caught.exception.diagnostic(),
                         "Argo CD permission check 01 expected yes: REQUIRED_PERMISSION_DENIED.")

    def test_unexpected_permission_grant_reports_check_number_expected_and_category(self):
        second = verifier.PERMISSION_CHECKS[1][:3]
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(permission_values={second: "yes"}))
        self.assertEqual(caught.exception.category, "UNEXPECTED_PERMISSION_ALLOWED")
        self.assertEqual(caught.exception.check_number, 2)
        self.assertEqual(caught.exception.expected, "no")

    def test_broad_application_read_is_denied(self):
        check = next(check for check in verifier.PERMISSION_CHECKS if check[:3] == ("get", "applications", "*/*"))
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(permission_values={check[:3]: "yes"}))
        self.assertEqual(caught.exception.category, "UNEXPECTED_PERMISSION_ALLOWED")
        self.assertEqual(caught.exception.expected, "no")

    def test_malformed_permission_response_is_sanitized(self):
        first = verifier.PERMISSION_CHECKS[0][:3]
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(permission_values={first: "yes\nprivate response"}))
        self.assertEqual(caught.exception.category, "INVALID_PERMISSION_RESPONSE")
        self.assertNotIn("private response", caught.exception.diagnostic())
        self.assertEqual(caught.exception.check_number, 1)

    def test_authentication_or_request_error_is_categorized_without_raw_response(self):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(failure="rpc unauthenticated: private response"))
        self.assertEqual(caught.exception.category, "AUTHENTICATION_OR_REQUEST_ERROR")
        self.assertNotIn("private response", caught.exception.diagnostic())

    def test_connection_timeout_is_categorized(self):
        timeout = subprocess.TimeoutExpired(["argocd"], 20, stderr="private timeout detail")
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(failure=timeout))
        self.assertEqual(caught.exception.category, "CONNECTION_TIMEOUT")
        self.assertNotIn("private timeout detail", caught.exception.diagnostic())

        timeout = subprocess.TimeoutExpired(["argocd"], 20, stderr="private timeout detail")
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(failure=timeout, failure_at="permission"))
        self.assertEqual(caught.exception.category, "CONNECTION_TIMEOUT")
        self.assertEqual(caught.exception.check_number, 1)
        self.assertEqual(caught.exception.expected, "yes")

    def test_tls_or_trust_failure_is_categorized_at_permission_check(self):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(
                failure="x509: certificate private.invalid is untrusted", failure_at="permission"
            ))
        self.assertEqual(caught.exception.category, "TLS_OR_TRUST_FAILURE")
        self.assertEqual(caught.exception.check_number, 1)
        self.assertEqual(caught.exception.expected, "yes")
        self.assertNotIn("private.invalid", caught.exception.diagnostic())

    def test_every_live_can_i_probe_uses_v351_supported_values(self):
        self.assertEqual(verifier.SUPPORTED_ACTIONS, {
            "get", "create", "update", "delete", "sync", "override", "action", "invoke",
        })
        self.assertEqual(verifier.SUPPORTED_RESOURCES, {
            "clusters", "projects", "applications", "applicationsets", "repositories",
            "write-repositories", "certificates", "accounts", "gpgkeys", "logs", "exec", "extensions",
        })
        for action, resource, _object_name, _expected in verifier.PERMISSION_CHECKS:
            with self.subTest(action=action, resource=resource):
                self.assertIn(action, verifier.SUPPORTED_ACTIONS)
                self.assertIn(resource, verifier.SUPPORTED_RESOURCES)

    def test_sync_denial_covers_rollback_in_v351(self):
        self.assertIn(("sync", "applications", verifier.APPLICATION_OBJECT, "no"),
                      verifier.PERMISSION_CHECKS)
        self.assertFalse(any(action == "rollback" for action, *_ in verifier.PERMISSION_CHECKS))

    def test_project_scoped_broad_checks_use_project_object_wildcards(self):
        for action, resource, object_name, _expected in verifier.PERMISSION_CHECKS:
            if resource in {"applications", "applicationsets", "exec"} and object_name in {"*", "*/*"}:
                with self.subTest(action=action, resource=resource):
                    self.assertEqual(object_name, "*/*")

    def test_fine_grained_mutation_grants_block_verification(self):
        policies = (
            "p, role:readonly, applications, update/*, *, allow",
            "p, role:unrelated, applications, delete/spec/replicas, *, allow",
            "p, role:unrelated, *, action/extensions/*, *, allow",
            "p, role:readonly, applications, update*, *, allow",
        )
        for policy in policies:
            with self.subTest(policy="[policy withheld]"):
                document = evidence_document(policy)
                self.write_evidence(document)
                with self.assertRaises(verifier.VerificationFailure) as caught:
                    self.validate()
                self.assertEqual(caught.exception.category, "RBAC_EVIDENCE_UNSAFE")

    def test_project_role_extra_policy_or_group_blocks_verification(self):
        cases = (
            {"name": "monikey-inspection-reader", "policies": [verifier.EXPECTED_PROJECT_POLICY],
             "groups": ["unexpected-group"]},
            {"name": "monikey-inspection-reader", "policies": [verifier.EXPECTED_PROJECT_POLICY, "extra"],
             "groups": []},
        )
        for role in cases:
            with self.subTest(role="[role withheld]"):
                self.write_evidence(evidence_document(project_roles=[role]))
                with self.assertRaises(verifier.VerificationFailure) as caught:
                    self.validate()
                self.assertEqual(caught.exception.category, "RBAC_EVIDENCE_INVALID")

        duplicate = cases[1]
        self.write_evidence(evidence_document(project_roles=[duplicate, duplicate]))
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.validate()
        self.assertEqual(caught.exception.category, "RBAC_EVIDENCE_INVALID")

    def test_missing_stale_and_malformed_global_evidence_fail_closed(self):
        with self.assertRaises(verifier.VerificationFailure) as missing:
            verifier.validate_rbac_evidence(
                None, expected_context=EXPECTED_CONTEXT, expected_api_server=EXPECTED_API_SERVER
            )
        self.assertEqual(missing.exception.category, "RBAC_EVIDENCE_UNAVAILABLE")

        with self.assertRaises(verifier.VerificationFailure) as blocked:
            verifier.verify(run=lambda *_args, **_kwargs: self.fail("Argo CD must not be queried without evidence"))
        self.assertEqual(blocked.exception.category, "RBAC_EVIDENCE_UNAVAILABLE")

        stale = evidence_document(captured_at="2020-01-01T00:00:00+00:00")
        self.write_evidence(stale)
        with self.assertRaises(verifier.VerificationFailure) as expired:
            self.validate()
        self.assertEqual(expired.exception.category, "RBAC_EVIDENCE_UNAVAILABLE")

        malformed = evidence_document("p, broken")
        self.write_evidence(malformed)
        with self.assertRaises(verifier.VerificationFailure) as invalid:
            self.validate()
        self.assertEqual(invalid.exception.category, "RBAC_EVIDENCE_INVALID")

    def test_group_claims_and_non_glob_policy_mode_fail_closed(self):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.verify(runner_for(groups=["unexpected-group"]))
        self.assertEqual(caught.exception.category, "IDENTITY_MISMATCH")

        self.write_evidence(evidence_document(extra_data={"policy.matchMode": "regex"}))
        with self.assertRaises(verifier.VerificationFailure) as invalid:
            self.validate()
        self.assertEqual(invalid.exception.category, "RBAC_EVIDENCE_INVALID")

        self.write_evidence(evidence_document(extra_data={"policy.default": "role:admin"}))
        with self.assertRaises(verifier.VerificationFailure) as unsafe:
            self.validate()
        self.assertEqual(unsafe.exception.category, "RBAC_EVIDENCE_UNSAFE")

        self.write_evidence(evidence_document(extra_data={"policy.default": "role:readonly"}))
        with self.assertRaises(verifier.VerificationFailure) as broad_read:
            self.validate()
        self.assertEqual(broad_read.exception.category, "RBAC_EVIDENCE_UNSAFE")

    def test_evidence_must_match_approved_cluster_identity(self):
        document = evidence_document()
        document["cluster"]["context"] = "other-cluster"
        self.write_evidence(document)
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.validate()
        self.assertEqual(caught.exception.category, "RBAC_EVIDENCE_INVALID")


if __name__ == "__main__":
    unittest.main()
