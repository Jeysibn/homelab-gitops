import importlib.util
import json
import os
import io
from contextlib import redirect_stdout
from unittest.mock import patch
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
            self.assertEqual(document["cluster"], {
                "context": "trusted-context", "apiServer": "https://cluster.invalid:6443"
            })
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

    def test_existing_output_is_never_overwritten_for_either_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            output.write_text("preserved")
            for user_id in (0, 1000):
                with self.subTest(user_id=user_id), patch.object(capture.os, "geteuid", return_value=user_id):
                    with self.assertRaises(capture.EvidenceError):
                        capture.write_private(output, {})
                    self.assertEqual(output.read_text(), "preserved")

    def test_rejects_wrong_directory_owner_and_shared_sticky_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            with patch.object(capture.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaisesRegex(capture.EvidenceError, "OUTPUT_DIRECTORY"):
                    capture.write_private(output, {})
            os.chmod(directory, 0o1777)
            with self.assertRaisesRegex(capture.EvidenceError, "OUTPUT_DIRECTORY"):
                capture.write_private(output, {})
            self.assertFalse(output.exists())

    def test_rejects_symlink_file_and_parent_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("preserved")
            link = root / "evidence.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(capture.EvidenceError, "OUTPUT_CREATE"):
                capture.write_private(link, {})
            parent_link = root / "linked-directory"
            parent_link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(capture.EvidenceError, "OUTPUT_DIRECTORY"):
                capture.write_private(parent_link / "new.json", {})
            self.assertEqual(target.read_text(), "preserved")

    def test_protected_regular_create_failure_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            real_open = capture.os.open
            def denied(path, flags, *args, **kwargs):
                if flags & os.O_CREAT:
                    raise PermissionError("private path and configuration")
                return real_open(path, flags, *args, **kwargs)
            with patch.object(capture.os, "open", side_effect=denied):
                with self.assertRaisesRegex(capture.EvidenceError, "OUTPUT_CREATE") as caught:
                    capture.write_private(Path(directory) / "evidence.json", {})
            self.assertNotIn("private", str(caught.exception))

    def test_partial_output_is_removed_after_write_or_permission_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            with patch.object(capture.os, "fchmod", side_effect=PermissionError("sensitive")):
                with self.assertRaisesRegex(capture.EvidenceError, "OUTPUT_PERMISSIONS"):
                    capture.write_private(output, {})
            self.assertFalse(output.exists())
            real_fdopen = capture.os.fdopen
            def cannot_write(fd, *args, **kwargs):
                stream = real_fdopen(fd, *args, **kwargs)
                stream.write = lambda *_: (_ for _ in ()).throw(OSError("sensitive"))
                return stream
            with patch.object(capture.os, "fdopen", side_effect=cannot_write):
                with self.assertRaisesRegex(capture.EvidenceError, "OUTPUT_WRITE"):
                    capture.write_private(output, {})
            self.assertFalse(output.exists())

    def test_kubernetes_errors_report_only_requested_stage(self):
        for stage in ("APP_PROJECT_READ", "RBAC_CONFIGMAP_READ"):
            def denied(*args, **kwargs):
                return SimpleNamespace(returncode=1, stdout="private response", stderr="credentials")
            with self.assertRaises(capture.EvidenceError) as caught:
                capture.checked_json(["kubectl"], run=denied, stage=stage)
            self.assertEqual(str(caught.exception), stage)

    def test_serialization_failure_creates_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            with self.assertRaisesRegex(capture.EvidenceError, "SERIALIZATION"):
                capture.write_private(output, {"invalid": object()})
            self.assertFalse(output.exists())

    def test_capture_attributes_each_kubernetes_failure(self):
        good = ["trusted-context", "https://cluster.invalid:6443",
                json.dumps(kubernetes_object("default")),
                json.dumps(kubernetes_object("argocd-rbac-cm"))]
        stages = ["CONTEXT_VALIDATION", "API_SERVER_VALIDATION", "APP_PROJECT_READ", "RBAC_CONFIGMAP_READ"]
        for failing, stage in enumerate(stages):
            calls = []
            def runner(*args, **kwargs):
                index = len(calls)
                calls.append(args)
                return SimpleNamespace(returncode=1 if index == failing else 0,
                                       stdout=good[index], stderr="private response")
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(capture.EvidenceError, stage):
                    capture.capture("trusted-context", "https://cluster.invalid:6443",
                                    Path(directory) / "evidence.json", run=runner)
                self.assertFalse((Path(directory) / "evidence.json").exists())

    def test_main_reports_sanitized_stage_without_traceback(self):
        with patch("sys.argv", [str(SCRIPT), "--context", "default", "--api-server", "expected", "--output", "unused"]), patch.object(capture, "capture", side_effect=capture.EvidenceError("OUTPUT_WRITE")):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(capture.main(), 1)
            self.assertEqual(out.getvalue(), "::error::Argo CD RBAC evidence capture failed: OUTPUT_WRITE.\n")

    def test_all_supported_fragments_are_preserved(self):
        data = {"policy.github-actions.csv": "fixture", "policy.github-actions-project.csv": "other"}
        self.assertEqual(capture.rbac_evidence(kubernetes_object("argocd-rbac-cm", data=data))["data"], data)

    def test_resource_specific_diagnostics(self):
        with self.assertRaisesRegex(capture.EvidenceError, "APP_PROJECT_VALIDATION"):
            capture.project_evidence(kubernetes_object("wrong-name"))
        for data in ({"policy..csv": ""}, {"policy.x.csv": None}, {"unrelated": ""}):
            with self.assertRaisesRegex(capture.EvidenceError, "RBAC_CONFIGMAP_VALIDATION"):
                capture.rbac_evidence(kubernetes_object("argocd-rbac-cm", data=data))


if __name__ == "__main__":
    unittest.main()
