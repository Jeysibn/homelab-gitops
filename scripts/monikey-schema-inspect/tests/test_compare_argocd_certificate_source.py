import base64
import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPT_DIR / "compare_argocd_certificate_source.py"
SPEC = importlib.util.spec_from_file_location("compare_argocd_certificate_source", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
REPOSITORY = SCRIPT_DIR.parents[1]


CLUSTER_CONFIG = {
    "current-context": "fixture-context",
    "clusters": [{"cluster": {
        "server": "https://api.fixture.invalid:6443",
        "certificate-authority-data": "Zml4dHVyZS1jYQ==",
        "insecure-skip-tls-verify": False,
    }}],
    "contexts": [{"name": "fixture-context", "context": {
        "user": "monikey-schema-inspector", "namespace": "monikey",
    }}]
}
DEPLOYMENT = {
    "metadata": {"name": "argocd-server"},
    "spec": {"template": {"spec": {"containers": [{
        "name": "argocd-server", "args": ["--staticassets", "/shared"]
    }]}}},
}
INGRESSES = {"items": []}
CERT = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCmZpeHR1cmUKLS0tLS1FTkQgQ0VSVElGSUNBVEUtLS0tLQ=="
OPENSSL_RESULT = SimpleNamespace(
    returncode=0,
    stdout=(
        "sha256 Fingerprint=" + ":".join(module.OBSERVED_ENDPOINT_SHA256[index:index + 2]
                                             for index in range(0, 64, 2))
        + "\nnotBefore=Jan  1 00:00:00 2026 GMT\nnotAfter=Jan  1 00:00:00 2030 GMT\n"
    ).encode(),
    stderr=b"",
)


class FakeKubectl:
    def __init__(self, *, denied=(), fingerprint_result=OPENSSL_RESULT,
                 ingresses=INGRESSES, deployment=DEPLOYMENT):
        self.denied = set(denied)
        self.fingerprint_result = fingerprint_result
        self.ingresses = ingresses
        self.deployment = deployment
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "openssl":
            return self.fingerprint_result
        args = command[1:]
        if args[:2] == ["auth", "can-i"]:
            key = (args[2], args[3])
            return subprocess.CompletedProcess(command, 0, "no\n" if key in self.denied else "yes\n", "")
        if args[:2] == ["get", "deployment/argocd-server"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.deployment), "")
        if args[:2] == ["get", "ingresses.networking.k8s.io"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.ingresses), "")
        if args[:2] == ["get", "configmap/argocd-cmd-params-cm"]:
            return subprocess.CompletedProcess(command, 0, "\n", "")
        if args[:2] == ["get", "secret/argocd-server-tls"]:
            if "metadata.labels" in args[-1]:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, CERT + "\n", "")
        if args[0] == "get" and args[1].startswith("secret/"):
            name = args[1].split("/", 1)[1]
            return subprocess.CompletedProcess(command, 0, CERT + "\n", "")
        raise AssertionError(f"unexpected fixture command: {command}")


def inspect_fixture(runner, *, expected_context="fixture-context", kubeconfig=None):
    return module.inspect(
        expected_context, "https://api.fixture.invalid:6443", runner=runner,
        kubeconfig=kubeconfig or CLUSTER_CONFIG,
    )


