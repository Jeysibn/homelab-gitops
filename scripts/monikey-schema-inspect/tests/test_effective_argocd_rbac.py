"""Cross-identity static authorization tests; no token-only probe mocks."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_verify_argocd_permissions import (
    verifier, evidence_document, runner_for, EXPECTED_CONTEXT, EXPECTED_API_SERVER,
)

IDENTITY = verifier.EXPECTED_USERNAME
OTHER = "proj:default:other"


def other_role(policies=None, groups=None):
    return {"name": "other", "policies": policies or [], "groups": groups or []}


class EffectiveArgocdRbacTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "evidence.json"

    def validate(self, document, **kwargs):
        self.path.write_text(json.dumps(document))
        return verifier.validate_rbac_evidence(
            str(self.path), expected_context=EXPECTED_CONTEXT,
            expected_api_server=EXPECTED_API_SERVER, **kwargs,
        )

    def rejected(self, document, category="RBAC_POLICY_UNSAFE", **kwargs):
        with self.assertRaises(verifier.VerificationFailure) as caught:
            self.validate(document, **kwargs)
        self.assertEqual(caught.exception.category, category)

    def roles(self, *additional):
        return evidence_document()["appProject"]["spec"]["roles"] + list(additional)

    def test_external_direct_inheritance_without_global_permission(self):
        self.rejected(evidence_document(f"g, attacker, {IDENTITY}"))

    def test_external_direct_inheritance_with_global_permission(self):
        self.rejected(evidence_document(f"g, attacker, {IDENTITY}\n{verifier.EXPECTED_PROJECT_POLICY}"))

    def test_external_transitive_inheritance(self):
        self.rejected(evidence_document(f"g, attacker, role:middle\ng, role:middle, {IDENTITY}"))

    def test_external_inheritance_split_across_fragments_including_empty_name(self):
        self.rejected(evidence_document(extra_data={
            "policy..csv": "g, attacker, role:one", "policy.z.csv": f"g, role:one, {IDENTITY}",
        }))

    def test_project_groups_assign_external_identity_to_inspection_role(self):
        roles = self.roles()
        roles[0]["groups"] = ["attacker"]
        self.rejected(evidence_document(project_roles=roles))

    def test_project_groups_map_inspection_identity_to_privileged_role(self):
        self.rejected(evidence_document(project_roles=self.roles(other_role(
            [f"p, {OTHER}, applications, sync, default/*, allow"], [IDENTITY],
        ))))

    def test_inspection_inherits_app_project_mutation(self):
        self.rejected(evidence_document(f"g, {IDENTITY}, {OTHER}", project_roles=self.roles(
            other_role([f"p, {OTHER}, applications, update, default/*, allow"]),
        )))

    def test_inspection_inherits_app_project_unauthorized_reads(self):
        for resource, obj in (("applications", "default/other"), ("projects", "default"),
                              ("applicationsets", "default/*"), ("clusters", "*")):
            with self.subTest(resource=resource):
                self.rejected(evidence_document(f"g, {IDENTITY}, {OTHER}", project_roles=self.roles(
                    other_role([f"p, {OTHER}, {resource}, get, {obj}, allow"]),
                )))

    def test_multihop_inspection_inheritance_reaches_privilege(self):
        self.rejected(evidence_document(
            f"g, {IDENTITY}, role:one\ng, role:one, role:two\ng, role:two, {OTHER}",
            project_roles=self.roles(other_role([f"p, {OTHER}, applications, delete, default/*, allow"])),
        ))

    def test_inheritance_crosses_global_and_project_policy_records(self):
        self.rejected(evidence_document(f"g, {IDENTITY}, role:middle", project_roles=self.roles(other_role([
            f"g, role:middle, {OTHER}", f"p, {OTHER}, applications, sync, default/*, allow",
        ]))))

    def test_external_inheritance_crosses_project_and_global_records(self):
        self.rejected(evidence_document(f"g, {OTHER}, {IDENTITY}", project_roles=self.roles(other_role(
            [], ["attacker"],
        ))))

    def test_isolated_broad_project_role_is_accepted_by_isolation_validator(self):
        document = evidence_document(project_roles=self.roles(other_role(
            [f"p, {OTHER}, applications, *, default/*, allow"], ["other-team"],
        )))
        self.assertEqual(self.validate(document), document)
        # No glob matching of subjects: similar-looking names are different.
        document["configMap"]["data"]["policy.csv"] = f"g, attacker, {IDENTITY}-different"
        self.assertEqual(self.validate(document), document)

    def test_authorized_application_read_and_implicit_project_read_are_reported(self):
        doc = evidence_document(verifier.EXPECTED_PROJECT_POLICY)
        self.assertEqual(self.validate(doc), doc)
        effective = verifier.validate_composed_policy(doc["configMap"]["data"], doc["appProject"])
        self.assertEqual(effective, frozenset({
            ("applications", "get", verifier.APPLICATION_OBJECT), ("projects", "get", "default"),
        }))

    def test_project_policies_cannot_hide_permission_on_inspection_subject(self):
        self.rejected(evidence_document(project_roles=self.roles(other_role([
            f"p, {IDENTITY}, applications, sync, default/*, allow",
        ]))))

    def test_deny_does_not_excuse_unsafe_inheritance(self):
        self.rejected(evidence_document(
            f"g, attacker, {IDENTITY}\np, attacker, applications, get, *, deny",
        ))

    def test_global_allowlist_defaults_and_builtin_inheritance_remain_strict(self):
        for policy, extra in (
            (f"p, {IDENTITY}, projects, get, default, allow", {}),
            ("g, unrelated, role:readonly", {}), ("g, unrelated, role:admin", {}),
            ("", {"policy.default": "role:readonly"}),
        ):
            with self.subTest(policy=policy):
                self.rejected(evidence_document(policy, extra_data=extra))

    def test_cross_policy_cycle_fails_closed_as_structure_error(self):
        self.rejected(evidence_document("g, role:one, role:two", project_roles=self.roles(other_role([
            "g, role:two, role:one",
        ]))), "RBAC_POLICY_VALIDATION")

    def test_deep_inheritance_fails_closed_without_recursion_error(self):
        policy = "\n".join(f"g, role:{n}, role:{n+1}" for n in range(1100))
        self.rejected(evidence_document(policy + f"\ng, role:1100, {IDENTITY}"))

    def test_malformed_unrelated_roles_cannot_be_discarded(self):
        for role in (None, {"name": "other", "groups": {}}, {"name": "other", "policies": "bad"},
                     {"name": "bad:name"}, {"name": "other", "policies": [None]}):
            with self.subTest(role=role):
                self.rejected(evidence_document(project_roles=self.roles(role)), "APP_PROJECT_VALIDATION")
        self.rejected(evidence_document(project_roles=self.roles(other_role(["p, malformed"]))),
                      "RBAC_POLICY_VALIDATION")
        self.rejected(evidence_document(project_roles=self.roles(other_role(), other_role())),
                      "APP_PROJECT_VALIDATION")

    def test_unsafe_csv_injection_in_runtime_groups_fails_closed(self):
        self.rejected(evidence_document(project_roles=self.roles(other_role([], [f"user, {IDENTITY}\ng, attacker"]))))

    def test_precreation_evidence_checks_inheritance_without_inspection_role(self):
        self.rejected(evidence_document(f"g, attacker, {IDENTITY}", project_roles=[]), require_project_role=False)

    def test_postwrite_project_override_is_composed_with_global_mappings(self):
        self.validate(evidence_document(f"g, {IDENTITY}, {OTHER}", project_roles=[]), require_project_role=False)
        path = Path(self.tempdir.name) / "project.json"
        path.write_text(json.dumps(evidence_document(project_roles=self.roles(other_role([
            f"p, {OTHER}, applications, sync, default/*, allow",
        ])))["appProject"]))
        with self.assertRaisesRegex(verifier.VerificationFailure, "RBAC_POLICY_UNSAFE"):
            verifier.verify(run=runner_for(), rbac_evidence_file=str(self.path), project_json_file=str(path),
                            expected_context=EXPECTED_CONTEXT, expected_api_server=EXPECTED_API_SERVER)

    def test_wildcard_denial_cannot_mask_exact_project_read(self):
        self.validate(evidence_document())
        self.assertIn(("get", "projects", "*", "no"), verifier.PERMISSION_CHECKS)
        self.assertIn(("get", "projects", "default", "no"), verifier.PERMISSION_CHECKS)
        with self.assertRaisesRegex(verifier.VerificationFailure, "UNEXPECTED_PERMISSION_ALLOWED"):
            verifier.verify(run=runner_for(permission_values={("get", "projects", "default"): "yes"}),
                            rbac_evidence_file=str(self.path), expected_context=EXPECTED_CONTEXT,
                            expected_api_server=EXPECTED_API_SERVER)

    def test_legacy_target_role_only_evidence_requires_recapture(self):
        for version in (None, 1, "2"):
            document = evidence_document()
            if version is None:
                del document["schemaVersion"]
            else:
                document["schemaVersion"] = version
            self.rejected(document, "RBAC_EVIDENCE_INVALID")

    def test_compensating_deny_does_not_silently_approve_contract_change(self):
        document = evidence_document(f"p, {IDENTITY}, projects, get, default, deny")
        self.validate(document)
        with self.assertRaisesRegex(verifier.VerificationFailure, "RBAC_IMPLICIT_PROJECT_READ_BLOCKER"):
            verifier.verify(run=runner_for(), rbac_evidence_file=str(self.path),
                            expected_context=EXPECTED_CONTEXT, expected_api_server=EXPECTED_API_SERVER)

    def test_cli_preflight_blocks_before_any_provisioning_even_without_role(self):
        self.validate(evidence_document(project_roles=[]), require_project_role=False)
        result = subprocess.run([
            "python3", str(verifier.__file__), "--check-rbac-evidence", "--rbac-evidence-file", str(self.path),
            "--expected-kubernetes-context", EXPECTED_CONTEXT,
            "--expected-kubernetes-api-server", EXPECTED_API_SERVER,
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("RBAC_IMPLICIT_PROJECT_READ_BLOCKER", result.stdout)
        self.assertEqual(result.stderr, "")
