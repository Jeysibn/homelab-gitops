import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "capture_argocd_rbac_evidence.py"
spec = importlib.util.spec_from_file_location("capture_argocd_rbac_evidence", SCRIPT)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def kubernetes_object(name, *, namespace="argocd", data=None, roles=None):
    return {
        "metadata": {
            "name": name, "namespace": namespace, "uid": f"{name}-uid",
            "resourceVersion": "42", "managedFields": [{"fields": "not retained"}],
        },
        "data": data or {},
        "spec": {"roles": roles or []},
        "status": {"jwtTokensByRole": {"token-values-not-retained": "not retained"}},
    }


class CaptureArgocdRbacEvidenceTests(unittest.TestCase):
    def test_captures_minimal_project_and_global_rbac_material_with_private_mode(self):
        role = {
            "name": "monikey-inspection-reader",
            "policies": ["approved-policy"],
            "groups": [],
        }
        project = kubernetes_object("default", roles=[role, {"name": "unrelated"}])
        config_map = kubernetes_object(
            "argocd-rbac-cm", data={"policy.csv": "", "policy.default": "role:readonly"}
        )
        outputs = [
            SimpleNamespace(returncode=0, stdout="trusted-context\n"),
            SimpleNamespace(returncode=0, stdout="https://cluster.invalid:6443"),
            SimpleNamespace(returncode=0, stdout=json.dumps(project)),
            SimpleNamespace(returncode=0, stdout=json.dumps(config_map)),
        ]
        commands = []

        def runner(command, **_kwargs):
            commands.append(command)
            return outputs.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rbac-evidence.json"
            capture.capture("trusted-context", "https://cluster.invalid:6443", output, run=runner)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(commands), 4)
            self.assertEqual(document["appProject"]["spec"]["roles"], [role])
            self.assertEqual(document["configMap"]["data"], config_map["data"])
            self.assertNotIn("managedFields", json.dumps(document))
            self.assertNotIn("jwtTokensByRole", json.dumps(document))

    def test_rejects_context_mismatch_before_reading_resources(self):
        def runner(command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="other-context\n")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(capture.EvidenceError):
                capture.capture("trusted-context", "https://cluster.invalid:6443",
                                Path(directory) / "evidence.json", run=runner)

    def test_rejects_malformed_or_unexpected_configmap_data(self):
        for data in (
            {"policy.csv": "", "unexpected-policy-key": "unknown"},
            {"policy.csv": []},
        ):
            with self.subTest(data="[withheld]"), self.assertRaises(capture.EvidenceError):
                capture.rbac_evidence(kubernetes_object("argocd-rbac-cm", data=data))

    def test_project_capture_omits_token_status_and_keeps_only_target_role(self):
        project = kubernetes_object(
            "default",
            roles=[{"name": "monikey-inspection-reader"}, {"name": "other-role"}],
        )
        output = capture.project_evidence(project)
        self.assertEqual([item["name"] for item in output["spec"]["roles"]],
                         ["monikey-inspection-reader"])
        self.assertNotIn("status", output)


if __name__ == "__main__":
    unittest.main()