class ArgoCertificateSourceTests(unittest.TestCase):
    def test_denied_argocd_metadata_access_blocks_before_any_secret_read(self):
        runner = FakeKubectl(denied={("get", "deployments/argocd-server")})
        result = inspect_fixture(runner)
        self.assertEqual(result["comparison"], "BLOCKED")
        self.assertEqual(result["certificate_source"], "UNKNOWN")
        self.assertFalse(any(command[1:2] == ["get"] and "secret/" in str(command) for command in runner.commands))

    def test_secret_is_not_read_without_named_secret_get_authorization(self):
        runner = FakeKubectl(denied={("get", "secrets/argocd-server-tls")})
        result = inspect_fixture(runner)
        self.assertEqual(result["comparison"], "BLOCKED")
        secret_gets = [command for command in runner.commands if command[1:2] == ["get"] and "secret/" in str(command)]
        self.assertFalse(secret_gets)
        permission_check = next(
            index for index, command in enumerate(runner.commands)
            if command[1:4] == ["auth", "can-i", "get"] and command[4] == "secrets/argocd-server-tls"
        )
        self.assertTrue(all(index < permission_check for index, command in enumerate(runner.commands)
                            if command[1:2] == ["get"]))

    def test_authorized_argocd_server_secret_fingerprint_matches_without_printing_data(self):
        runner = FakeKubectl()
        result = inspect_fixture(runner)
        self.assertEqual(result["comparison"], "MATCH")
        self.assertEqual(result["certificate_source"], "Argo CD server TLS Secret; writer unverified")
        self.assertIn("validity=CURRENT", result["certificate_metadata"])
        serialized = json.dumps(result)
        self.assertNotIn(CERT, serialized)
        self.assertNotIn("fixture.invalid", serialized)
        self.assertTrue(any(command[0] == "openssl" for command in runner.commands))
        data_read = next(command for command in runner.commands if command[1:2] == ["get"]
                         and command[1] == "get" and command[2] == "secret/argocd-server-tls"
                         and "tls\\.crt" in command[-1])
        self.assertEqual(data_read[-1], r"jsonpath={.data.tls\.crt}")
        secret_reads = [command for command in runner.commands
                        if command[1:2] == ["get"] and len(command) > 2
                        and command[2].startswith("secret/")]
        self.assertTrue(all("jsonpath=" in command[-1] and command[-1] != "-o json"
                            for command in secret_reads))
        self.assertFalse(any("privateKey" in " ".join(command) for command in secret_reads))

    def test_fingerprint_mismatch_is_reported_without_revealing_fingerprint_or_target(self):
        mismatch = SimpleNamespace(
            returncode=0,
            stdout=b"sha256 Fingerprint=00:11\nnotBefore=Jan  1 00:00:00 2026 GMT\nnotAfter=Jan  1 00:00:00 2030 GMT\n",
            stderr=b"",
        )
        result = inspect_fixture(FakeKubectl(fingerprint_result=mismatch))
        self.assertEqual(result["comparison"], "MISMATCH")
        self.assertNotIn("00:11", json.dumps(result))
        self.assertNotIn("api.fixture.invalid", json.dumps(result))

    def test_real_openssl_fixture_produces_sha256_and_validity_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / "fixture.pem"
            key = Path(directory) / "fixture-key.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(certificate), "-days", "1",
                 "-subj", "/CN=fixture.invalid"],
                capture_output=True, check=True,
            )
            expected_output = subprocess.run(
                ["openssl", "x509", "-in", str(certificate), "-noout", "-fingerprint", "-sha256"],
                capture_output=True, text=True, check=True,
            ).stdout
            expected = expected_output.split("=", 1)[1].strip().replace(":", "").upper()
            fingerprint, validity = module._certificate_metadata(
                base64.b64encode(certificate.read_bytes()).decode(), subprocess.run
            )
        self.assertEqual(fingerprint, expected)
        self.assertEqual(validity, "CURRENT")

    def test_ingress_tls_secret_is_identified_as_external_terminator(self):
        ingresses = {"items": [{
            "metadata": {"name": "private-ingress", "annotations": {}},
            "spec": {
                "rules": [{"host": "argocd.fixture.invalid", "http": {"paths": [
                    {"backend": {"service": {"name": "argocd-server"}}}
                ]}}],
                "tls": [{"hosts": ["argocd.fixture.invalid"], "secretName": "argocd-edge-tls"}],
            },
        }]}
        runner = FakeKubectl(ingresses=ingresses)
        result = inspect_fixture(runner)
        self.assertEqual(result["comparison"], "MATCH")
        self.assertEqual(result["certificate_source"], "Kubernetes Ingress TLS terminator")
        checks = [command for command in runner.commands if command[1:4] == [
            "auth", "can-i", "get"] and command[4] == "secrets/argocd-edge-tls"]
        reads = [command for command in runner.commands if command[1:2] == ["get"]
                 and command[1] == "get" and command[2] == "secret/argocd-edge-tls"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(reads), 1)
        self.assertLess(runner.commands.index(checks[0]), runner.commands.index(reads[0]))

    def test_wrong_cluster_identity_and_insecure_kubeconfig_fail_closed(self):
        invalid_configs = []
        wrong_server = copy.deepcopy(CLUSTER_CONFIG)
        wrong_server["clusters"][0]["cluster"]["server"] = "https://wrong.invalid"
        invalid_configs.append(wrong_server)
        no_ca = copy.deepcopy(CLUSTER_CONFIG)
        no_ca["clusters"][0]["cluster"].pop("certificate-authority-data")
        invalid_configs.append(no_ca)
        insecure = copy.deepcopy(CLUSTER_CONFIG)
        insecure["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
        invalid_configs.append(insecure)
        wrong_user = copy.deepcopy(CLUSTER_CONFIG)
        wrong_user["contexts"][0]["context"]["user"] = "admin"
        invalid_configs.append(wrong_user)

        for config in invalid_configs:
            runner = FakeKubectl()
            result = inspect_fixture(runner, kubeconfig=config)
            self.assertEqual(result["comparison"], "BLOCKED")
            self.assertFalse(any(command[1:2] == ["auth"] for command in runner.commands))

    def test_argocd_insecure_without_ingress_tls_source_blocks(self):
        deployment = {
            **DEPLOYMENT,
            "spec": {"template": {"spec": {"containers": [{"args": ["--insecure"]}]}}},
        }
        runner = FakeKubectl(deployment=deployment)
        result = inspect_fixture(runner)
        self.assertEqual(result["comparison"], "BLOCKED")

    def test_verification_workflow_is_manual_approved_read_only_and_fixed(self):
        import yaml

        path = REPOSITORY / ".github/workflows/monikey-argocd-certificate-kubernetes-verification.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True, {}))
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        job = workflow["jobs"]["verify"]
        self.assertEqual(workflow["permissions"], {"contents": "read", "actions": "read"})
        self.assertIn("github.ref == 'refs/heads/main' && inputs.confirm_certificate_verification",
                      workflow["jobs"]["environment-preflight"]["if"])
        self.assertEqual(job["environment"]["name"], "monikey-production-inspection")
        script = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("compare_argocd_certificate_source.py", script)
        self.assertNotIn("kubectl apply", script)
        self.assertNotIn("kubectl exec", script)
        self.assertNotIn("--insecure", script)
        self.assertNotIn("upload-artifact", script)


if __name__ == "__main__":
    unittest.main()
