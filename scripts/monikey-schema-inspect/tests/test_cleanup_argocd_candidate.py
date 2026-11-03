import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "cleanup_argocd_candidate", ROOT / "cleanup_argocd_candidate.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ADMIN_TOKEN = "admin-credential-test-only"
CANDIDATE_TOKEN = "candidate-credential-test-only"
METADATA = {"id": "candidate-run-id", "iat": 1234567890}


def project(records=None):
    return {
        "metadata": {"name": "default"},
        "spec": {"roles": [{
            "name": module.ROLE,
            "jwtTokens": records if records is not None else [
                {"id": METADATA["id"], "iat": METADATA["iat"], "exp": METADATA["iat"] + 3600}
            ],
        }]},
    }


class FakeRunner:
    def __init__(self, document=None, failing_command=None):
        self.document = document if document is not None else project()
        self.failing_command = failing_command
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == self.failing_command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="fictional failure")
        if command[:4] == ["argocd", "proj", "get", "default"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(self.document), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class CandidateTokenCleanupTests(unittest.TestCase):
    def assert_admin_only_revoke(self, runner):
        self.assertEqual(len(runner.calls), 2)
        get_command, get_kwargs = runner.calls[0]
        delete_command, delete_kwargs = runner.calls[1]
        self.assertEqual(get_command[:4], ["argocd", "proj", "get", "default"])
        self.assertEqual(delete_command, [
            "argocd", "proj", "role", "delete-token", "default",
            "monikey-inspection-reader", str(METADATA["iat"]), "--grpc-web",
        ])
        for _, kwargs in runner.calls:
            self.assertEqual(kwargs["env"]["ARGOCD_AUTH_TOKEN"], ADMIN_TOKEN)
            self.assertNotIn(CANDIDATE_TOKEN, kwargs["env"].values())
        self.assertNotIn("delete", " ".join(get_command))

    def test_failed_candidate_verification_revokes_only_candidate_with_admin_auth(self):
        runner = FakeRunner()
        self.assertTrue(module.revoke_candidate(METADATA, runner=runner, environ={
            "ARGOCD_AUTH_TOKEN": ADMIN_TOKEN,
        }))
        self.assert_admin_only_revoke(runner)

    def test_failed_secret_storage_revokes_only_candidate_with_admin_auth(self):
        runner = FakeRunner()
        # The same EXIT cleanup runs after either failed verification or failed secret storage.
        self.assertTrue(module.revoke_candidate(METADATA, runner=runner, environ={
            "ARGOCD_AUTH_TOKEN": ADMIN_TOKEN,
        }))
        self.assert_admin_only_revoke(runner)

    def test_ambiguous_issued_at_does_not_revoke_any_token_or_role(self):
        runner = FakeRunner(document=project([
            {"id": METADATA["id"], "iat": METADATA["iat"]},
            {"id": "unrelated-id", "iat": METADATA["iat"]},
        ]))
        self.assertFalse(module.revoke_candidate(METADATA, runner=runner, environ={
            "ARGOCD_AUTH_TOKEN": ADMIN_TOKEN,
        }))
        self.assertEqual(len(runner.calls), 1)

    def test_missing_admin_auth_or_project_metadata_fails_closed(self):
        runner = FakeRunner()
        self.assertFalse(module.revoke_candidate(METADATA, runner=runner, environ={}))
        self.assertEqual(runner.calls, [])
        missing_metadata = FakeRunner(document={
            "metadata": {"name": "default"}, "spec": {"roles": []}
        })
        self.assertFalse(module.revoke_candidate(METADATA, runner=missing_metadata, environ={
            "ARGOCD_AUTH_TOKEN": ADMIN_TOKEN,
        }))
        self.assertEqual(len(missing_metadata.calls), 1)

    def test_cleanup_never_deletes_the_project_role(self):
        runner = FakeRunner()
        self.assertTrue(module.revoke_candidate(METADATA, runner=runner, environ={
            "ARGOCD_AUTH_TOKEN": ADMIN_TOKEN,
        }))
        joined = [" ".join(command) for command, _ in runner.calls]
        self.assertFalse(any("proj role delete default" in command for command in joined))
        self.assertTrue(any("proj role delete-token default monikey-inspection-reader" in command
                            for command in joined))

    def test_workflow_exit_cleanup_keeps_admin_auth_on_verification_and_storage_failures(self):
        repository = ROOT.parents[1]
        workflow = repository / ".github/workflows/monikey-argocd-readonly-token-bootstrap.yml"
        import yaml

        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        mint_step = next(step for step in document["jobs"]["bootstrap"]["steps"]
                         if step.get("id") == "mint")
        script = mint_step["run"]
        start = script.index("cleanup() {")
        end = script.index("\ntrap cleanup EXIT", start)
        cleanup_function = script[start:end]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "monikey-argocd-token-bootstrap"
            work.mkdir(mode=0o700)
            (work / "candidate-metadata.json").write_text(json.dumps(METADATA), encoding="utf-8")
            audit = root / "argocd-calls.txt"
            fake_argocd = root / "argocd"
            fake_argocd.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "assert os.environ.get('ARGOCD_AUTH_TOKEN') == 'admin-credential-test-only'\n"
                "assert 'CANDIDATE_TOKEN' not in os.environ\n"
                f"pathlib.Path({str(audit)!r}).open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1:4] == ['proj', 'get', 'default']:\n"
                f" print(json.dumps({project()!r}))\n"
                "elif sys.argv[1:4] == ['proj', 'role', 'delete-token']:\n"
                " pass\n"
                "else:\n"
                " raise SystemExit(9)\n",
                encoding="utf-8",
            )
            fake_argocd.chmod(0o700)

            for stage in ("verification", "secret-storage"):
                with self.subTest(stage=stage):
                    audit.unlink(missing_ok=True)
                    work.mkdir(mode=0o700, exist_ok=True)
                    (work / "candidate-metadata.json").write_text(
                        json.dumps(METADATA), encoding="utf-8"
                    )
                    harness = "\n".join((
                        "set -euo pipefail",
                        f"work={str(work)!r}",
                        "role_created=true",
                        "candidate_created=true",
                        "stored=false",
                        cleanup_function,
                        "trap cleanup EXIT",
                        f"failure_stage={stage!r}",
                        "if [[ $failure_stage == verification ]]; then",
                        "  if ARGOCD_AUTH_TOKEN='candidate-credential-test-only' false; then exit 0; else exit 17; fi",
                        "else",
                        "  if printf '%s' candidate | false; then exit 0; else exit 18; fi",
                        "fi",
                    ))
                    env = dict(os.environ)
                    env.update({
                        "RUNNER_TEMP": str(root),
                        "ARGOCD_AUTH_TOKEN": ADMIN_TOKEN,
                        "PATH": f"{root}{os.pathsep}{env['PATH']}",
                    })
                    result = subprocess.run(
                        ["bash", "-c", harness], cwd=repository, env=env,
                        capture_output=True, text=True, check=False,
                    )
                    self.assertIn(result.returncode, (17, 18), result.stderr)
                    self.assertIn("project role created by this run was preserved", result.stdout)
                    commands = audit.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(commands), 2)
                    self.assertEqual(commands[0], "proj get default --grpc-web --output json")
                    self.assertEqual(
                        commands[1],
                        f"proj role delete-token default {module.ROLE} {METADATA['iat']} --grpc-web",
                    )
                    self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()
