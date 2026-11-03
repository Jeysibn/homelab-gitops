import base64
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "prepare_argocd_readonly_token", ROOT / "prepare_argocd_readonly_token.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
REPO_ROOT = ROOT.parents[1]


def fake_jwt(lifetime=3600):
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = json.dumps({"iat": 1000, "exp": 1000 + lifetime}).encode()
    payload = base64.urlsafe_b64encode(claims).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def token_response(lifetime=3600, token_id="candidate-run-token"):
    return f"Create token succeeded.\n  ID: {token_id}\n  Token: {fake_jwt(lifetime)}\n"


def project(roles=None):
    return {"metadata": {"name": "default"}, "spec": {"roles": roles or []}}


class ReadonlyTokenBootstrapTests(unittest.TestCase):
    def test_missing_role_plans_fixed_creation(self):
        self.assertEqual(module.role_plan(project()), "create")
        with self.assertRaisesRegex(ValueError, "still absent after creation"):
            module.require_role_ready(project())

    def test_role_ready_accepts_only_the_exact_existing_role(self):
        role = {"name": module.ROLE, "policies": [module.EXPECTED_POLICY], "groups": []}
        self.assertEqual(module.require_role_ready(project([role])), "reuse")

    def test_exact_role_is_reused_without_mutation(self):
        role = {
            "name": "monikey-inspection-reader",
            "policies": [module.EXPECTED_POLICY],
            "groups": [],
        }
        self.assertEqual(module.role_plan(project([role])), "reuse")

    def test_existing_role_with_any_extra_policy_or_group_is_rejected(self):
        cases = [
            {"name": module.ROLE, "policies": [module.EXPECTED_POLICY, "p, extra"], "groups": []},
            {"name": module.ROLE, "policies": [module.EXPECTED_POLICY], "groups": ["wide-group"]},
            {"name": module.ROLE, "policies": [], "groups": []},
        ]
        for role in cases:
            with self.subTest(role=role), self.assertRaises(ValueError):
                module.role_plan(project([role]))

    def test_unknown_or_duplicate_project_role_state_fails_closed(self):
        for document in (
            {},
            {"metadata": {"name": "other"}, "spec": {"roles": []}},
            project([{"name": module.ROLE}, {"name": module.ROLE}]),
            {"metadata": {"name": "default"}, "spec": {"roles": "unknown"}},
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                module.role_plan(document)

    def test_token_extraction_requires_exactly_one_jwt_and_writes_private_file(self):
        token = fake_jwt()
        output = token_response()
        self.assertEqual(module.extract_token(output), token)
        token_value, metadata = module.token_details(output)
        self.assertEqual(token_value, token)
        self.assertEqual(metadata, {"id": "candidate-run-token", "iat": 1000})
        for output in ("Token: short", "ID: unique\nToken: a.b.c\nToken: d.e.f",
                       f"ID: unique\nToken: {fake_jwt(1800)}"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                module.extract_token(output)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "token"
            module.write_private(path, token)
            self.assertEqual(path.read_text().strip(), token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_workflow_is_manual_approved_fixed_target_and_validates_before_storage(self):
        workflow = (REPO_ROOT / ".github/workflows/monikey-argocd-readonly-token-bootstrap.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("github.ref == 'refs/heads/main' && inputs.confirm_readonly_token_bootstrap", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s*(push|pull_request|schedule|workflow_run):")
        self.assertIn("name: monikey-inspection-provisioning", workflow)
        self.assertIn("ARGOCD_AUTH_TOKEN: ${{ secrets.ARGOCD_AUTH_TOKEN }}", workflow)
        self.assertIn("SECRET_WRITER_TOKEN: ${{ secrets.MONIKEY_ENVIRONMENT_SECRET_WRITER_TOKEN }}", workflow)
        self.assertIn("account can-i update projects default", workflow)
        self.assertIn("argocd proj get default --grpc-web --output json", workflow)
        self.assertIn("--action get --permission allow --object monikey-inspection-access", workflow)
        self.assertIn("--expires-in 1h", workflow)
        self.assertIn("verify_argocd_permissions.py", workflow)
        self.assertIn("gh secret set ARGOCD_READONLY_AUTH_TOKEN", workflow)
        self.assertIn("::add-mask::$candidate", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("argocd app sync", workflow)
        self.assertNotIn("kubectl", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertIn('candidate_created=true', workflow)
        self.assertIn("cleanup_argocd_candidate.py", workflow)
        self.assertIn("ARGOCD_AUTH_TOKEN=\"$(<\"$work/candidate-token\")\"", workflow)
        self.assertNotIn('export ARGOCD_AUTH_TOKEN="$(<"$work/candidate-token")"', workflow)
        self.assertNotIn("argocd proj role delete default monikey-inspection-reader", workflow)
        self.assertIn('if [[ "$result" -ne 0 && "$role_created" == true && "$stored" == false ]]', workflow)
        self.assertIn("The project role created by this run was preserved", workflow)

        credential_step = workflow.split("      - name: Require fixed credentials and target", 1)[1].split(
            "      - name: Connect to Tailscale", 1
        )[0]
        declared = set(re.findall(r"^\s{10}([A-Z][A-Z0-9_]+): \$\{\{", credential_step, re.MULTILINE))
        required = set(re.findall(r': "\$\{([A-Z][A-Z0-9_]+):\?', credential_step))
        self.assertEqual(required, {
            "ARGOCD_SERVER", "ARGOCD_AUTH_TOKEN", "SECRET_WRITER_TOKEN",
            "TS_OAUTH_CLIENT_ID", "TS_OAUTH_SECRET",
        })
        self.assertTrue(required.issubset(declared))
        bootstrap_job = workflow.split("  bootstrap:", 1)[1]
        job_environment = bootstrap_job.split("    steps:", 1)[0]
        self.assertNotIn("ARGOCD_AUTH_TOKEN:", job_environment)
        self.assertNotIn("SECRET_WRITER_TOKEN:", job_environment)
        mint_step = bootstrap_job.split("      - name: Create only the fixed role", 1)[1]
        self.assertIn("ARGOCD_AUTH_TOKEN: ${{ secrets.ARGOCD_AUTH_TOKEN }}", mint_step)
        self.assertIn("SECRET_WRITER_TOKEN: ${{ secrets.MONIKEY_ENVIRONMENT_SECRET_WRITER_TOKEN }}", mint_step)

        preflight = workflow.split("      - name: Inspect current authority", 1)[1].split(
            "  - name: Create only the fixed role", 1
        )[0]
        write_index = workflow.index("argocd proj role create default")
        self.assertLess(workflow.index("account can-i update projects default"), write_index)
        self.assertLess(workflow.index('run_argocd_read "$work/default-project.json"'), write_index)
        mint = workflow.split("      - name: Create only the fixed role", 1)[1]
        self.assertIn('role-ready "$work/project-after-write.json"', mint)
        self.assertIn('[[ "$role_plan_after" == reuse ]]', mint)
        self.assertLess(mint.index("verify_argocd_permissions.py"), mint.index("gh secret set"))
        self.assertIn('if [[ "$result" -ne 0 && "$candidate_created" == true && "$stored" == false ]]', mint)
        self.assertIn('if [[ "$result" -ne 0 && "$role_created" == true && "$stored" == false ]]', mint)
        self.assertLess(mint.index('candidate_created=true'), mint.index('verify_argocd_permissions.py'))
        self.assertLess(mint.index('trap cleanup EXIT'), mint.index('Candidate token failed'))
        secret_failure = mint.index('Could not store the verified token')
        self.assertLess(secret_failure, mint.index('stored=true'))


if __name__ == "__main__":
    unittest.main()
