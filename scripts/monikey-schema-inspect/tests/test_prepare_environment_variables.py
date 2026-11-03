import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "prepare_environment_variables", ROOT / "prepare_environment_variables.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
REPO_ROOT = ROOT.parents[1]
SERVER = "https://cluster.example.invalid:6443"
CONTEXT = "homelab-readonly"


def kubeconfig():
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": CONTEXT,
        "contexts": [{
            "name": CONTEXT,
            "context": {"cluster": "homelab-cluster", "user": "backup"},
        }],
        "clusters": [{
            "name": "homelab-cluster",
            "cluster": {
                "server": SERVER,
                "certificate-authority-data": "Q0E=",
                "insecure-skip-tls-verify": False,
            },
        }],
        "users": [{"name": "backup", "user": {"token": "fictional-token"}}],
    }


def variables(entries=()):
    return {"total_count": len(entries), "variables": list(entries)}


def existing(target=None, provisioning=(), production=()):
    target = target or {
        "MONIKEY_SCHEMA_EXPECTED_CONTEXT": CONTEXT,
        "MONIKEY_SCHEMA_EXPECTED_API_SERVER": SERVER,
    }
    return module.build_plan(target, {
        "monikey-inspection-provisioning": variables(provisioning),
        "monikey-production-inspection": variables(production),
    })


class EnvironmentTargetVariableTests(unittest.TestCase):
    def test_extracts_only_selected_context_and_exact_api_server(self):
        self.assertEqual(module.extract_target(kubeconfig()), {
            "MONIKEY_SCHEMA_EXPECTED_CONTEXT": CONTEXT,
            "MONIKEY_SCHEMA_EXPECTED_API_SERVER": SERVER,
        })

    def test_rejects_unselected_or_unsafe_kubeconfig(self):
        invalid = []
        value = kubeconfig()
        value["contexts"].append({"name": "extra", "context": {}})
        invalid.append(value)
        value = kubeconfig()
        value["clusters"].append({"name": "extra", "cluster": {}})
        invalid.append(value)
        value = kubeconfig()
        value["current-context"] = "other"
        invalid.append(value)
        value = kubeconfig()
        value["contexts"][0]["context"]["cluster"] = "other"
        invalid.append(value)
        value = kubeconfig()
        value["clusters"][0]["cluster"].pop("certificate-authority-data")
        invalid.append(value)
        value = kubeconfig()
        value["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
        invalid.append(value)
        value = kubeconfig()
        value["clusters"][0]["cluster"]["server"] = "http://cluster.example.invalid"
        invalid.append(value)
        value = kubeconfig()
        value["clusters"][0]["cluster"]["server"] = "https://user:pass@cluster.example.invalid"
        invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    module.extract_target(value)

    def test_creates_only_missing_values_and_preserves_matching_values(self):
        plan = existing(
            provisioning=[{"name": "MONIKEY_SCHEMA_EXPECTED_CONTEXT", "value": CONTEXT}],
            production=[{"name": name, "value": value} for name, value in (
                ("MONIKEY_SCHEMA_EXPECTED_CONTEXT", CONTEXT),
                ("MONIKEY_SCHEMA_EXPECTED_API_SERVER", SERVER),
            )],
        )
        self.assertEqual(plan["writes"], [{
            "environment": "monikey-inspection-provisioning",
            "name": "MONIKEY_SCHEMA_EXPECTED_API_SERVER",
            "value": SERVER,
        }])

    def test_conflicting_existing_value_fails_without_a_write_plan(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            existing(provisioning=[{
                "name": "MONIKEY_SCHEMA_EXPECTED_API_SERVER",
                "value": "https://different.example.invalid",
            }])

    def test_duplicate_or_malformed_github_variable_entries_fail_closed(self):
        with self.assertRaises(ValueError):
            module.listed_values(variables([
                {"name": "A", "value": "one"}, {"name": "A", "value": "two"}
            ]))
        with self.assertRaises(ValueError):
            module.listed_values({"variables": [{"name": "A"}]})

    def test_verification_requires_matching_values_in_both_environments(self):
        plan = existing()
        correct = variables([
            {"name": "MONIKEY_SCHEMA_EXPECTED_CONTEXT", "value": CONTEXT},
            {"name": "MONIKEY_SCHEMA_EXPECTED_API_SERVER", "value": SERVER},
        ])
        module.verify_plan(plan, {
            "monikey-inspection-provisioning": correct,
            "monikey-production-inspection": correct,
        })
        wrong = variables([{"name": "MONIKEY_SCHEMA_EXPECTED_CONTEXT", "value": CONTEXT}])
        with self.assertRaisesRegex(ValueError, "do not match"):
            module.verify_plan(plan, {
                "monikey-inspection-provisioning": correct,
                "monikey-production-inspection": wrong,
            })

    def test_plan_file_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            module.write_private_json(path, existing())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["target"]["MONIKEY_SCHEMA_EXPECTED_CONTEXT"], CONTEXT)

    def test_workflow_is_manual_approved_local_only_and_non_overwriting(self):
        workflow = (REPO_ROOT / ".github/workflows/monikey-inspection-target-variables.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("github.ref == 'refs/heads/main' && inputs.confirm_target_variable_setup", workflow)
        self.assertIn("name: monikey-inspection-provisioning", workflow)
        self.assertIn("MONIKEY_BACKUP_KUBECONFIG_B64", workflow)
        self.assertIn("MONIKEY_ENVIRONMENT_SECRET_WRITER_TOKEN", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("--method POST", workflow)
        self.assertIn("kubectl config view", workflow)
        self.assertNotIn("tailscale/github-action", workflow)
        self.assertNotIn("kubectl get", workflow)
        self.assertNotIn("kubectl port-forward", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertIn("trap ", workflow)
        self.assertIn("variables?per_page=100", workflow)


if __name__ == "__main__":
    unittest.main()
