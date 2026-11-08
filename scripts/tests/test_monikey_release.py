import tempfile
import unittest
from pathlib import Path

from monikey_release import close_gates, promote

SHA = "a" * 40
APP = Path("kubernetes/platform/monikey/manifests/application.yaml")
MIGRATION = Path("kubernetes/platform/monikey/manifests/migration-runner.yaml")


class MoniKeyReleaseTests(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        (root / APP).parent.mkdir(parents=True)
        (root / MIGRATION).parent.mkdir(parents=True, exist_ok=True)
        (root / APP).write_text(
            "ghcr.io/jeysibn/monikey-v2-api:sha-" + "b" * 40 + "\n"
            "ghcr.io/jeysibn/monikey-v2-frontend:sha-" + "b" * 40 + "\n"
        )
        (root / MIGRATION).write_text(
            '  MONIKEY_MIGRATION_ADOPT: "false"\n'
            '  MONIKEY_MIGRATION_APPLY: "false"\n'
            "ghcr.io/jeysibn/monikey-v2-api:sha-" + "b" * 40 + "\n"
        )

    def test_promotes_paired_images_and_adoption_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            promote(root, SHA, True)
            self.assertEqual((root / APP).read_text().count("sha-" + SHA), 2)
            migration = (root / MIGRATION).read_text()
            self.assertIn('MONIKEY_MIGRATION_APPLY: "true"', migration)
            self.assertIn('MONIKEY_MIGRATION_ADOPT: "true"', migration)
            self.assertIn("monikey-v2-api:sha-" + SHA, migration)

    def test_closes_gates_only_for_matching_deployed_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            promote(root, SHA, False)
            close_gates(root, SHA)
            migration = (root / MIGRATION).read_text()
            self.assertIn('MONIKEY_MIGRATION_APPLY: "false"', migration)
            self.assertIn('MONIKEY_MIGRATION_ADOPT: "false"', migration)
            self.assertIn("monikey-v2-api:sha-" + SHA, migration)

    def test_refuses_gate_reset_for_other_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            promote(root, SHA, False)
            with self.assertRaisesRegex(ValueError, "do not match"):
                close_gates(root, "c" * 40)
            self.assertIn(
                'MONIKEY_MIGRATION_APPLY: "true"',
                (root / MIGRATION).read_text(),
            )

    def test_refuses_ambiguous_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            (root / APP).write_text((root / APP).read_text() + "ghcr.io/jeysibn/monikey-v2-api:sha-" + "b" * 40)
            with self.assertRaisesRegex(ValueError, "Expected exactly one"):
                promote(root, SHA, False)


if __name__ == "__main__":
    unittest.main()
