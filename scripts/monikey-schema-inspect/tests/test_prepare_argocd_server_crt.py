import base64
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "prepare_argocd_server_crt.py"
SPEC = importlib.util.spec_from_file_location("prepare_argocd_server_crt", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


PEM = b"-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"


class ApprovedServerCertificateTests(unittest.TestCase):
    def dates(self, not_before="Jan  1 00:00:00 2026 GMT", not_after="Jan  1 00:00:00 2030 GMT"):
        def runner(command, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=f"notBefore={not_before}\nnotAfter={not_after}\n".encode(),
                stderr=b"",
            )
        return runner

    def test_decodes_only_pem_certificates_and_writes_private_file(self):
        encoded = base64.b64encode(PEM).decode()
        result = module.decode_certificates(encoded, runner=self.dates())
        self.assertEqual(result, PEM)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner-temp" / "trust.pem"
            module.write_private(path, result)
            self.assertEqual(path.read_bytes(), PEM)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_rejects_private_keys_malformed_bundles_and_out_of_date_certs(self):
        invalid = base64.b64encode(b"-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----").decode()
        with self.assertRaises(ValueError):
            module.decode_certificates(invalid, runner=self.dates())
        with self.assertRaises(ValueError):
            module.decode_certificates(base64.b64encode(b"not a certificate").decode(), runner=self.dates())
        expired = base64.b64encode(PEM).decode()
        with self.assertRaisesRegex(ValueError, "validity period"):
            module.decode_certificates(
                expired, runner=self.dates(not_after="Jan  1 00:00:00 2020 GMT")
            )

    def test_optional_secret_is_not_required_and_invalid_material_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trust.pem"
            self.assertFalse(module.prepare_optional("", path))
            self.assertFalse(path.exists())
            with self.assertRaises(ValueError):
                module.prepare_optional("not-base64", path)
            self.assertFalse(path.exists())

    def test_command_does_not_echo_invalid_secret_content(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, ARGOCD_SERVER_TRUST_CERT_B64="fixture-secret-material")
            result = subprocess.run(
                ["python3", str(SCRIPT), str(Path(directory) / "trust.pem")],
                capture_output=True, text=True, env=env, check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a valid, current PEM certificate bundle", result.stdout)
        self.assertNotIn("fixture-secret-material", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
