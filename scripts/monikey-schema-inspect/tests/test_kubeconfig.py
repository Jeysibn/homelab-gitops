import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_kubeconfig", ROOT / "build_kubeconfig.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "homelab",
        "clusters": [{"name": "cluster", "cluster": {
            "server": "https://cluster.example.test:6443",
            "certificate-authority-data": "fictional-ca-data",
        }}],
        "contexts": [{"name": "homelab", "context": {
            "cluster": "cluster", "user": "backup", "namespace": "monikey",
        }}],
        "users": [{"name": "backup", "user": {"token": "fictional-bootstrap-token"}}],
    }


class InspectorKubeconfigTests(unittest.TestCase):
    def test_replaces_bootstrap_identity_with_fixed_inspector_identity(self):
        result = module.build_config(fixture(), "header.payload.signature", "homelab",
                                     "https://cluster.example.test:6443")
        self.assertEqual(result["users"], [{"name": "monikey-schema-inspector",
                                           "user": {"token": "header.payload.signature"}}])
        self.assertEqual(result["contexts"][0]["context"]["namespace"], "monikey")
        self.assertEqual(result["contexts"][0]["context"]["user"], "monikey-schema-inspector")

    def test_rejects_unexpected_context_or_server(self):
        with self.assertRaises(ValueError):
            module.build_config(fixture(), "a.b.c", "other", "https://cluster.example.test:6443")
        with self.assertRaises(ValueError):
            module.build_config(fixture(), "a.b.c", "homelab", "https://other.example.test")

    def test_rejects_missing_ca_or_insecure_tls(self):
        config = fixture()
        config["clusters"][0]["cluster"].pop("certificate-authority-data")
        with self.assertRaises(ValueError):
            module.build_config(config, "a.b.c", "homelab", "https://cluster.example.test:6443")
        config = fixture()
        config["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
        with self.assertRaises(ValueError):
            module.build_config(config, "a.b.c", "homelab", "https://cluster.example.test:6443")

    def test_rejects_malformed_token_and_multi_cluster_config(self):
        with self.assertRaises(ValueError):
            module.build_config(fixture(), "not-a-jwt", "homelab",
                                "https://cluster.example.test:6443")
        config = fixture()
        config["clusters"].append(config["clusters"][0])
        with self.assertRaises(ValueError):
            module.build_config(config, "a.b.c", "homelab", "https://cluster.example.test:6443")


if __name__ == "__main__":
    unittest.main()
